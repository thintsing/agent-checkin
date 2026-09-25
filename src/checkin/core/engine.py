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
from .models import CheckinResult, FAILURE_OUTCOMES, Outcome, Recipe
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

        # ② 随机时刻：等窗口。放在取锁之前，避免长时间持锁挡住手动补跑。
        if not now:
            reason = scheduler.should_skip_today(self.cfg)
            if reason:
                log.info("今天不执行：%s", reason)
                return [CheckinResult(r.id, Outcome.SKIPPED, message=f"今天不执行：{reason}")
                        for r in targets]
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

        # ③ 单实例闸门
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

    def _run_all(self, targets: List[Recipe], dry_run: bool,
                 probe: bool) -> List[CheckinResult]:
        results: List[CheckinResult] = []
        for i, recipe in enumerate(targets):
            if i > 0 and not (dry_run or probe):
                gap = jitter.sleep_range(self.cfg.safety.inter_site_gap_range)
                log.info("站点间隔 %.1fs …", gap)
                time.sleep(gap)

            results.append(self._run_one(recipe, dry_run, probe))
        return results

    def _run_one(self, recipe: Recipe, dry_run: bool, probe: bool) -> CheckinResult:
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

            if last.outcome in FAILURE_OUTCOMES and attempts < max_attempts:
                wait = jitter.backoff_sec(attempts)
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
