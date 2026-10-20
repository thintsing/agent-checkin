"""调度引擎：等窗口 -> 遍历配方 -> 幂等判断 -> 分派驱动 -> 熔断/退避 -> 汇总通知。"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import List, Optional

from . import jitter
from . import scheduler
from .lock import RunLock
from .models import CheckinResult, FAILURE_OUTCOMES, Outcome, Recipe, RunBudget
from .state import CST, StateStore
from ..drivers import Driver, driver_for

log = logging.getLogger("checkin.engine")


def _not_before_passed(gate: str) -> bool:
    """`gate` 形如 "10:00"（UTC+8）。判断今天是否已过该时刻。

    空值 = 没声明闸门，直接放行（不告警）。
    非空但解析不出来（写错格式）时返回 True —— **宁可多发一次提醒，也不静默吞掉**。
    "没提醒"是用户无法察觉的失败；"多提醒一次"只是烦，代价小得多。
    """
    gate = (gate or "").strip()
    if not gate:
        return True
    hh, _, mm = gate.partition(":")
    now = datetime.now(CST)
    try:
        target = now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
    except (TypeError, ValueError):
        log.warning("reminder.not_before 格式无法解析（%r），按'已过'处理", gate)
        return True
    return now >= target


class Engine:
    def __init__(self, cfg, recipes: List[Recipe]):
        self.cfg = cfg
        self.recipes = recipes
        self.state = StateStore(cfg.state_path)

    def run(self, only: Optional[List[str]] = None, dry_run: bool = False,
            probe: bool = False, now: bool = False) -> List[CheckinResult]:
        """执行一轮签到。

        now=False（默认）时，先在调度窗口内随机采样一个时刻并等待到点再跑 ——
        "每天同一秒唤醒"本身就是最容易被风控标记的模式，所以随机化放在引擎里，
        而不是各入口各写一遍：无论谁调用 Engine，都自动获得这层保护。

        now=True 表示**调用方已经安排好时机**，不要再内部等待：
          · 手动补跑（`run_checkin.bat --now`）；
          · 计划任务唤醒 —— 2026-10-08 起随机化挪到了 Task Scheduler 的
            `-RandomDelay`（每次运行重新随机），被唤醒即执行。
        注意 now 只影响"等不等窗口"，**不影响"今天该不该跑"**（见 ② ）。
        """
        targets = [r for r in self.recipes if r.enabled and r.mode != "disabled"]
        if only:
            targets = [r for r in targets if r.id in only]
        if not targets:
            return []

        readonly = bool(dry_run or probe)

        # ① 只读模式不碰接口、不写状态，无需等窗口、无需独占
        if readonly:
            return self._run_all(targets, dry_run, probe)

        # ② 今日跳过策略（周末等）—— **与"等不等窗口"无关，任何时候都该生效**。
        #    刻意放在 `if not now` **之外**：计划任务现在用 `--now` 唤醒，若把这条
        #    关进 `not now`，`skip_weekends` 就会被静默绕过 —— 那正是"配了却不起作用"
        #    的隐形故障，本项目最忌讳的一类。
        reason = scheduler.should_skip_today(self.cfg)
        if reason:
            log.info("今天不执行：%s", reason)
            return [CheckinResult(r.id, Outcome.SKIPPED, message=f"今天不执行：{reason}")
                    for r in targets]

        # ③ 随机时刻：等窗口（now=True 已由调用方安排好，不再等）
        if not now:
            target = scheduler.plan_today(self.cfg)
            wait = (target - datetime.now()).total_seconds()
            if wait > 0:
                log.info("计划 %s 执行，等待 %.0f 分钟（窗口 %s–%s，%s）",
                         target.strftime("%H:%M:%S"), wait / 60,
                         self.cfg.schedule.window_start, self.cfg.schedule.window_end,
                         self.cfg.schedule.distribute)
                scheduler.wait_until(
                    target, chunk_seconds=60,
                    on_tick=lambda left: log.info("距执行还有 %.0f 分钟", left / 60)
                    if int(left) % 600 < 60 else None)

        # ④ 单实例闸门
        lock = RunLock(os.path.join(os.path.dirname(self.cfg.state_path), "run.lock"))
        if not lock.acquire():
            log.warning("已有签到实例在运行（%s 被占用），本次不再抢跑", lock.path)
            return [CheckinResult(r.id, Outcome.SKIPPED,
                                  message="另一实例正在运行，已跳过（防止并发重复签到）")
                    for r in targets]
        try:
            return self._run_all(targets, dry_run, probe)
        finally:
            lock.release()

    # 第二遍开跑的最低余粮（秒）。低于这个值就不回头了 ——
    # 补领动辄要几分钟（拉起客户端、等端口），剩十几秒只能收获超时。
    _MIN_PASS2_SEC = 60

    def _run_all(self, targets: List[Recipe], dry_run: bool,
                 probe: bool) -> List[CheckinResult]:
        if dry_run or probe:
            return self._pass(targets, dry_run, probe, None)

        # 整轮总预算 —— **防止一个站点把后面的全部饿死**。
        # 2026-10-17 三站全军覆没：qoder 卡在"守候端口 5400s"那个分支上，
        # 排在它后面的 trae / workbuddy **压根没被执行**，而进程还一直在等
        # ⇒ 连汇总通知都没发出来。详见 models.RunBudget 的说明。
        sa = self.cfg.safety
        budget_sec = int(getattr(sa, "round_budget_sec", 0) or 0)
        deadline = time.monotonic() + budget_sec if budget_sec > 0 else 0.0
        if budget_sec > 0:
            log.info("本轮预算 %d 分钟（超时的站点退到第二遍补领）", budget_sec // 60)

        results = self._pass(targets, dry_run, probe, RunBudget(deadline, 1))
        pending = [r.site_id for r in results if self._still_worth_retrying(r)]
        if not pending:
            return results

        left = RunBudget(deadline, 2).left()
        if left is not None and left < self._MIN_PASS2_SEC:
            log.warning("预算只剩 %.0fs（<%ds），本轮不再回头补领：%s",
                        left, self._MIN_PASS2_SEC, "、".join(pending))
            return results

        log.info("第一遍 %d/%d 个站点未落实，用剩余预算回头补领：%s",
                 len(pending), len(results), "、".join(pending))
        again = [t for t in targets if t.id in pending]
        second = self._pass(again, dry_run, probe, RunBudget(deadline, 2))
        # 按第一遍的**顺序**合并 —— 通知/日志读起来要跟站点声明顺序一致
        merged = {r.site_id: r for r in second}
        return [merged.get(r.site_id, r) for r in results]

    # 值得回头再试一次的语义。SKIPPED（今天已完成/窗口未到/周末）不该重试，
    # NEED_LOGIN 重试毫无意义（要的是人去登录），CIRCUIT 本来就是"今天别再碰"。
    _RETRYABLE = (Outcome.NO_ACTION, Outcome.ERROR)

    def _still_worth_retrying(self, res: CheckinResult) -> bool:
        """这个站点值不值得占用剩余预算再跑一遍。

        除了看结果语义，还要再看一次**幂等状态** —— 典型反例是 manual 站点：
        它第一遍返回 NO_ACTION 表示"提醒已发出"，若不看状态就重试，第二遍会被
        幂等闸门拦成 SKIPPED，反而把一个"我做完了"的结论换成"跳过了"，更难读。
        """
        recipe = next((r for r in self.recipes if r.id == res.site_id), None)
        if res.outcome in (Outcome.NEED_LOGIN, Outcome.CIRCUIT):
            return False
        if self.state.done_today(res.site_id):
            return False
        if recipe is not None and recipe.mode == "manual" \
                and self.state.prompted_today(res.site_id):
            return False
        return res.outcome in self._RETRYABLE

    def _pass(self, targets: List[Recipe], dry_run: bool, probe: bool,
              budget: Optional[RunBudget]) -> List[CheckinResult]:
        """跑一遍站点集合，返回与 `targets` **同序**的结果列表。

        预算挂到 `cfg.budget` 上带下去 —— `Driver.run` 的签名被 AGENTS.md 冻结，
        不能加参数，详见 core/config.py 里 AppConfig.budget 的说明。
        """
        previous = getattr(self.cfg, "budget", None)
        self.cfg.budget = budget
        try:
            results: List[CheckinResult] = []
            for i, recipe in enumerate(targets):
                if i > 0 and not (dry_run or probe):
                    gap = jitter.sleep_range(self.cfg.safety.inter_site_gap_range)
                    if budget is not None:
                        left = budget.left()
                        if left is not None:
                            gap = min(gap, left)
                    log.info("站点间隔 %.1fs …", gap)
                    time.sleep(gap)
                results.append(self._run_one(recipe, dry_run, probe, budget))
            return results
        finally:
            self.cfg.budget = previous

    def _run_one(self, recipe: Recipe, dry_run: bool, probe: bool,
                 budget: Optional[RunBudget] = None) -> CheckinResult:
        sid = recipe.id
        sa = self.cfg.safety
        idle = not (dry_run or probe)      # 只有真跑才走闸门；诊断/演练永远放行

        # ① 开窗时刻闸门（可选，写在 `reminder.not_before`，如 "10:00"）
        #   早于开窗时刻提醒是一记**无效提醒**：用户照做也领不到，还会把当天
        #   "已提醒"的额度消耗掉，导致真正开窗后不再提醒。声明了才生效。
        if idle:
            gate = str(recipe.reminder.get("not_before") or "").strip()
            if gate and not _not_before_passed(gate):
                now_hm = datetime.now(CST).strftime("%H:%M")
                log.info("[%s] 未到开窗时刻 %s（现在 %s），本次不提醒", sid, gate, now_hm)
                return CheckinResult(sid, Outcome.SKIPPED,
                                     message=f"未到 {gate} 开窗时刻（现在 {now_hm}）")

        # ② 幂等闸门：auto 看服务端证据，manual 看"今天是否已提醒过"
        if idle and sa.skip_when_done_today:
            if self.state.done_today(sid):
                log.info("[%s] 今日已完成，跳过", sid)
                return CheckinResult(sid, Outcome.SKIPPED, message="今日已完成")
            if recipe.mode == "manual" and self.state.prompted_today(sid):
                log.info("[%s] 今日已提醒过，跳过（同一天不重复弹窗）", sid)
                return CheckinResult(sid, Outcome.SKIPPED, message="今日已提醒，待人工确认")

        if self.state.failures_today(sid) >= sa.circuit_break_after:
            log.warning("[%s] 当日连续失败已达熔断阈值，跳过", sid)
            return CheckinResult(sid, Outcome.CIRCUIT, message="熔断：当日不再尝试")

        driver: Optional[Driver] = driver_for(recipe.mode)
        if driver is None:
            return CheckinResult(sid, Outcome.NO_ACTION, message=f"未知 mode={recipe.mode}")

        if idle:
            delay = jitter.sleep_range(sa.pre_delay_range)
            if budget is not None:
                left = budget.left()
                if left is not None:
                    delay = min(delay, left)
            log.info("[%s] 拟人延迟 %.1fs 后执行（%s）", sid, delay, recipe.mode)
            time.sleep(delay)
        else:
            log.info("[%s] %s 模式，跳过随机延迟", sid, "probe" if probe else "dry-run")

        last: Optional[CheckinResult] = None
        attempts = 0
        max_attempts = 1 + (0 if (dry_run or probe or recipe.mode != "auto") else sa.max_retries)

        while attempts < max_attempts:
            attempts += 1
            try:
                last = driver.run(recipe, self.cfg, dry_run=dry_run, probe=probe)
            except Exception as e:  # 兜底：驱动内部异常不当作业务失败轰炸
                log.exception("[%s] 驱动异常", sid)
                last = CheckinResult(sid, Outcome.ERROR, message=f"异常: {e}")

            # 驱动**返回 None** 属于契约违约，但绝不能让整轮因此炸掉。
            # 2026-10-20 实证：`client` 驱动里一段代码错位，落到函数末尾返回 None，
            # 下一行 `last.outcome` 抛 AttributeError ⇒ 进程退出码 1 ⇒ 后面的站点
            # 一个都不跑、汇总和推送也一条都发不出（正是本项目最怕的静默失败）。
            # 一个站点的驱动写坏了，惩罚必须止步于这个站点。
            if last is None:
                log.error("[%s] 驱动返回 None（契约违约），按错误处理，不影响其余站点", sid)
                last = CheckinResult(sid, Outcome.ERROR, message="驱动未返回结果")

            if last.outcome in FAILURE_OUTCOMES and attempts < max_attempts:
                wait = jitter.backoff_sec(attempts)
                if budget is not None:
                    left = budget.left()
                    if left is not None:
                        wait = min(wait, left)
                log.warning("[%s] 失败(%s)，%.0fs 后重试", sid, last.outcome.value, wait)
                time.sleep(wait)
                continue
            break

        assert last is not None
        if not idle:
            log.info("[%s] 诊断结果：%s - %s", sid, last.outcome.value, last.message)
        else:
            self.state.record(sid, last.outcome.value)
            # manual：提醒已送出，记下"今天提醒过了"，让同一天的重跑不再弹窗。
            # 只在 NO_ACTION 时记 —— 那是"我这一侧干完了、等你人工确认"的语义；
            # 驱动抛异常落到 ERROR 时不该算提醒成功（用户根本没看到）。
            if recipe.mode == "manual" and last.outcome == Outcome.NO_ACTION:
                self.state.record_prompt(sid)

        log.info("[%s] => %s | %s", sid, last.outcome.value, last.message)
        return last
