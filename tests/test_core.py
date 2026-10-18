"""核心逻辑离线单元测试 —— 不联网、不开浏览器、不触发任何签到。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v

设计约定：这里只测"纯逻辑"（判定语义 / 调度 / 状态 / 退避 / 注入脚本 / 锁 /
引擎的只读与闸门分支）。凡是需要真实浏览器与登录态的部分，属于活体测试，
放 test_cdp_live.py。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from checkin.__main__ import (                                # noqa: E402
    _fallback_hint,
    _install_script,
    _print_task,
    _closing_hint,
    _collect_gaps,
    _psq,
    _task_command,
    _uninstall_script,
    _wake_hint,
)
from checkin.browser import page_script                       # noqa: E402
from checkin.core import jitter, notify, scheduler           # noqa: E402
from checkin.core import log as log_mod                       # noqa: E402
from checkin.core import procenv                              # noqa: E402
from checkin.core.config import (                            # noqa: E402
    AppConfig,
    ChromeConfig,
    SafetyConfig,
    ScheduleConfig,
    load_config,
)
from checkin.core.engine import Engine                       # noqa: E402
from checkin.core.lock import RunLock                        # noqa: E402
from checkin.core.models import (                            # noqa: E402
    RunBudget,
    Action,
    CheckinResult,
    Outcome,
    Recipe,
    Session,
    Verdict,
)
from checkin.core.state import CST, StateStore, today_key         # noqa: E402


# ----------------------------------------------------------------------
# 判定语义
# ----------------------------------------------------------------------

class TestVerdict(unittest.TestCase):
    def setUp(self):
        self.v = Verdict(
            rules=[{"code": 0, "result": "success"},
                   {"code": 10001, "result": "already"}],
            http_401_result="need_login",
            unknown_result="no_action")

    def test_known_codes(self):
        self.assertIs(self.v.map(200, 0), Outcome.SUCCESS)
        self.assertIs(self.v.map(400, 10001), Outcome.ALREADY)   # 业务码优先，忽略 HTTP
        self.assertIs(self.v.map(200, 0), Outcome.SUCCESS)

    def test_business_code_beats_http_status(self):
        """签到成功时接口会返回 HTTP 400 但 code=0 —— 只看状态码会误判。"""
        self.assertIs(self.v.map(400, 0), Outcome.SUCCESS)

    def test_401_is_need_login(self):
        self.assertIs(self.v.map(401, None), Outcome.NEED_LOGIN)
        self.assertIs(self.v.map(403, None), Outcome.NEED_LOGIN)

    def test_unknown_code_is_no_action(self):
        """前端改版出现未知 code 时，绝不猜成成功。"""
        self.assertIs(self.v.map(200, 99999), Outcome.NO_ACTION)
        self.assertIs(self.v.map(200, None), Outcome.NO_ACTION)


class TestVerdictFlags(unittest.TestCase):
    """按响应体 data 判定 —— code=0 只说"请求成功"，不说"今天领到了"。"""

    def setUp(self):
        self.v = Verdict(
            rules=[{"code": 0, "result": "success"},
                   {"code": 10001, "result": "already"}],
            checked_in_flag="today_checked_in",
            result_flag="status",
            result_map={"claimed": "success", "already_claimed": "already",
                        "not_eligible": "no_action", "event_ended": "no_action",
                        "unknown_biz_error": "no_action"})

    def test_already_checked_in_reads_the_flag(self):
        self.assertTrue(self.v.already_checked_in({"today_checked_in": True}))
        self.assertFalse(self.v.already_checked_in({"today_checked_in": False}))
        self.assertFalse(self.v.already_checked_in(None))
        self.assertFalse(self.v.already_checked_in({}))

    def test_flag_must_be_strictly_true(self):
        """字符串 "true" / 1 都不算 —— 只认服务端明确的布尔 true，避免误跳过。"""
        self.assertFalse(self.v.already_checked_in({"today_checked_in": "true"}))
        self.assertFalse(self.v.already_checked_in({"today_checked_in": 1}))

    def test_map_flags_uses_the_client_vocabulary(self):
        self.assertIs(self.v.map_flags({"status": "claimed"}), Outcome.SUCCESS)
        self.assertIs(self.v.map_flags({"status": "already_claimed"}), Outcome.ALREADY)
        self.assertIs(self.v.map_flags({"status": "event_ended"}), Outcome.NO_ACTION)
        self.assertIs(self.v.map_flags({"status": "not_eligible"}), Outcome.NO_ACTION)

    def test_event_ended_must_not_look_like_success(self):
        """回归：活动结束后接口仍回 code=0，只看 code 会报假成功。"""
        flags = {"status": "event_ended"}
        self.assertIs(self.v.map_flags(flags), Outcome.NO_ACTION)
        self.assertIs(self.v.map(200, 0), Outcome.SUCCESS)      # 旧判据会误判
        self.assertIsNot(self.v.map_flags(flags), Outcome.SUCCESS)

    def test_map_flags_returns_none_when_unusable(self):
        """字段缺失或取值不认识 → 返回 None，交给 code 规则兜底。"""
        self.assertIsNone(self.v.map_flags({}))
        self.assertIsNone(self.v.map_flags({"status": "something-new"}))
        self.assertIsNone(self.v.map_flags(None))
        self.assertIsNone(Verdict().map_flags({"status": "claimed"}))

    def test_verdict_without_new_fields_still_works(self):
        """老配方没有这两个字段，行为必须与从前完全一致。"""
        old = Verdict(rules=[{"code": 0, "result": "success"}])
        self.assertFalse(old.already_checked_in({"today_checked_in": True}))
        self.assertIsNone(old.map_flags({"status": "claimed"}))
        self.assertIs(old.map(200, 0), Outcome.SUCCESS)


class TestRecipeFromDict(unittest.TestCase):
    def test_parses_full_recipe(self):
        r = Recipe.from_dict({
            "id": "demo", "name": "Demo", "mode": "auto",
            "origin": "https://example.com",
            "session": {"start_url": "https://example.com/home",
                        "tab_match": ["example.com"]},
            "actions": {"status": {"method": "GET", "path": "/s"},
                        "trigger": {"method": "POST", "path": "/t", "body": {}}},
            "verdict": {"rules": [{"code": 0, "result": "success"}]},
        })
        self.assertEqual(r.id, "demo")
        self.assertTrue(r.enabled)
        self.assertEqual(r.session.tab_match, ["example.com"])
        self.assertEqual(r.actions["trigger"].method, "POST")
        self.assertIs(r.verdict.map(200, 0), Outcome.SUCCESS)

    def test_missing_id_raises(self):
        with self.assertRaises(KeyError):
            Recipe.from_dict({"name": "x"})

    def test_defaults(self):
        r = Recipe.from_dict({"id": "x"})
        self.assertEqual(r.mode, "auto")
        self.assertEqual(r.session.kind, "browser_page")
        self.assertIs(r.verdict.map(200, 1), Outcome.NO_ACTION)


# ----------------------------------------------------------------------
# 调度：随机时刻
# ----------------------------------------------------------------------

def _cfg(start="10:05", end="12:30", distribute="random", skip_weekends=False):
    return AppConfig(
        root=".", chrome=ChromeConfig(), safety=SafetyConfig(),
        state_path="state.json", log_dir="logs",
        schedule=ScheduleConfig(window_start=start, window_end=end,
                                distribute=distribute, skip_weekends=skip_weekends))


class TestScheduler(unittest.TestCase):
    """适配 core/scheduler.py 当前 API（plan_today(cfg) / should_skip_today(cfg)）。

    该模块不提供 rng / 日期注入，所以这里用统计与行为断言，
    并用 window_bounds() 拿到真实窗口端点做对照。
    """

    def setUp(self):
        # window_bounds() 读的是**模块级** scheduler._cfg，而它由 plan_today() /
        # should_skip_today() 写入 —— 谁最后调用谁说了算。不显式复位就会产生
        # **测试顺序耦合**：断言结果取决于"上一个跑过的测试恰好往全局里塞了什么"。
        #
        # 这里显式注入本类使用的窗口，同时让测试**不再依赖 scheduler 的兜底默认值**
        # ——历史上那两处兜底恰好等于本函数的假窗口，于是一直是"偶然绿"；
        # 2026-09-24 窗口迁移（07:40/11:20 → 10:05/12:30）时兜底被同步、假窗口没同步，
        # 这个巧合当场崩掉，暴露了本用例的真实脆性。
        self.addCleanup(setattr, scheduler, "_cfg", None)
        scheduler._cfg = _cfg().schedule

    def test_plan_lands_inside_today_window(self):
        cfg = _cfg()
        for _ in range(30):
            t = scheduler.plan_today(cfg)
            self.assertEqual(t.date(), datetime.now().date())
            self.assertGreaterEqual(t.time(), dtime(10, 5))
            self.assertLessEqual(t.time(), dtime(12, 30))

    def test_beta_clusters_towards_middle(self):
        """Beta(2,2) 应向中间聚集：落在窗口两端各 10% 的比例远低于均匀分布的 20%。"""
        cfg = _cfg()
        start, end = scheduler.window_bounds()
        span = (end - start).total_seconds()
        self.assertGreater(span, 0)
        n, edges = 2000, 0
        for _ in range(n):
            off = (scheduler.plan_today(cfg) - start).total_seconds()
            if off < span * 0.1 or off > span * 0.9:
                edges += 1
        self.assertLess(edges / n, 0.14)     # 均匀分布下约 0.20

    def test_earliest_mode_hits_window_start(self):
        t = scheduler.plan_today(_cfg(distribute="earliest"))
        self.assertEqual(t.time(), dtime(10, 5))

    def test_latest_mode_is_near_window_end(self):
        t = scheduler.plan_today(_cfg(distribute="latest"))
        self.assertGreater(t.time(), dtime(12, 0))   # 0.95 位置 ≈ 12:22

    def test_fallback_window_matches_real_config(self):
        """`scheduler` 的**兜底默认值**必须与 config.yaml 的实际窗口一致。

        兜底只在"配置文件缺失 / 键缺失"时才生效 —— 失同步既不报错也看不出来，
        哪天配置真丢了就会悄悄退回旧窗口。这类静默漂移只能用测试钉死。
        """
        real, _ = load_config(str(PROJECT_ROOT))
        scheduler._cfg = None                        # 强制走兜底分支
        start, end = scheduler.window_bounds()
        self.assertEqual(start.strftime("%H:%M"), real.schedule.window_start)
        self.assertEqual(end.strftime("%H:%M"), real.schedule.window_end)

    def test_window_bounds_are_ordered(self):
        start, end = scheduler.window_bounds()
        self.assertLess(start, end)
        self.assertEqual(start.time(), dtime(10, 5))

    def test_skip_weekends_is_wired_to_config(self):
        self.assertIsNone(scheduler.should_skip_today(_cfg(skip_weekends=False)))
        reason = scheduler.should_skip_today(_cfg(skip_weekends=True))
        is_weekend = datetime.now().weekday() >= 5
        self.assertEqual(reason is not None, is_weekend)

    def test_wait_until_returns_immediately_when_target_past(self):
        before = datetime.now()
        scheduler.wait_until(before - timedelta(seconds=5))
        self.assertLess((datetime.now() - before).total_seconds(), 2.0)

    def test_wake_hint_is_ten_minutes_before_window(self):
        """唤醒时刻 = 窗口开始前 10 分钟（纯算术，与配置值无关）。

        这个"提前量"是**固定**的 —— 随机性由程序在窗口内采样，
        触发器本身**不带** `-RandomDelay`（见下面 test_trigger_* 的回归守卫）。
        """
        self.assertEqual(_wake_hint("10:05"), "09:55")   # 当前实际配置
        self.assertEqual(_wake_hint("07:40"), "07:30")
        self.assertEqual(_wake_hint("00:05"), "00:00")   # 不产生负时刻
        self.assertEqual(_wake_hint("13:00"), "12:50")

    def test_fallback_hint_is_thirty_minutes_after_window_end(self):
        """兜底触发时刻 = 窗口结束 +30 分钟（当前配置即 13:00）。

        2026-10-15 真漏签一天：主触发 09:55 因"开机未登录会话"被静默判 missed，
        程序**根本没启动**（当天连日志都没有）。兜底触发就是为这个场景加的第二次机会。
        """
        self.assertEqual(_fallback_hint("12:30"), "13:00")   # 当前实际配置
        self.assertEqual(_fallback_hint("11:00"), "11:30")
        self.assertEqual(_fallback_hint("23:50"), "00:20")   # 跨天要回绕
        self.assertEqual(_fallback_hint("坏值"), "13:00")    # 解析不了就退回 13:00


# ----------------------------------------------------------------------
# 拟人节奏
# ----------------------------------------------------------------------

class TestJitter(unittest.TestCase):
    def test_sleep_range_bounds(self):
        for _ in range(50):
            v = jitter.sleep_range([1.5, 6.0])
            self.assertGreaterEqual(v, 1.5)
            self.assertLessEqual(v, 6.0)

    def test_backoff_grows_then_caps(self):
        a = jitter.backoff_sec(1, base=10, cap=100)
        b = jitter.backoff_sec(2, base=10, cap=100)
        c = jitter.backoff_sec(5, base=10, cap=100)   # 远超 cap
        self.assertGreater(b, a)
        self.assertLessEqual(c, 100 * 1.25)           # cap + 抖动上限
        self.assertGreaterEqual(c, 100)


# ----------------------------------------------------------------------
# 状态：幂等 / 熔断 / 连续天数
# ----------------------------------------------------------------------

class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")
        self.store = StateStore(self.path)

    def test_initially_clean(self):
        self.assertFalse(self.store.done_today("s"))
        self.assertEqual(self.store.failures_today("s"), 0)
        self.assertEqual(self.store.streak("s"), 0)

    def test_success_marks_done(self):
        self.store.record("s", "success")
        self.assertTrue(self.store.done_today("s"))
        self.assertEqual(self.store.streak("s"), 1)

    def test_already_also_counts_as_done(self):
        self.store.record("s", "already")
        self.assertTrue(self.store.done_today("s"))

    def test_rerun_same_day_does_not_double_count_streak(self):
        self.store.record("s", "success")
        self.store.record("s", "success")
        self.assertEqual(self.store.streak("s"), 1)

    def test_failures_accumulate_then_reset_on_success(self):
        for _ in range(3):
            self.store.record("s", "error")
        self.assertEqual(self.store.failures_today("s"), 3)
        self.store.record("s", "success")
        self.assertEqual(self.store.failures_today("s"), 0)

    def test_streak_resets_after_gap(self):
        s = self.store
        s.record("s", "success")
        # 伪造"上次完成"为前天 → 今天再签，连续天数应重置为 1
        site = s.data["sites"]["s"]
        site["last_done_date"] = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        s._save()
        s.record("s", "success")
        self.assertEqual(s.streak("s"), 1)

    def test_streak_increments_on_consecutive_days(self):
        s = self.store
        s.record("s", "success")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        site = s.data["sites"]["s"]
        site["streak"] = 4
        site["last_done_date"] = yesterday
        s._save()
        s.record("s", "success")
        self.assertEqual(s.streak("s"), 5)

    def test_state_file_has_no_credentials(self):
        self.store.record("s", "success")
        blob = Path(self.path).read_text(encoding="utf-8").lower()
        for kw in ("token", "cookie", "authorization", "password", "secret"):
            self.assertNotIn(kw, blob)

    def test_corrupt_file_recovers(self):
        Path(self.path).write_text("{ not json", encoding="utf-8")
        s = StateStore(self.path)          # 不应抛异常
        self.assertFalse(s.done_today("s"))

    def test_today_key_is_utc8(self):
        self.assertRegex(today_key(), r"^\d{4}-\d{2}-\d{2}$")

    # --- manual 站点的"已提醒"语义（与 done_today 刻意分离）-----------

    def test_initially_not_prompted(self):
        self.assertFalse(self.store.prompted_today("s"))

    def test_record_prompt_marks_prompted(self):
        self.store.record_prompt("s")
        self.assertTrue(self.store.prompted_today("s"))

    def test_prompt_does_not_count_as_done(self):
        """提醒过 ≠ 签到了。合并两者会让手动站点凭空刷连续天数。"""
        self.store.record_prompt("s")
        self.assertFalse(self.store.done_today("s"))

    def test_prompt_does_not_touch_streak_or_done_fields(self):
        self.store.record_prompt("s")
        site = self.store.data["sites"]["s"]
        self.assertEqual(self.store.streak("s"), 0)
        self.assertNotIn("last_done", site)
        self.assertNotIn("last_done_date", site)

    def test_prompt_expires_next_day(self):
        self.store.record_prompt("s")
        site = self.store.data["sites"]["s"]
        site["last_prompt_date"] = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
        self.store._save()
        self.assertFalse(self.store.prompted_today("s"))

    def test_prompt_does_not_reset_failure_counter(self):
        """提醒成功不代表站点恢复了 —— 熔断计数只认站点侧结果。"""
        self.store.record("s", "error")
        self.store.record_prompt("s")
        self.assertEqual(self.store.failures_today("s"), 1)


# ----------------------------------------------------------------------
# 注入页面的 JS
# ----------------------------------------------------------------------

class TestPageScript(unittest.TestCase):
    def _build(self, **kw):
        return page_script.build(
            mode=kw.get("mode", "trigger"),
            status=kw.get("status", ("GET", "/api/status", None)),
            trigger=kw.get("trigger", ("POST", "/api/checkin", {})),
            token_key=kw.get("token_key", ""))

    def test_placeholders_are_all_substituted(self):
        js = self._build()
        self.assertNotIn("__MODE__", js)
        self.assertNotIn("__STATUS_PATH__", js)
        self.assertNotIn("__TRIGGER_PATH__", js)
        self.assertNotIn("__TOKEN_KEY__", js)
        self.assertIn('"/api/status"', js)
        self.assertIn('"/api/checkin"', js)

    def test_body_is_json_encoded_and_js_escaped(self):
        """body 里的引号必须转义成 \\" ，否则 JS 字符串会提前闭合、脚本语法错误。"""
        js = self._build(trigger=("POST", "/t", {"a": 1}))
        self.assertIn(r'const TRIGGER_BODY = "{\"a\": 1}";', js)
        # 裸拼接的坏形态绝不允许出现
        self.assertNotIn('"{"a"', js)

    def test_body_with_quotes_survives(self):
        """注入后的字面量必须能解码回原始 body —— 这直接证明转义正确。

        JSON 先把 " 转成 \\" ，JS 字符串再把它转成 \\\\" ，所以看字面量很反直觉；
        不靠肉眼比对，直接做一次往返解码。
        """
        body = {"q": 'he said "hi"', "nested": {"k": ["a", "b"]}}
        js = self._build(trigger=("POST", "/t", body))
        m = re.search(r'const TRIGGER_BODY = (".*");', js)
        self.assertIsNotNone(m, "没找到 TRIGGER_BODY 字面量")
        json_text = json.loads(m.group(1))       # JS 字面量 → JSON 文本
        self.assertEqual(json.loads(json_text), body)

    def test_empty_body_is_valid(self):
        js = self._build(trigger=("POST", "/t", {}))
        self.assertIn('const TRIGGER_BODY = "{}";', js)

    def test_path_is_escaped(self):
        js = self._build(status=("GET", '/a"b', None))
        self.assertIn(r'/a\"b', js)

    def test_probe_mode_returns_before_trigger(self):
        js = page_script.build("probe", ("GET", "/s", None), ("POST", "/t", {}), "")
        self.assertIn('if (MODE === "probe")', js)
        idx_probe = js.index('if (MODE === "probe")')
        idx_trigger = js.index("diag.triggerCall")
        self.assertLess(idx_probe, idx_trigger)

    def test_token_is_redacted_not_leaked(self):
        js = self._build()
        # 只回传脱敏片段，绝不整串回传
        self.assertIn("redact", js)
        self.assertIn('slice(0, 6)', js)
        self.assertIn('slice(-4)', js)

    def test_fetch_uses_credentials(self):
        self.assertIn('credentials: "include"', self._build())

    def test_status_body_is_injected(self):
        """状态接口也是 POST，body 必须传下去 —— 否则带不上 Content-Type。"""
        js = self._build(status=("POST", "/api/status", {}))
        self.assertIn('const STATUS_BODY = "{}";', js)

    def test_content_type_is_set_when_body_present(self):
        """回归：带 body 却不声明 Content-Type，fetch 会发 text/plain，网关可能直接拒。"""
        js = self._build()
        self.assertIn('headers["Content-Type"] = "application/json"', js)

    def test_checked_in_flag_is_injected(self):
        js = page_script.build("probe", ("POST", "/s", {}), ("POST", "/t", {}), "",
                               "today_checked_in")
        self.assertIn('const CHECKED_IN_FLAG = "today_checked_in";', js)
        self.assertNotIn("__CHECKED_IN_FLAG__", js)

    def test_empty_checked_in_flag_substitutes_cleanly(self):
        js = self._build()
        self.assertIn('const CHECKED_IN_FLAG = "";', js)

    def test_trigger_is_skipped_when_already_checked_in(self):
        """已签就收手：跳过判定必须出现在 triggerCall 之前，少发一次写请求。"""
        js = self._build()
        idx_skip = js.index("skippedBecauseCheckedIn")
        idx_trigger = js.index("diag.triggerCall")
        self.assertLess(idx_skip, idx_trigger)
        self.assertIn("CHECKED_IN_FLAG && flags && flags[CHECKED_IN_FLAG] === true", js)

    def test_extract_flags_only_takes_scalars(self):
        """只回传标量字段，避免把响应体里的无关/嵌套内容带出页面。"""
        js = self._build()
        self.assertIn("function extractFlags", js)
        self.assertIn('typeof v === "boolean"', js)
        self.assertIn('typeof v === "number"', js)


# ----------------------------------------------------------------------
# 运行锁
# ----------------------------------------------------------------------

class TestRunLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "run.lock")

    def test_second_acquire_is_refused(self):
        a, b = RunLock(self.path), RunLock(self.path)
        self.assertTrue(a.acquire())
        try:
            self.assertFalse(b.acquire())
        finally:
            a.release()

    def test_release_allows_reacquire(self):
        a, b = RunLock(self.path), RunLock(self.path)
        self.assertTrue(a.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_context_manager_releases(self):
        with RunLock(self.path):
            self.assertFalse(RunLock(self.path).acquire())
        self.assertTrue(RunLock(self.path).acquire())

    def _write_lock(self, pid, ts=None):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "ts": int(time.time()) if ts is None else ts}, fh)

    @staticmethod
    def _a_dead_pid():
        """拿一个**确定已经退出**的 PID —— 比硬编码某个数字可靠得多。"""
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def test_lock_held_by_a_dead_process_is_reclaimed_immediately(self):
        """**回归（2026-10-17 实测）**：进程被杀/崩掉时 `release()` 不会执行，锁文件留下。

        原先只按时间判陈旧（30 分钟）⇒ 进程早就没了、锁还压着半小时，这期间每一次运行
        （含兜底与收尾触发）都被"另一实例正在运行"整轮跳过，**且不报错不提醒**。
        现在持锁进程不在了就视为陈旧，下一轮立刻能接上。
        """
        dead = self._a_dead_pid()
        self._write_lock(dead, ts=int(time.time()))                   # 时间戳是**现在**
        self.assertTrue(RunLock(self.path)._is_stale(),
                        "持锁进程已死 ⇒ 即便时间戳是刚刚也算陈旧")
        self.assertTrue(RunLock(self.path).acquire(), "陈旧锁必须能被立刻接管")

    def test_lock_held_by_a_live_process_is_respected(self):
        """反过来：持锁进程还活着 ⇒ **必须**让路，防的是并发重复签到。"""
        self._write_lock(os.getpid(), ts=int(time.time()))
        self.assertFalse(RunLock(self.path)._is_stale())
        self.assertFalse(RunLock(self.path).acquire())

    def test_unparsable_lock_file_counts_as_stale(self):
        """锁文件写坏了 ⇒ 当陈旧处理（宁可接管，也不能永久卡死）。"""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertTrue(RunLock(self.path)._is_stale())
        self.assertTrue(RunLock(self.path).acquire())


# ----------------------------------------------------------------------
# 配置加载
# ----------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_loads_real_project(self):
        cfg, recipes = load_config(str(PROJECT_ROOT))
        self.assertTrue(os.path.isabs(cfg.state_path))
        self.assertTrue(os.path.isabs(cfg.log_dir))
        ids = {r.id for r in recipes}
        self.assertIn("workbuddy", ids)
        self.assertIn("qoder", ids)

    def test_schedule_defaults(self):
        cfg, _ = load_config(str(PROJECT_ROOT))
        self.assertLess(dtime(*map(int, cfg.schedule.window_start.split(":"))),
                        dtime(*map(int, cfg.schedule.window_end.split(":"))))
        self.assertEqual(cfg.schedule.timezone, "Asia/Shanghai")

    def test_dataclass_defaults_match_config_yaml(self):
        """`ScheduleConfig` 的字段默认值也必须与 config.yaml 一致。

        与 scheduler 的兜底同理：默认可信只在"配置文件缺失 / 键缺失"时才生效，
        失同步既不报错也看不出来。窗口一旦迁移，这几处必须一起动。
        """
        real, _ = load_config(str(PROJECT_ROOT))
        d = ScheduleConfig()
        self.assertEqual(d.window_start, real.schedule.window_start)
        self.assertEqual(d.window_end, real.schedule.window_end)

    def test_safety_defaults_are_sane(self):
        cfg, _ = load_config(str(PROJECT_ROOT))
        s = cfg.safety
        self.assertLess(s.pre_delay_range[0], s.pre_delay_range[1])
        self.assertLess(s.inter_site_gap_range[0], s.inter_site_gap_range[1])
        self.assertGreaterEqual(s.circuit_break_after, 1)
        self.assertTrue(s.skip_when_done_today)

    def test_workbuddy_recipe_matches_the_live_api(self):
        """回归：这组断言来自 2026-09-24 对活接口的实测，别让它们被改回去。

        - status 曾经是 GET，而该路由只接受 POST → 返回 404，看起来像"接口不存在"。
        - code=0 只代表请求成功，所以必须靠 data.today_checked_in 判断"今天领没领"。
        - data.status 的词表来自客户端自己的 CheckinClaimStatus。
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        wb = next(r for r in recipes if r.id == "workbuddy")

        self.assertEqual(wb.actions["status"].method, "POST")
        self.assertEqual(wb.actions["trigger"].method, "POST")
        self.assertEqual(wb.actions["status"].path,
                         "/billing/meter/checkin-activity-status")
        self.assertEqual(wb.actions["trigger"].path, "/billing/meter/daily-checkin")
        self.assertEqual(wb.verdict.checked_in_flag, "today_checked_in")
        self.assertEqual(wb.verdict.result_flag, "status")
        self.assertEqual(wb.verdict.result_map["event_ended"], "no_action")

    def test_status_path_is_not_the_zombie_endpoint(self):
        """`/billing/meter/checkin-status` 是已废弃的僵尸接口（恒返回全 0），不可使用。"""
        _, recipes = load_config(str(PROJECT_ROOT))
        for r in recipes:
            for act in r.actions.values():
                self.assertNotEqual(act.path, "/billing/meter/checkin-status",
                                    f"配方 {r.id} 用了恒返回全 0 的废弃接口")

    def test_qoder_uses_client_driver_and_declares_its_open_hour(self):
        """Qoder 走**客户端自动化**（2026-09-25 全流程实测通过，证据见 recipes/qoder.yaml 头注释）。

        这条断言被推翻过一次，值得记住为什么：原先写的是 `mode == "manual"`，
        依据是"网页自动化三重取证不通"。那个依据**至今仍然成立**（网页确实不通），
        但它推出的结论错了 —— 不通的是**网页**，不是**自动化**。
        真正的通路是 CDP 接管桌面客户端，当天实测：按钮「领取」→「已领取」，
        服务端 main.log 里 claimable 由 true 变 false。
        ⇒ 守卫真正守的是"结论必须跟着证据走"，而不是"永远保持 manual"。

        `not_before` 现在身兼两职：既是提醒闸门，也是 client 驱动判断
        "日志里的状态属于哪个活动窗口"的窗口起点。改掉它会让"本窗口是否已领"
        跨窗口误判（把上一轮窗口的结论当成今天的）。
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        q = next(r for r in recipes if r.id == "qoder")
        self.assertEqual(q.mode, "client")
        self.assertEqual(q.reminder.get("not_before"), "10:00")
        self.assertEqual(q.actions, {}, "client 配方不该带任何 HTTP 接口动作")
        self.assertTrue(q.reminder.get("toast_body"), "降级路径必须有提醒文案")

    def test_qoder_client_recipe_is_complete(self):
        """client 配方的必填项 —— 缺一个就是"每天准时静默失败"（任务成功、什么也没发生）。"""
        _, recipes = load_config(str(PROJECT_ROOT))
        q = next(r for r in recipes if r.id == "qoder")
        c = q.client
        for key in ("debug_port", "target_match", "data_dir", "process_match"):
            self.assertTrue(c.get(key), f"client.{key} 不能为空")
        self.assertIsInstance(c["debug_port"], int)
        # 可执行文件有两种定位方式，**至少要有一种**：
        #   · exe_glob —— 版本目录会滚动累积时用它（原来 Qoder 这么干）
        #   · exe      —— 2026-10-15 起 Qoder 改用**顶层启动器**（见配方注释：
        #                  glob 指向内层 exe 会绕过启动器，升级后起来就退、端口永不就绪）
        self.assertTrue(str(c.get("exe") or "").strip() or str(c.get("exe_glob") or "").strip(),
                        "exe / exe_glob 必须至少有一个非空，否则永远找不到客户端")
        self.assertTrue(c["close_after"] is True,
                        "领完必须关掉 —— 调试端口是本机任何程序都能接管会话的口子")
        # ⚠ 2026-10-11 实测真失败：客户端重启后落在**设置页**，那一层没有活动入口
        #   ⇒ 两个 open_entry_labels 全 no-button ⇒ no_action ⇒ 当天漏签。
        #   故必须有"回主工作区"的按钮文案（`_open_entry` 推进入口前会先点它，幂等）。
        self.assertTrue(c.get("home_labels"),
                        "缺 home_labels 时，窗口一旦停在设置页就整天找不到入口")

    def test_workbuddy_client_recipe_is_complete(self):
        """WorkBuddy 与 Qoder 的**关键差异**必须钉在配方里。

        2026-10-10 逐项核对时发现两处缺口：
          · `close_after` 注释写着"必须 false"，**字段却漏了** —— 默认 True 会让
            "客户端没在跑 → 由我们拉起"那条路在领完后**把用户的主力工具关掉**；
          · 没有 `wait_port_sec` —— 客户端长期不重启（10-09 13:15 起一直跑到次日）
            造成的无端口形态，每天只能降级提醒，**天天静默漏签**。
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        wb = next(r for r in recipes if r.id == "workbuddy")
        c = wb.client

        self.assertIs(c.get("close_after"), False,
                      "WorkBuddy 是用户全天在用的主力工具，领完绝不能关它")
        self.assertGreater(int(c.get("wait_port_sec") or 0), 0,
                           "客户端可能多日不重启，必须守候端口才能自愈")
        self.assertEqual(wb.reminder.get("not_before"), "10:00",
                         "开窗时刻既是提醒闸门，也是「本窗口已领」判定的窗口起点")
        # 免端口的日志预检：三项缺一都会静默退回 None，等于白配
        ls = c.get("log_state") or {}
        self.assertTrue(str(ls.get("file") or "").endswith("main.log"))
        self.assertTrue(ls.get("marker"), "缺 marker 会把任意同名字段当判据")
        self.assertEqual(ls.get("claimed_field"), "today_checked_in")
        self.assertIs(ls.get("claimed_value"), True)
        # bridge 仍是主路径（不点 DOM，直接走客户端自己的 http 通道）
        bridge = c.get("bridge") or {}
        self.assertTrue(bridge.get("status_js") and bridge.get("claim_js"))

    def test_trae_client_recipe_is_complete(self):
        """Trae CN 的接入契约（2026-10-12 真机端到端跑通）。

        ⚠ 当天曾按"猜"的 DOM 路径写过一版（`workbench.html` + 账号菜单 aria），
        **两条都是错的**：Trae CN 1.107 跑的是 **Solo Lite 新 UI**，renderer URL 是
        `vscode-file://...`（`workbench.html` 永不命中 = 每天静默 no_action）；
        左下角账号触发器点开的是**付费页**、且按钮 class 带构建期 hash。
        ⇒ 改为 **bridge 主路径**：直接调客户端自己的 IPC 通道（实测链：
           `icubeClaimCheckinCredits` → `claimIdeCheckinCredits` →
           `ipcRenderer.invoke('vscode:sandbox::main-invoke-claimCheckinCredits')`）。
        下面的守卫把"这次踩坑"钉住，防止有人凭直觉改回 DOM / 改错 target。
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        t = next((r for r in recipes if r.id == "trae"), None)
        self.assertIsNotNone(t, "找不到 trae 配方")
        c = t.client or {}
        self.assertEqual(t.mode, "client")
        for key in ("exe", "process_match", "debug_port", "target_match", "main_target_match"):
            self.assertTrue(c.get(key), f"client.{key} 不能为空")
        self.assertIsInstance(c["debug_port"], int)
        # 签完关掉"我们拉起的那个"（不留调试端口后门）；用户在用的实例永不受影响。
        # 用户 2026-10-12 选「自动拉起，签完关掉」，与 Qoder 同策略。
        self.assertIs(c.get("close_after"), True,
                      "close_after 只作用于'我们拉起的实例'，签完应关掉不留后门")
        # 无端口守候（IDE 可能连续数天不重启）
        self.assertGreater(int(c.get("wait_port_sec") or 0), 0)

        # ① 主路径必须是 bridge —— 缺任一条就退回"点 DOM"，而 DOM 这条路这里走不通
        bridge = c.get("bridge") or {}
        self.assertTrue(bridge.get("status_js"), "缺 status_js 就无法判「今日已签」，会重复调 claim")
        self.assertTrue(bridge.get("claim_js"), "缺 claim_js 就领不到积分")
        # ② target_match 必须是**实测过的** renderer 特征。写死的旧值是 `workbench.html`，
        #    而真实的 renderer URL 是 `vscode-file://vscode-app/...` ⇒ 永不命中 ⇒ 静默漏签。
        self.assertIn("vscode-file", str(c["target_match"]),
                      "Trae 的 renderer 是 vscode-file:// 形态；match 写错 = 永不命中 = 每天 no_action")
        # ③ 判定"今日已签"的字段名必须配，否则 bridge 会每次都去调 claim（可能被服务端拒绝）
        self.assertEqual(t.verdict.checked_in_flag, "checked_in",
                         "缺它就无法在已签时收手，会重复触发领取")
        # ④ 无可靠日志旁证 ⇒ 不得配 log_state（配了就是把任意同名字段当判据）
        self.assertNotIn("log_state", c, "Trae 日志不落响应体，log_state 必然是假判据")

    def test_bridge_sites_declare_a_reachable_target(self):
        """凡是走 bridge 的站点，`target_match` 都必须非空。

        空 match 在 `_wait_target` 里**永不命中**（它只在 `match in url` 为真时返回），
        结果是每天静默 `no_action` —— 且不报错、不提醒。Trae 接入时正因 match 写成了
        不存在的特征（`workbench.html`）而差点踩到，故立此守卫。
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        sites = [r for r in recipes
                 if isinstance((r.client or {}).get("bridge"), dict)
                 and (r.client or {})["bridge"].get("status_js")]
        self.assertGreaterEqual(len(sites), 2, "至少有 workbuddy / trae 两站走 bridge")
        for r in sites:
            self.assertTrue(str((r.client or {}).get("target_match") or "").strip(),
                            f"{r.id}: bridge 站点必须有可命中的 target_match")

    def test_port_wait_is_symmetric_across_client_recipes(self):
        """「无端口守候」必须在**所有** client 配方上对称配置。

        ⚠ 2026-10-11 反转：此前这里是一条"Qoder 不得有 `wait_port_sec`"的守卫，
        理由是"它没这个场景"。**当天就被现实推翻** —— Qoder 客户端同样可能连续
        多天不重启（当时的实例 10-09 13:15:48 起、跑到 10-10 16:17 才被取代），
        无端口时立刻放弃 ⇒ 当天漏签，而 WorkBuddy（有守候）顺利接管。
        ⇒ 教训：这类"补救"字段属于**场景共性**，不对称 = 等于没修。
        （`log_state` 不在此列 —— 它绑定日志**结构**：Qoder 是 `data_dir` 的
          多会话目录形态，WorkBuddy 是单文件，确实不同，仍不得互相套用。）
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        # 动态取**全部** client 配方，不写死名单 —— 写死的守卫在新增站点时会
        # 静默漏检，而那恰恰是它要防的事（2026-10-12 新增 Trae 时才发现旧版写死）。
        clients = [r for r in recipes if r.client]
        self.assertGreaterEqual(len(clients), 3, "至少有 qoder / workbuddy / trae 三站")
        for r in clients:
            self.assertGreater(
                int((r.client or {}).get("wait_port_sec") or 0), 0,
                f"{r.id}: 客户端可能多日不重启，必须守候端口才能自愈")
        # log_state 仍然只属于 WorkBuddy（绑定日志结构，不是场景共性）
        q = next(r for r in recipes if r.id == "qoder")
        self.assertNotIn("log_state", q.client or {})

    def test_auto_restart_permission_is_symmetric_across_client_recipes(self):
        """用户 2026-10-14 授权"该重启重启，不要摆着" ⇒ **所有** client 站点都要开。

        少配一个，那个站点遇到端口绑定失败（**已知不会自愈**）时就又"摆着"了 ——
        正是被吐槽的行为。与 `wait_port_sec` 同理：这类补救字段属于**场景共性**，
        不对称 = 等于没修（2026-10-11 已在 `wait_port_sec` 上栽过一次）。
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        clients = [r for r in recipes if r.client]
        self.assertGreaterEqual(len(clients), 3, "至少有 qoder / workbuddy / trae 三站")
        for r in clients:
            self.assertIs((r.client or {}).get("restart_on_bind_failure"), True,
                          f"{r.id}: 缺 restart_on_bind_failure，端口绑定失败时会摆着不处理")

    def test_busy_probe_is_symmetric_across_client_recipes(self):
        """用户 2026-10-14 要求"重启前先看有没有在执行的任务" ⇒ **所有** client 站点
        都必须配忙闲判据。

        缺一个的后果是双向的：判据缺失时驱动**保守不重启**（该站点回到"摆着"），
        而若有人图省事把"没有判据也重启"的逻辑加上去，就是直接打断用户 —— 所以这道
        守卫同时钉住"必须有判据"和"必须有监测上限"。
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        clients = [r for r in recipes if r.client]
        self.assertGreaterEqual(len(clients), 3, "至少有 qoder / workbuddy / trae 三站")
        for r in clients:
            probe = (r.client or {}).get("busy_probe") or {}
            self.assertTrue(str(probe.get("busy_js") or "").strip(),
                            f"{r.id}: 缺 busy_probe.busy_js ⇒ 无法判断忙闲 ⇒ 该站点将永远不自动重启")
            self.assertGreater(int(probe.get("wait_busy_sec") or 0), 0,
                               f"{r.id}: 缺 wait_busy_sec ⇒ 客户端忙时会无限等下去")

    def test_debug_ports_are_distinct_and_not_recycled(self):
        """两个 client 站点不得共用调试端口（共用 = 后启动的那个静默绑不上）。

        端口是**一次性资源**：被僵尸句柄占死的端口只能弃用（9334、9335 均如此）。
        这里只做"互不相同 + 是合法端口号"的机械校验；具体值随僵尸情况更换，
        以各自配方的 `client.debug_port` 为准。
        """
        _, recipes = load_config(str(PROJECT_ROOT))
        ports = [r.client["debug_port"] for r in recipes if r.client and r.client.get("debug_port")]
        self.assertEqual(len(ports), len(set(ports)), f"调试端口重复: {ports}")
        for p in ports:
            self.assertTrue(1024 < int(p) < 65536, f"端口号越界: {p}")


# ----------------------------------------------------------------------
# 进程环境清洗（client 驱动的地基）
# ----------------------------------------------------------------------

class TestProcEnv(unittest.TestCase):
    """背景：Agent 环境里的 ELECTRON_RUN_AS_NODE=1 会让被测 Electron 应用
    退化成 Node 模式 —— 不建窗口、不绑调试端口、跑完就退出。这是"启动失败"
    的真根因（对照实测：不清 40s 端口未就绪 / 清了 2s 就绪）。
    """

    def setUp(self):
        from checkin.core import procenv
        self.procenv = procenv

    def test_electron_run_as_node_is_stripped(self):
        base = {"PATH": "x", "ELECTRON_RUN_AS_NODE": "1",
                "NODE_OPTIONS": "--require=shim.cjs", "KEEP": "me"}
        env = self.procenv.clean_env(base)
        self.assertNotIn("ELECTRON_RUN_AS_NODE", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertEqual(env["KEEP"], "me")
        self.assertEqual(env["PATH"], "x")

    def test_agent_namespaced_vars_are_stripped(self):
        base = {"CODEBUDDY_X": "1", "CLAUDE_Y": "2",
                "WORKBUDDY_NODE_ENV": "production", "OTHER": "3"}
        self.assertEqual(sorted(self.procenv.clean_env(base)), ["OTHER"])

    def test_clean_env_does_not_mutate_input(self):
        base = {"ELECTRON_RUN_AS_NODE": "1"}
        self.procenv.clean_env(base)
        self.assertIn("ELECTRON_RUN_AS_NODE", base, "不能改动调用方传进来的 dict")

    # --- pid_alive：进程判活 -------------------------------------------

    def test_a_dead_pid_is_not_alive(self):
        """**僵尸陷阱**（2026-10-17 实测，这条守卫当时竟然是绿的）：

        `OpenProcess` 对**已退出、但句柄还被父进程握着**的进程照样返回句柄 ——
        只看"句柄拿没拿到"的话，`pid_alive` 会**恒为 True**。而它是
        `RunLock._is_stale()` 判活的唯一依据 ⇒ 锁永远不会被判陈旧，
        "进程死了就立刻接管"这条机制从写下那一刻起就没生效过。
        必须再调 `GetExitCodeProcess`，只有退出码仍是 259 才算活着。
        """
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()                       # 确认已退出；Popen 仍握着它的句柄 ⇒ 典型僵尸
        self.assertFalse(self.procenv.pid_alive(p.pid),
                         "已退出的进程必须判为不活 —— 否则运行锁永远不会被判陈旧")

    def test_the_current_process_is_alive(self):
        """反过来不能判死：把活的说成死的会放任并发重复签到。"""
        self.assertTrue(self.procenv.pid_alive(os.getpid()))

    def test_a_pid_that_never_existed_is_not_alive(self):
        self.assertFalse(self.procenv.pid_alive(999999))


# ----------------------------------------------------------------------
# client 驱动的纯逻辑（真启动客户端属于活体测试，不进单元测试）
# ----------------------------------------------------------------------

class TestClientDriverLogic(unittest.TestCase):
    def setUp(self):
        from checkin.drivers.client_claim import ClientDriver
        self.d = ClientDriver()

    def test_version_key_picks_the_newest_build(self):
        """版本目录滚动累积，必须按数字比较 —— 字符串比较会让 0.10.0 输给 0.9.0。"""
        paths = [
            r"...\.qoder-versions\0.9.0\Qoder CN.exe",
            r"...\.qoder-versions\0.10.0\Qoder CN.exe",
            r"...\.qoder-versions\0.4.2\Qoder CN.exe",
        ]
        self.assertTrue(max(paths, key=self.d._version_key).endswith("0.10.0\\Qoder CN.exe"))

    def test_claimed_in_window_rejects_stale_log(self):
        """陈旧日志不算数：上一轮窗口的 claimable=false 与今天无关。"""
        now = datetime.now(CST)
        start = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if now < start:
            start -= timedelta(days=1)
        stale = start - timedelta(hours=1)
        self.assertFalse(self.d._claimed_in_window(False, stale, "10:00"))

    def test_claimed_in_window_accepts_fresh_log(self):
        now = datetime.now(CST)
        fresh = now.replace(hour=10, minute=30, second=0, microsecond=0)
        if fresh > now:
            fresh -= timedelta(days=1)
        self.assertTrue(self.d._claimed_in_window(False, fresh, "10:00"))

    def test_claimable_true_or_missing_never_counts_as_claimed(self):
        now = datetime.now(CST)
        self.assertFalse(self.d._claimed_in_window(True, now, "10:00"))
        self.assertFalse(self.d._claimed_in_window(None, now, "10:00"))
        self.assertFalse(self.d._claimed_in_window(False, None, "10:00"))

    def test_log_state_survives_empty_short_sessions(self):
        """回归（真踩过）：客户端**"启动即关"的短会话不写 campaign 行**。

        原先只扫最近 5 个会话目录 —— 实测在窗口期内跑几次验证就攒下 6 个空会话，
        把唯一带状态的会话挤出了窗口，`_log_state` 返回 None，
        于是"零打扰预检"**静默失效**：配置没问题、单测也全绿，但功能就是不生效。
        用户随手开一下客户端再关掉，同样会制造这种空会话。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logs = os.path.join(tmp.name, "logs")

        good = os.path.join(logs, "20260925-030000.000-1-aaa")     # 较早，但有效
        os.makedirs(good)
        with open(os.path.join(good, "main.log"), "w", encoding="utf-8") as f:
            f.write('[2026-09-25T04:52:12.375Z] [INFO] [main] [Campaign] 活动状态响应解析完成 '
                    '{"normalizationResult":"accepted","claimable":false}\n')

        for i in range(8):                                          # 更新的空会话
            d = os.path.join(logs, f"20260925-05{i:02d}00.000-{i}-bbb")
            os.makedirs(d)
            with open(os.path.join(d, "main.log"), "w", encoding="utf-8") as f:
                f.write("unrelated startup noise\n")

        st = self.d._log_state({"data_dir": tmp.name}, "10:00")
        self.assertIsNotNone(st, "空会话把唯一带状态的会话挤出了扫描窗口")
        self.assertFalse(st["claimable"])
        self.assertEqual(st["at"], "2026-09-25 12:52:12", "UTC 04:52 应换算成 CST 12:52")

    # ------------------------------------------ 单文件日志形态（WorkBuddy）

    @staticmethod
    def _wb_client(log_file, **over):
        c = {"debug_port": 9336, "process_match": "WorkBuddy.exe", "wait_port_sec": 0,
             "log_state": {"file": log_file, "marker": "[Checkin] refreshStatus",
                           "claimed_field": "today_checked_in", "claimed_value": True}}
        c.update(over)
        return c

    @staticmethod
    def _wb_line(flag, minutes_ago=5, marker="[Checkin] refreshStatus"):
        """一行 WorkBuddy 形态的签到日志（时间戳为 UTC，与真机一致）。"""
        from datetime import timezone
        ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return json.dumps(
            {"timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "level": "info",
             "scope": "queue-diag",
             "message": [marker + " -> active",
                         {"uiState": "claimed" if flag else "available",
                          "today_checked_in": flag}]},
            ensure_ascii=False)

    def _tmp_log(self, content):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "main.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_log_state_from_file_reads_the_workbuddy_shape(self):
        """单文件 JSON 日志：`today_checked_in=true` 且时刻在本窗口内 ⇒ 判"已领"。

        时间戳是 **UTC**（客户端日志风格），必须先换算再与窗口起点比 ——
        不换算会整整错 8 小时，把清晨的陈旧记录误当成今天窗口内的。
        """
        path = self._tmp_log(self._wb_line(True) + "\n")
        st = self.d._log_state(self._wb_client(path), "10:00")
        self.assertIsNotNone(st)
        self.assertFalse(st["claimable"], "对外契约与 Qoder 分支一致：已领 ⇒ claimable=False")
        self.assertTrue(st["claimed_in_window"])

    def test_log_state_from_file_not_claimed_when_flag_false(self):
        """`today_checked_in=false` ⇒ claimable=True / claimed_in_window=False（确实该去领）。"""
        path = self._tmp_log(self._wb_line(False) + "\n")
        st = self.d._log_state(self._wb_client(path), "10:00")
        self.assertIsNotNone(st)
        self.assertTrue(st["claimable"])
        self.assertFalse(st["claimed_in_window"])

    def test_log_state_from_file_missing_file_is_none(self):
        missing = os.path.join(tempfile.gettempdir(), "no_such_wb_log_xyz.log")
        self.assertIsNone(self.d._log_state(self._wb_client(missing), "10:00"))

    def test_log_state_from_file_requires_the_marker(self):
        """没命中 marker 的行不许当判据 —— 否则日志里任意同名字段都会污染结论。"""
        path = self._tmp_log(self._wb_line(True, marker="[Unrelated] noise") + "\n")
        self.assertIsNone(self.d._log_state(self._wb_client(path), "10:00"))

    def test_log_state_without_spec_keeps_the_old_session_scan(self):
        """没配 `log_state` 的老站点（Qoder）必须完全维持原行为，不能误走新分支。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(self.d._log_state({"data_dir": tmp.name}, "10:00"))

    # ------------------------------------------ 无端口守候（10-10 真漏签的解法）

    @staticmethod
    def _run_recipe(**client_over):
        client = {"debug_port": 9336, "process_match": "WorkBuddy.exe", "wait_port_sec": 0,
                  "log_state": {"file": "/no/such/log"}}
        client.update(client_over)
        return Recipe.from_dict({"id": "workbuddy", "name": "wb", "enabled": True,
                                 "mode": "client", "origin": "https://x", "client": client})

    def test_running_without_port_waits_then_claims(self):
        """回归（2026-10-10 真漏签）：客户端在跑但没有调试端口时**不再立刻放弃** ——
        按配方守候，端口一出现就接管补领。这就是"客户端长期不重启"的解法。
        """
        r = self._run_recipe(wait_port_sec=60)
        ok = CheckinResult("workbuddy", Outcome.SUCCESS, message="ok")
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running", return_value="cmdline"), \
             mock.patch.object(self.d, "_wait_port", return_value=True) as wp, \
             mock.patch.object(self.d, "_claim", return_value=ok):
            res = self.d.run(r, _cfg())
        self.assertIs(res, ok)
        wp.assert_called_once()
        self.assertEqual(wp.call_args[0][1], 60, "守候时长必须取自配方 wait_port_sec")

    def test_running_without_port_degrades_when_wait_expires(self):
        """守候到点仍没端口 ⇒ 回到"降级提醒"，且绝不误报成功。"""
        r = self._run_recipe(wait_port_sec=60)
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running", return_value="cmdline"), \
             mock.patch.object(self.d, "_wait_port", return_value=False), \
             mock.patch.object(self.d, "_remind") as rem:
            res = self.d.run(r, _cfg())
        self.assertEqual(res.outcome, Outcome.NO_ACTION)
        rem.assert_called_once()

    def test_zero_wait_keeps_the_old_immediate_give_up(self):
        """缺省 0 = 不守候，维持老行为（对 Qoder 等无此场景的站点零影响）。"""
        r = self._run_recipe(wait_port_sec=0)
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running", return_value="cmdline"), \
             mock.patch.object(self.d, "_wait_port") as wp, \
             mock.patch.object(self.d, "_remind"):
            res = self.d.run(r, _cfg())
        wp.assert_not_called()
        self.assertEqual(res.outcome, Outcome.NO_ACTION)

    # ------------------------------ 端口绑定失败（2026-10-14 真漏签一天的新形态）

    def _port_arg_cmdline(self):
        return ('"C:\\Users\\x\\AppData\\Local\\Programs\\Trae CN\\Trae CN.exe" '
                '--remote-debugging-port=9338')

    def test_port_arg_present_but_unbound_is_not_waited(self):
        """回归（2026-10-14 真漏签一天）：命令行**已带**端口参数却连不上 ⇒ 那次启动的
        端口绑定失败（实测：旧实例 11:54:28 才 quit、新实例 11:54:29 就起来，
        相隔 1 秒，9338 没释放完，新实例 listen 失败后静默继续跑）。
        这种实例整个生命周期都不会有端口 ⇒ **守候是无用的**，必须立刻提示重启客户端。
        """
        r = self._run_recipe(debug_port=9338, wait_port_sec=5400)
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running", return_value=self._port_arg_cmdline()), \
             mock.patch.object(self.d, "_wait_port", return_value=False) as wp, \
             mock.patch.object(self.d, "_claim") as claim, \
             mock.patch.object(self.d, "_remind") as rem:
            res = self.d.run(r, _cfg())
        self.assertEqual(res.outcome, Outcome.NO_ACTION)
        self.assertEqual(res.detail.get("reason"), "debug_port_bind_failed")
        claim.assert_not_called()
        rem.assert_called_once()
        # 只做一次 45s 静默期探测，绝不按 wait_port_sec 长守候（那天白等了 90 分钟）
        self.assertEqual(wp.call_count, 1)
        self.assertEqual(wp.call_args[0][1], self.d._PORT_BIND_GRACE_SEC)

    def test_port_arg_present_and_late_bound_still_claims(self):
        """已带参数但端口**延迟**就绪（客户端刚启动、还没 listen 完）⇒ 照常接管补领，
        不能把"慢"误判成"绑定失败"。
        """
        r = self._run_recipe(debug_port=9338, wait_port_sec=5400)
        ok = CheckinResult("workbuddy", Outcome.SUCCESS, message="ok")
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running", return_value=self._port_arg_cmdline()), \
             mock.patch.object(self.d, "_wait_port", return_value=True), \
             mock.patch.object(self.d, "_claim", return_value=ok) as claim:
            res = self.d.run(r, _cfg())
        self.assertIs(res, ok)
        claim.assert_called_once()

    def test_port_arg_of_another_port_falls_back_to_waiting(self):
        """命令行带的是**别的**端口号（不是本站点配方那个）⇒ 不算"绑定失败"，
        仍走原来的长守候（用户可能重启时带了另一个站的端口）。"""
        r = self._run_recipe(debug_port=9338, wait_port_sec=60)
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running",
                               return_value='"...exe" --remote-debugging-port=9999'), \
             mock.patch.object(self.d, "_wait_port", return_value=False) as wp, \
             mock.patch.object(self.d, "_remind"):
            res = self.d.run(r, _cfg())
        self.assertEqual(res.outcome, Outcome.NO_ACTION)
        self.assertEqual(res.detail.get("reason"), "already_running_without_debug_port")
        self.assertEqual(wp.call_args[0][1], 60)

    # ------------------------ 自动重启自愈（用户 2026-10-14 授权"该重启重启"）

    def test_bind_failure_auto_restarts_and_retries_when_enabled(self):
        """端口绑定失败 + 配方开启重启 + **配了忙闲判据且客户端空闲** ⇒
        自动重启并当场重试，而不是把问题摆在那里等人工（这正是被吐槽的行为）。"""
        r = self._busy_recipe()
        ok = CheckinResult("workbuddy", Outcome.SUCCESS, message="ok")
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running", return_value=self._port_arg_cmdline()), \
             mock.patch.object(self.d, "_wait_port", return_value=False), \
             mock.patch.object(self.d, "_client_busy", return_value=False), \
             mock.patch.object(self.d, "_restart_client", return_value=True) as rs, \
             mock.patch.object(self.d, "_claim", return_value=ok) as claim:
            res = self.d.run(r, _cfg())
        self.assertIs(res, ok, "重启成功后就该当场接管，而不是报 no_action")
        rs.assert_called_once()
        claim.assert_called_once()

    def test_bind_failure_does_not_restart_when_disabled(self):
        """**没开**这个开关的站点绝不能被自动重启 —— 不做无谓打断（权限是窄口的）。"""
        r = self._run_recipe(debug_port=9338, wait_port_sec=60)
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running", return_value=self._port_arg_cmdline()), \
             mock.patch.object(self.d, "_wait_port", return_value=False), \
             mock.patch.object(self.d, "_restart_client") as rs, \
             mock.patch.object(self.d, "_remind"):
            res = self.d.run(r, _cfg())
        rs.assert_not_called()
        self.assertEqual(res.detail.get("reason"), "debug_port_bind_failed")

    def test_restart_sequence_waits_for_port_release(self):
        """重启四步必须齐全，尤其**等端口释放**不能省 —— 抢在旧 socket 释放前拉起，
        新实例会再踩同一个 `listen()` 失败，等于白重启（2026-10-14 漏签的成因）。"""
        r = self._run_recipe(debug_port=9338)
        order = []
        app_calls = {"n": 0}

        def fake_app_running(_c):
            app_calls["n"] += 1
            return "cmdline" if app_calls["n"] == 1 else ""      # 第二次起已退出

        port_calls = {"n": 0}

        def fake_port_alive(_p):
            port_calls["n"] += 1
            return port_calls["n"] == 1                          # 先仍占用，之后释放

        with mock.patch.object(self.d, "_resolve_exe", return_value="C:/x.exe"), \
             mock.patch.object(self.d, "_shutdown",
                               side_effect=lambda p: order.append("shutdown")), \
             mock.patch.object(self.d, "_kill_by_name",
                               side_effect=lambda n: order.append("kill")), \
             mock.patch.object(self.d, "_app_running", side_effect=fake_app_running), \
             mock.patch.object(self.d, "_port_alive", side_effect=fake_port_alive), \
             mock.patch.object(self.d, "_wait_port", return_value=True), \
             mock.patch("checkin.drivers.client_claim.time.sleep"), \
             mock.patch("checkin.drivers.client_claim.procenv.spawn",
                        side_effect=lambda a: order.append("spawn") or type(
                            "P", (), {"pid": 1})()) as sp:
            ok = self.d._restart_client(r.client, 9338)
        self.assertTrue(ok)
        self.assertEqual(order, ["shutdown", "kill", "spawn"],
                         "顺序必须是：优雅关 → 强关 → 拉起；且强关前确认过进程还在")
        self.assertGreaterEqual(port_calls["n"], 2,
                                "必须**轮询等到**端口真的释放才拉起，否则会再踩 listen 失败")
        self.assertIn("--remote-debugging-port=9338", sp.call_args[0][0])

    def test_restart_gives_up_when_process_wont_die(self):
        """进程杀不掉时**不许**继续拉起（会变成两个实例抢端口），直接放弃转人工。"""
        r = self._run_recipe(debug_port=9338)
        with mock.patch.object(self.d, "_resolve_exe", return_value="C:/x.exe"), \
             mock.patch.object(self.d, "_shutdown"), \
             mock.patch.object(self.d, "_kill_by_name"), \
             mock.patch.object(self.d, "_app_running", return_value="cmdline"), \
             mock.patch("checkin.drivers.client_claim.time.sleep"), \
             mock.patch("checkin.drivers.client_claim.procenv.spawn") as sp:
            ok = self.d._restart_client(r.client, 9338)
        self.assertFalse(ok)
        sp.assert_not_called()

    # -------- 端口真假的两道判据（2026-10-15 被假端口骗了半天）

    class _FakeResp:
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _patch_http_ok(self):
        return mock.patch("checkin.drivers.client_claim._LOCAL_OPENER")

    def test_port_alive_rejects_zombie_listener(self):
        """HTTP 通了还不算 —— 监听者 PID 已死 = **僵尸 socket**，必须判不可用。

        2026-10-15 第三次踩到：`netstat` 显示 LISTENING，但记的是**已死 PID**，
        客户端 `listen()` 失败后静默没端口。若只信 HTTP，会被"挂起超时"
        或沙箱对无监听端口返回的 502 同时骗过去。
        """
        with self._patch_http_ok() as O, \
             mock.patch.object(type(self.d), "_port_listener_pid", return_value=12060), \
             mock.patch.object(type(self.d), "_pid_alive", return_value=False):
            O.open.return_value = self._FakeResp()
            self.assertFalse(self.d._port_alive(9339))

    def test_port_alive_accepts_healthy_listener(self):
        """HTTP 通 + PID 活着 ⇒ 真端口。"""
        with self._patch_http_ok() as O, \
             mock.patch.object(type(self.d), "_port_listener_pid", return_value=16984), \
             mock.patch.object(type(self.d), "_pid_alive", return_value=True):
            O.open.return_value = self._FakeResp()
            self.assertTrue(self.d._port_alive(9339))

    def test_port_alive_http_failure_short_circuits(self):
        """HTTP 就不通时**不该**再跑 netstat（省掉一次子进程）。"""
        with self._patch_http_ok() as O, \
             mock.patch.object(type(self.d), "_port_listener_pid") as lp:
            O.open.side_effect = OSError("refused")
            self.assertFalse(self.d._port_alive(9339))
            lp.assert_not_called()

    def test_port_alive_degrades_when_netstat_unavailable(self):
        """netstat 查不到（被拦/超时）⇒ 降级为只信 HTTP，不误杀好端口。"""
        with self._patch_http_ok() as O, \
             mock.patch.object(type(self.d), "_port_listener_pid", return_value=None):
            O.open.return_value = self._FakeResp()
            self.assertTrue(self.d._port_alive(9339))

    def test_port_listener_pid_parses_netstat(self):
        """从 netstat 输出里正确取出 LISTENING 行的 PID。"""
        fake = (
            "  TCP    127.0.0.1:9339         0.0.0.0:0              LISTENING       16984\n"
            "  TCP    127.0.0.1:9339         127.0.0.1:51263        CLOSE_WAIT      16984\n"
            "  TCP    127.0.0.1:49201        127.0.0.1:9339         TIME_WAIT       0\n"
        )

        class _R:
            stdout = fake.encode("utf-8")

        with mock.patch("checkin.drivers.client_claim.subprocess.run",
                        return_value=_R()):
            self.assertEqual(self.d._port_listener_pid(9339), 16984)
        # 没有 LISTENING 行的端口 ⇒ None（不是 0，也不是抛错）
        with mock.patch("checkin.drivers.client_claim.subprocess.run",
                        return_value=_R()):
            self.assertIsNone(self.d._port_listener_pid(9999))

    @staticmethod
    def _ctypes_stub(handle=4321, exit_code=259):
        """假 ctypes：能分别表达"句柄拿不拿得到"与"进程退没退出"两个维度。

        必须分开表达 —— 2026-10-17 就是栽在只看了第一个维度上（见下一条用例）。
        """
        C = mock.MagicMock()
        C.windll.kernel32.OpenProcess.return_value = handle
        C.c_uint32.return_value.value = exit_code      # 259 = STILL_ACTIVE
        C.windll.kernel32.GetExitCodeProcess.return_value = 1
        return C

    def test_pid_alive_uses_openprocess(self):
        """判活用 `OpenProcess` 而不是 `os.kill` —— 沙箱里 os.kill 对所有 PID 都抛错。

        2026-10-15 实测：os.kill 把 9336/9338/9339 三个**活**端口全判成"监听者已死"。
        """
        with mock.patch("checkin.core.procenv.ctypes", self._ctypes_stub()):
            self.assertTrue(self.d._pid_alive(12108))
        with mock.patch("checkin.core.procenv.ctypes",
                        self._ctypes_stub(handle=None)):
            self.assertFalse(self.d._pid_alive(12060), "拿不到句柄 = 那个进程没了")

    def test_pid_alive_treats_a_zombie_as_dead(self):
        """**句柄拿得到 ≠ 活着**（2026-10-17 实测，这条守卫当时是绿的假象）：

        僵尸进程（已退出、但父进程还握着句柄）`OpenProcess` 照样返回句柄 ——
        只看句柄 ⇒ `pid_alive` 恒为 True ⇒ `RunLock` 判活形同虚设，
        "持锁进程死了就立刻接管"从写下那一刻起一次都没生效过。
        必须再取退出码，只有 259(STILL_ACTIVE) 才算活着。
        """
        with mock.patch("checkin.core.procenv.ctypes",
                        self._ctypes_stub(exit_code=0)):
            self.assertFalse(self.d._pid_alive(12108))

    def test_pid_alive_degrades_to_true_on_api_failure(self):
        """API 本身不可用时返回 True —— 宁可漏判僵尸，也别把好端口说成坏。"""
        C = mock.MagicMock()
        C.windll.kernel32.OpenProcess.side_effect = OSError("blocked")
        with mock.patch("checkin.core.procenv.ctypes", C):
            self.assertTrue(self.d._pid_alive(12108))


    def _busy_recipe(self):
        return self._run_recipe(debug_port=9338, wait_port_sec=60,
                                restart_on_bind_failure=True,
                                busy_probe={"busy_js": "BUSY()", "wait_busy_sec": 1200})

    def test_restart_allowed_when_client_idle(self):
        r = self._busy_recipe()
        with mock.patch.object(self.d, "_client_busy", return_value=False):
            self.assertTrue(self.d._safe_to_restart(r, r.client, 9338, "m"))

    def test_restart_waits_when_client_busy(self):
        """忙 ⇒ **不立刻重启**，而是挂载监测；等到空闲才放行。"""
        r = self._busy_recipe()
        with mock.patch.object(self.d, "_client_busy", return_value=True), \
             mock.patch.object(self.d, "_wait_client_idle", return_value=True) as w:
            self.assertTrue(self.d._safe_to_restart(r, r.client, 9338, "m"))
        w.assert_called_once()
        self.assertEqual(w.call_args[0][3], 1200, "监测上限必须取自配方 wait_busy_sec")

    def test_restart_refused_when_busy_never_ends(self):
        """监测超时仍忙 ⇒ 不重启（宁可这次漏签）。"""
        r = self._busy_recipe()
        with mock.patch.object(self.d, "_client_busy", return_value=True), \
             mock.patch.object(self.d, "_wait_client_idle", return_value=False):
            self.assertFalse(self.d._safe_to_restart(r, r.client, 9338, "m"))

    def test_restart_refused_when_busy_state_unknown(self):
        """看不清（None）⇒ 不重启。这是本机制的核心：**不可知 ≠ 空闲**。"""
        r = self._busy_recipe()
        with mock.patch.object(self.d, "_client_busy", return_value=None):
            self.assertFalse(self.d._safe_to_restart(r, r.client, 9338, "m"))

    def test_restart_refused_without_busy_probe(self):
        """没配忙闲判据 ⇒ 一律不重启（没有判据就不能动用户的客户端）。"""
        r = self._run_recipe(debug_port=9338, restart_on_bind_failure=True)
        with mock.patch.object(self.d, "_client_busy") as cb:
            self.assertFalse(self.d._safe_to_restart(r, r.client, 9338, "m"))
        cb.assert_not_called()

    def test_bind_failure_busy_does_not_restart_end_to_end(self):
        """端到端：忙 ⇒ `run()` 不重启客户端，降级为提醒。"""
        r = self._busy_recipe()
        with mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running", return_value=self._port_arg_cmdline()), \
             mock.patch.object(self.d, "_wait_port", return_value=False), \
             mock.patch.object(self.d, "_safe_to_restart", return_value=False), \
             mock.patch.object(self.d, "_restart_client") as rs, \
             mock.patch.object(self.d, "_remind") as rem:
            res = self.d.run(r, _cfg())
        rs.assert_not_called()
        rem.assert_called_once()
        self.assertEqual(res.detail.get("reason"), "debug_port_bind_failed")

    def test_client_busy_normalises_return(self):
        """表达式必须返回**严格布尔**；返回别的（undefined/字符串/数字）一律算"不可知"。

        为什么这么严：`if (val is True)` 之外的任何真值都被当成"看不清"，
        才不会因为一句 `return "busy"` 就让驱动以为看懂了。
        """
        class FakePage:
            def __init__(self, *a, **k):
                self.val = None
            def evaluate(self, expr, await_promise=True):
                return self.val
            def close(self):
                pass

        cfg = {"busy_probe": {"busy_js": "BUSY()"}}
        for raw, want in ((True, True), (False, False), (None, None),
                          ("true", None), (1, None), (0, None), ({}, None)):
            with mock.patch.object(self.d, "_wait_target",
                                   return_value={"webSocketDebuggerUrl": "ws://x"}), \
                 mock.patch("checkin.drivers.client_claim.CDPPage") as P:
                page = FakePage()
                page.val = raw
                P.return_value = page
                got = self.d._client_busy(9338, "m", cfg)
            self.assertEqual(got, want, f"表达式返回 {raw!r} 应归一化为 {want!r}")

    def test_js_uses_full_event_sequence_not_bare_click(self):
        """回归：该 UI 绑 pointerdown，只派发 click() 会**静默无效**（踩过）。"""
        _, click, markers = self.d._js({"claim_button": "领取"})
        for evt in ("pointerdown", "mousedown", "pointerup", "mouseup", "click"):
            self.assertIn(f"'{evt}'", click)
        self.assertIn("composed: true", click, "不穿透 shadow DOM 就点不到")
        self.assertIn('"领取"', click)
        self.assertIn("已领取", markers)

    def test_app_running_query_cannot_match_itself(self):
        """回归（真踩过）：查询脚本本身是 `powershell.exe` 的命令行参数，里面含
        `*Qoder CN.exe*` 字面量 —— 只按 CommandLine 匹配会**匹配到它自己**。
        后果是致命的：驱动永远认为"客户端在运行"→ 永远走降级提醒 → **从不自动领取**
        （配好了也白配，而且不报错）。
        """
        script = self.d._app_running_script("Qoder CN.exe")
        self.assertIn("$_.Name -eq 'Qoder CN.exe'", script,
                      "必须按进程名过滤，否则 powershell.exe 会自我匹配")
        self.assertIn("$_.ProcessId -ne $PID", script, "第二道保险：排除查询者自己")
        self.assertIn("app-host.cjs", script, "必须排除常驻的 native-messaging-host")

    def test_app_running_script_escapes_single_quotes(self):
        """进程名含单引号时必须双写转义，否则拼出来的 PowerShell 语法直接炸。"""
        self.assertIn("'O''Brien.exe'", self.d._app_running_script("O'Brien.exe"))

    def test_app_running_query_hides_its_console(self):
        """进程查询派生 powershell.exe 时必须带 `CREATE_NO_WINDOW`。

        计划任务用 pythonw（无控制台）运行；不声明这个 flag，Windows 会为这个
        控制台子进程**新建一个可见窗口** —— 一次闪屏就能让"不弹黑框"白做。
        """
        from checkin.core import procenv
        seen = {}

        def fake_run(*a, **kw):
            seen.update(kw)
            return mock.Mock(stdout="", returncode=0)

        with mock.patch("checkin.drivers.client_claim.subprocess.run", side_effect=fake_run):
            self.d._app_running({"process_match": "Qoder CN.exe"})
        self.assertEqual(seen.get("creationflags"), procenv.NO_WINDOW)

    def test_open_entry_scripts_click_by_aria_label(self):
        """回归：客户端外壳的入口**只有 aria-label 可依**（纯图标、无文案）。
        实测就是"查看我的用量 → 打开 Rewards"这两步。
        """
        from checkin.drivers.client_claim import _JS_CLICK_ARIA
        self.assertIn("aria-label", _JS_CLICK_ARIA)
        self.assertIn("includes(label)", _JS_CLICK_ARIA)

    def test_open_entry_uses_full_event_sequence(self):
        """回归：外壳入口与活动页按钮同属一个 UI 框架、同样绑 pointerdown；
        只派发 click() 会**静默无效**（与 _JS_CLICK 踩过的是同一个坑）。
        """
        from checkin.drivers.client_claim import _JS_CLICK_ARIA
        for evt in ("pointerdown", "mousedown", "pointerup", "mouseup", "click"):
            self.assertIn(f"'{evt}'", _JS_CLICK_ARIA)
        self.assertIn("composed: true", _JS_CLICK_ARIA, "不穿透 shadow DOM 就点不到")

    def test_home_click_script_matches_by_text(self):
        """「返回应用」按钮**只有文本、没有 aria-label**（2026-10-11 实测），
        所以回主工作区必须走"按文本匹配"的实现 —— 用 aria 那版会永远 `no-button`。
        """
        from checkin.drivers.client_claim import _JS_CLICK_TEXT
        self.assertIn("innerText", _JS_CLICK_TEXT)
        self.assertIn("=== label", _JS_CLICK_TEXT, "必须精确匹配，避免误点同名前缀元素")
        for evt in ("pointerdown", "mousedown", "pointerup", "mouseup", "click"):
            self.assertIn(f"'{evt}'", _JS_CLICK_TEXT)
        self.assertIn("composed: true", _JS_CLICK_TEXT)

    def test_open_entry_returns_home_before_clicking_entry(self):
        """回归（2026-10-11 真失败）：客户端停在**子页面**（设置页）时，活动入口
        一个都找不到 ⇒ 两个 label 全 `no-button` ⇒ `no_action` ⇒ **当天漏签**。

        修法：推进入口前先点 `home_labels`（「返回应用」）回到主工作区。
        这条断言**调用顺序** —— 顺序反了（先找入口再回主工作区）在设置页上照样白跑。
        """
        seen = []

        class _Page:
            def evaluate(self, js, await_promise=False):
                for tag in ("返回应用", "入口A"):
                    if f'"{tag}"' in js:
                        seen.append(tag)
                        return "dispatched"
                return "no-button"

            def close(self):
                pass

        c = {"open_entry_labels": ["入口A"], "home_labels": ["返回应用"],
             "main_target_match": "x", "entry_delay_sec": 0, "entry_timeout_sec": 5}
        with mock.patch.object(self.d, "_main_target",
                               return_value={"webSocketDebuggerUrl": "ws://x"}), \
                mock.patch("checkin.drivers.client_claim.CDPPage", return_value=_Page()), \
                mock.patch("checkin.drivers.client_claim.time.sleep"):
            self.d._open_entry(9334, c)
        self.assertEqual(seen[:2], ["返回应用", "入口A"],
                         "必须先回主工作区、再点入口 —— 顺序反了在设置页上照样找不到入口")

    def test_home_click_is_idempotent_when_already_home(self):
        """已经在主工作区时（找不到「返回应用」按钮）不得有任何副作用。

        幂等是这条修复能不能上的前提：主工作区**本来就没有**该按钮，
        点不到 == 什么都不做。若这里会因"找不到"而中断，正常路径反而会被它拖死。
        """
        seen = []

        class _Page:
            def evaluate(self, js, await_promise=False):
                if '"返回应用"' in js:
                    seen.append("home-miss")
                    return "no-button"      # 主工作区：该按钮不存在
                seen.append("entry-hit")
                return "dispatched"

            def close(self):
                pass

        c = {"open_entry_labels": ["入口A"], "home_labels": ["返回应用"],
             "main_target_match": "x", "entry_delay_sec": 0, "entry_timeout_sec": 5}
        with mock.patch.object(self.d, "_main_target",
                               return_value={"webSocketDebuggerUrl": "ws://x"}), \
                mock.patch("checkin.drivers.client_claim.CDPPage", return_value=_Page()), \
                mock.patch("checkin.drivers.client_claim.time.sleep"):
            self.d._open_entry(9334, c)
        self.assertIn("entry-hit", seen, "回主工作区失败不能阻断入口点击")

    def test_open_entry_is_noop_without_labels(self):
        """没配 open_entry_labels 时必须原地返回、**不连 CDP**。

        否则只读诊断会因为一个空配置去连调试端口，客户端没开时把诊断卡住。
        """
        self.d._open_entry(9334, {})
        self.d._open_entry(9334, {"open_entry_labels": []})
        self.d._open_entry(9334, {"open_entry_labels": None})

    def test_open_entry_needs_main_target_hint(self):
        """没有 main_target_match 就不猜主窗口 —— 猜错会点到别的页面上去。"""
        self.assertIsNone(self.d._main_target(9334, ""))

    def test_open_entry_polls_until_label_appears(self):
        """回归（2026-10-10 真漏签一天）：入口是**异步渲染**的 → 必须轮询到可点为止。

        当天实测：点『查看我的用量』成功后，面板底部的礼物图标要 **3 秒**才渲染出来，
        而当时实现是"盲等 `entry_delay_sec`(1.5s) 后只试一次" ⇒ 第二个入口 `no-button`
        ⇒ 整个站点报 `no_action`，**当天 100 Credits 没领到**。
        这条用假页面复现"前两次 `no-button`、第三次才 `dispatched`"：断言它等到了。
        """
        calls = {"n": 0}

        class _Page:
            def evaluate(self, js, await_promise=False):
                calls["n"] += 1
                return "no-button" if calls["n"] < 3 else "dispatched"

            def close(self):
                pass

        c = {"open_entry_labels": ["A"], "main_target_match": "x",
             "entry_delay_sec": 0, "entry_timeout_sec": 10}
        with mock.patch.object(self.d, "_main_target",
                               return_value={"webSocketDebuggerUrl": "ws://x"}), \
                mock.patch("checkin.drivers.client_claim.CDPPage", return_value=_Page()), \
                mock.patch("checkin.drivers.client_claim.time.sleep"):
            self.d._open_entry(9334, c)
        self.assertGreaterEqual(calls["n"], 3, "入口出现前就放弃了 —— 又回到盲等逻辑")

    def test_open_entry_gives_up_after_budget(self):
        """入口一直不出现时，按 `entry_timeout_sec` 收手 —— 不能无限轮询把任务挂死。"""
        calls = {"n": 0}

        class _Page:
            def evaluate(self, js, await_promise=False):
                calls["n"] += 1
                return "no-button"

            def close(self):
                pass

        c = {"open_entry_labels": ["A", "B"], "main_target_match": "x",
             "entry_delay_sec": 0, "entry_timeout_sec": 0}      # 预算 0 ⇒ 每个入口试一次
        with mock.patch.object(self.d, "_main_target",
                               return_value={"webSocketDebuggerUrl": "ws://x"}), \
                mock.patch("checkin.drivers.client_claim.CDPPage", return_value=_Page()):
            self.d._open_entry(9334, c)
        self.assertEqual(calls["n"], 2, "每个入口应各试一次就收手")

    def test_entry_timing_cfg_tolerates_bad_values(self):
        """配方是人写的 YAML，超时字段写成 `1,5`/`"3s"`/留空都不该炸掉领取流程。"""
        from checkin.drivers.client_claim import _float_cfg
        self.assertEqual(_float_cfg({"a": "1,5"}, "a", 2.0), 2.0)
        self.assertEqual(_float_cfg({"a": "3s"}, "a", 2.0), 2.0)
        self.assertEqual(_float_cfg({}, "a", 2.0), 2.0)
        self.assertEqual(_float_cfg({"a": 3}, "a", 2.0), 3.0)

    # -------------------------- 「签到 UI 在主窗口内」的站点（2026-10-12 接入 Trae）

    @staticmethod
    def _main_ui_recipe(**client_over):
        client = {"debug_port": 9338, "process_match": "x.exe", "wait_port_sec": 0,
                  "claim_in_main_window": True, "open_entry_labels": ["账户"],
                  "main_target_match": "workbench.html", "entry_delay_sec": 0,
                  "entry_timeout_sec": 0}
        client.update(client_over)
        return Recipe.from_dict({"id": "trae", "name": "t", "enabled": True, "mode": "client",
                                 "origin": "https://x", "client": client})

    def test_main_window_ui_expands_entry_and_never_reloads(self):
        """回归（2026-10-12 接入 Trae CN）：签到 UI 在**客户端主窗口**里（账号菜单浮层）
        的站点必须满足两条，缺一条就废：

          · 找按钮**之前**先点入口展开 —— 否则按钮永远藏在浮层后面，`found` 恒为 False，
            站点会稳定报 no_action（而且看起来"配置都对"）；
          · **绝不重载**主窗口 —— 主窗口就是用户的工作区，重载会打断手头的活
            （VSCode 系重载会重建整个 workbench）。这类站点也没有 Qoder 那种
            「跨天存活的陈旧快照」问题需要靠重载来证伪。
        """
        calls = {"entry": 0, "reload": 0}

        class _Page:
            def evaluate(self, js, await_promise=False):
                # find 探测：返回"没找到按钮"，逼出复核分支
                return {"found": False, "ready": True, "body": "菜单里没有签到项"}

            def reload(self, wait):
                calls["reload"] += 1
                return True

            def close(self):
                pass

        r = self._main_ui_recipe()
        with mock.patch.object(self.d, "_wait_target",
                               return_value={"webSocketDebuggerUrl": "ws://x"}), \
                mock.patch.object(self.d, "_open_entry",
                                  side_effect=lambda *a: calls.__setitem__("entry", calls["entry"] + 1)), \
                mock.patch("checkin.drivers.client_claim.CDPPage", return_value=_Page()), \
                mock.patch("checkin.drivers.client_claim.time.sleep"):
            self.d._claim(r, dict(r.client), 9338, "workbench.html")

        self.assertEqual(calls["entry"], 1, "target 已命中也要先展开入口，否则够不到按钮")
        self.assertEqual(calls["reload"], 0, "绝不重载主窗口 —— 那是用户的工作区")

    def test_non_main_window_site_still_rechecks_stale(self):
        """对照：**没开** `claim_in_main_window` 的站点（Qoder）必须维持原行为 ——
        找不到按钮时仍走「重载复核」，不能因为新增开关而丢了这条 2026-10-06 的教训。
        """
        calls = {"reload": 0}

        class _Page:
            def evaluate(self, js, await_promise=False):
                return {"found": False, "ready": True, "body": "已领取"}

            def reload(self, wait):
                calls["reload"] += 1
                return True

            def close(self):
                pass

        r = Recipe.from_dict({"id": "q", "name": "q", "enabled": True, "mode": "client",
                              "origin": "https://x",
                              "client": {"debug_port": 9337, "process_match": "x.exe",
                                         "target_match": "growth-page", "entry_delay_sec": 0,
                                         "entry_timeout_sec": 0}})
        with mock.patch.object(self.d, "_wait_target",
                               return_value={"webSocketDebuggerUrl": "ws://x"}), \
                mock.patch("checkin.drivers.client_claim.CDPPage", return_value=_Page()), \
                mock.patch("checkin.drivers.client_claim.time.sleep"):
            self.d._claim(r, dict(r.client), 9337, "growth-page")

        self.assertGreater(calls["reload"], 0,
                           "非主窗口站点丢了陈旧快照复核 ⇒ 会跨天误判 already、静默漏签")

    def test_qoder_recipe_declares_entry_labels(self):
        """回归（2026-09-26 真踩到，当天没领到）：客户端整天常开时，
        活动入口**不会随开窗自动出现**（判定发生在启动那一刻）。

        配方必须声明"点哪些入口把它调出来"，否则"常开 + 端口已开"这条
        最优路径（②分支）永远不会真正领到东西，而且报的是误导性的 no_action。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        from checkin.core.config import load_config
        _, recipes = load_config(root)
        r = next((x for x in recipes if x.id == "qoder"), None)
        self.assertIsNotNone(r, "找不到 qoder 配方")
        c = r.client or {}
        self.assertTrue(c.get("open_entry_labels"),
                        "缺少 open_entry_labels，常开场景无法领取")
        self.assertTrue(c.get("main_target_match"), "缺少 main_target_match")

    def test_driver_for_resolves_client(self):
        from checkin.drivers import driver_for
        drv = driver_for("client")
        self.assertIsNotNone(drv)
        self.assertEqual(drv.mode, "client")


# ----------------------------------------------------------------------
# 长驻浏览器的"后台冻结"（2026-09-27 真踩到：WorkBuddy 连报 error 的真凶）
# ----------------------------------------------------------------------

class TestBrowserPageFreeze(unittest.TestCase):
    """背景：Chromium 会把长时间处于后台/隐藏的标签页**冻结**（Page Lifecycle → frozen）。
    冻结后页面的 Task Queue 被挂起：
      - 同步的 `Runtime.evaluate`（`1+1`、`document.visibilityState`）**照常返回**；
      - 任何 `await` 的 Promise（页面里的 `fetch`）**永远不 resolve**。
    CDP 侧看到的就是"WebSocket 读超时"，极易被误判成网络故障 —— 而它其实是
    环境状态问题，重试一万次也不会好。

    本项目的专用浏览器是**长驻**的（为保住登录态、避免每天弹窗），所以它必然
    长期待在后台 —— 只要复用它，签到就会超时。2026-09-26 10:27 启动的那个实例，
    到 09-27 已冻了约 25 小时，当天两次运行（11:21、12:45）全部 error。
    活体实测：解冻后同一个 fetch 从"超时"变成 **0.1s 返回 200**。
    """

    def _fake_page(self, recv_side_effect):
        from checkin.browser.cdp import CDPPage
        p = object.__new__(CDPPage)          # 不走 __init__，不真连 WebSocket
        p._id = 0
        ws = mock.MagicMock()
        ws.recv.side_effect = recv_side_effect
        p._ws = ws
        return p, ws

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _cfg(self, chrome=None):
        return AppConfig(root=".", chrome=chrome or ChromeConfig(),
                         safety=SafetyConfig(),
                         state_path=os.path.join(self.tmp.name, "state.json"),
                         log_dir=os.path.join(self.tmp.name, "logs"))

    @staticmethod
    def _sent(ws):
        return [json.loads(c[0][0]) for c in ws.send.call_args_list]

    def test_wake_sends_lifecycle_active(self):
        p, ws = self._fake_page([json.dumps({"id": 1, "result": {}})])
        p.wake()
        calls = self._sent(ws)
        self.assertEqual(calls[0]["method"], "Page.setWebLifecycleState")
        self.assertEqual(calls[0]["params"], {"state": "active"})

    def test_wake_falls_back_to_bring_to_front(self):
        """`setWebLifecycleState` 不被支持时退回 bringToFront，别让整轮签到就此失败。"""
        p, ws = self._fake_page([
            json.dumps({"id": 1, "error": {"message": "not supported"}}),
            json.dumps({"id": 2, "result": {}}),
        ])
        p.wake()
        self.assertEqual([c["method"] for c in self._sent(ws)],
                         ["Page.setWebLifecycleState", "Page.bringToFront"])

    def test_wake_never_raises(self):
        """唤醒只是"尽力而为"：两条路都失败也必须静默返回，
        由上层超时/重试兜底 —— 绝不能把异常泄进驱动主流程。"""
        p, ws = self._fake_page(RuntimeError("boom"))
        p.wake()                              # 不抛即通过

    def test_driver_wakes_before_evaluating(self):
        """回归守卫（守的是**顺序**）。

        顺序错了照样"能跑"，测试也照样绿 —— 只有真机上复用冻结实例时才会
        静默复原成"每天超时"。所以必须把调用顺序钉死。
        """
        from checkin.core.models import Outcome, Session
        from checkin.drivers import browser_page

        page = mock.MagicMock()
        page.current_url.return_value = "https://www.codebuddy.cn/home/"
        page.evaluate.return_value = {"skippedBecauseCheckedIn": True}
        r = Recipe(id="wb", name="wb", mode="auto",
                   session=Session(start_url="https://www.codebuddy.cn/home/",
                                   tab_match=["codebuddy.cn"]))
        cfg = self._cfg()
        with mock.patch.object(browser_page.launcher, "ensure_chrome"), \
             mock.patch.object(browser_page.cdp.CDPPage, "connect", return_value=page):
            res = browser_page.BrowserPageDriver().run(r, cfg)

        self.assertEqual([c[0] for c in page.method_calls],
                         ["wake", "current_url", "evaluate", "close"],
                         "wake 必须在 evaluate 之前调用")
        self.assertEqual(res.outcome, Outcome.ALREADY)

    def test_launched_browser_suppresses_backgrounding(self):
        """新启动的浏览器要带"别把窗口/渲染进程降级到后台"的开关，从源头避免冻结。

        注意：这两个开关**只在启动那一刻**生效；复用已在跑的实例只能靠 wake()。
        """
        from checkin.browser import launcher

        seen = {}

        def fake_popen(args, **kw):
            seen["args"] = list(args)
            return mock.MagicMock()

        cfg = self._cfg(chrome=ChromeConfig(
            remote_debugging_port=9333, user_data_dir=self.tmp.name,
            executable="", startup_timeout_sec=5))
        with mock.patch.object(launcher, "port_ready", side_effect=[False, True]), \
             mock.patch.object(launcher, "find_chrome", return_value=r"C:\fake\msedge.exe"), \
             mock.patch.object(launcher.subprocess, "Popen", side_effect=fake_popen):
            launcher.ensure_chrome(cfg, "https://www.codebuddy.cn/home/")

        self.assertIn("--disable-renderer-backgrounding", seen["args"])
        self.assertIn("--disable-backgrounding-occluded-windows", seen["args"])


class TestBrowserPageNeedLoginNotify(unittest.TestCase):
    """背景（2026-09-28 实测，代价是**漏签半天**）：

    `need_login` 原先**只写日志**，界面上毫无动静 —— 当天 10:51 就已判定未登录，
    用户直到 12:29 自己来问才发现。而登录态是会自然失效的（实测 `session` /
    `session_2` cookie 有效期都是 168 小时 = 7 天，到期只能人工重登，天天访问
    页面也不能无限续期），所以这条路径**注定会被走到**。

    这里守两件事：
      ① need_login 必须发出提醒；
      ② already / success **绝不能**发 —— 通知一旦天天弹就没人看了，
         稀缺性本身就是通知有效性的一部分。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, evaluate_result, probe=False, reminder=None):
        from checkin.core.models import Action, Recipe, Session
        from checkin.drivers import browser_page

        page = mock.MagicMock()
        page.current_url.return_value = "https://www.codebuddy.cn/home/"
        page.evaluate.return_value = evaluate_result
        r = Recipe(id="wb", name="wb", mode="auto",
                   session=Session(start_url="https://www.codebuddy.cn/home/",
                                   tab_match=["codebuddy.cn"]),
                   actions={"status": Action("POST", "/x", {}),
                            "trigger": Action("POST", "/y", {})},
                   reminder=reminder or {})
        cfg = AppConfig(root=".", chrome=ChromeConfig(), safety=SafetyConfig(),
                        state_path=os.path.join(self.tmp.name, "state.json"),
                        log_dir=os.path.join(self.tmp.name, "logs"))
        sent = []
        with mock.patch.object(browser_page.launcher, "ensure_chrome"), \
             mock.patch.object(browser_page.cdp.CDPPage, "connect", return_value=page), \
             mock.patch.object(browser_page.notify, "_toast",
                               side_effect=lambda t, b: sent.append((t, b))):
            res = browser_page.BrowserPageDriver().run(r, cfg, probe=probe)
        return res, sent

    def test_trigger_401_notifies(self):
        res, sent = self._run({"statusCall": {"http": 401, "code": None},
                               "triggerCall": {"http": 401, "code": None}})
        self.assertEqual(res.outcome, Outcome.NEED_LOGIN)
        self.assertEqual(len(sent), 1, "need_login 必须发出一条提醒")

    def test_probe_401_notifies(self):
        """只读探针也要提醒 —— 它正是"登录态还在不在"的日常体检入口。"""
        res, sent = self._run({"statusCall": {"http": 401, "code": None}}, probe=True)
        self.assertEqual(res.outcome, Outcome.NEED_LOGIN)
        self.assertEqual(len(sent), 1)

    def test_recipe_copy_is_used(self):
        """文案走配方，代码里不出现站点名（与 client_claim 同约定）。"""
        _, sent = self._run({"statusCall": {"http": 401, "code": None},
                             "triggerCall": {"http": 401, "code": None}},
                            reminder={"toast_title": "标题A", "toast_body": "正文B"})
        self.assertEqual(sent[0], ("标题A", "正文B"))

    def test_default_copy_when_recipe_silent(self):
        """配方没写 toast 文案时也要能出声。

        否则新增一个站点、照抄配方却漏了 `reminder` 段，提醒就会静默消失 ——
        而那正是本次要杜绝的失败模式。兜底文案由代码给（带站点名）。
        """
        _, sent = self._run({"statusCall": {"http": 401, "code": None},
                             "triggerCall": {"http": 401, "code": None}})
        self.assertEqual(len(sent), 1)
        self.assertIn("wb", sent[0][0], "兜底文案应带上站点名")

    def test_no_notify_on_already(self):
        """已签不该弹窗（通知的稀缺性 = 通知的有效性）。"""
        res, sent = self._run({"skippedBecauseCheckedIn": True})
        self.assertEqual(res.outcome, Outcome.ALREADY)
        self.assertEqual(sent, [])


# ----------------------------------------------------------------------
# 引擎闸门（只读，不碰网络/浏览器）
# ----------------------------------------------------------------------

class TestEngineGates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.cfg = AppConfig(
            root=".", chrome=ChromeConfig(),
            # pre_delay 归零：这些用例会真的走到驱动，不该为"拟人延迟"白等几秒
            safety=SafetyConfig(max_retries=0, pre_delay_range=[0.0, 0.0]),
            state_path=self.state_path, log_dir=os.path.join(self.tmp.name, "logs"))
        # manual 驱动会真弹 Windows toast —— 测试里不该往屏幕上扔通知
        patcher = mock.patch("checkin.drivers.reminder.notify._toast")
        self.addCleanup(patcher.stop)
        self.toast = patcher.start()

    def _manual(self, sid="q"):
        return Recipe(id=sid, name=sid, mode="manual", reminder={"toast_title": "t"})

    def test_no_targets_returns_empty(self):
        self.assertEqual(Engine(self.cfg, []).run(dry_run=True), [])

    def test_disabled_recipe_is_excluded(self):
        r = self._manual(); r.enabled = False
        self.assertEqual(Engine(self.cfg, [r]).run(dry_run=True), [])

    def test_only_filter(self):
        engine = Engine(self.cfg, [self._manual("a"), self._manual("b")])
        res = engine.run(only=["b"], dry_run=True)
        self.assertEqual([x.site_id for x in res], ["b"])

    def test_unknown_mode_is_no_action(self):
        r = Recipe(id="weird", name="weird", mode="does-not-exist")
        res = Engine(self.cfg, [r]).run(dry_run=True)
        self.assertIs(res[0].outcome, Outcome.NO_ACTION)

    def test_human_like_delay_only_on_real_runs(self):
        """拟人延迟**只**在真跑时施加。

        这个条件被写反过一次（`if not idle` ⇒ 真跑反而跳过延迟、dry-run 白等）：
        真跑没有延迟 = 在最该伪装的地方留下了最像机器的心跳；而 dry-run 睡几秒
        纯属浪费。双坏，且都不报错 —— 所以值得单独锁住。
        """
        cases = [(dict(now=True), 1), (dict(probe=True), 0), (dict(dry_run=True), 0)]
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                with mock.patch("checkin.core.engine.jitter.sleep_range",
                                return_value=0.0) as sampled:
                    Engine(self.cfg, [self._manual()]).run(**kwargs)
                self.assertEqual(sampled.call_count, expected,
                                 f"{kwargs} 下延迟采样次数应为 {expected}")

    def test_now_skips_the_window_wait(self):
        """`--now`（手动补跑）不得再等窗口内的随机时刻。

        注意：计划任务**不再**用 `--now`（2026-10-10 回退，见
        TestTaskInstall.test_action_does_not_pass_now）——
        它现在只服务"人工立刻补跑"这一种场景。
        """
        with mock.patch("checkin.core.engine.scheduler.plan_today") as plan, \
                mock.patch("checkin.core.engine.scheduler.wait_until") as wait:
            Engine(self.cfg, [self._manual()]).run(now=True)
        plan.assert_not_called()
        wait.assert_not_called()

    def test_day_skip_applies_even_when_now(self):
        """今日跳过策略（周末等）与"等不等窗口"无关 —— `--now` 也拦。

        这条是**防回归**：原先 `should_skip_today` 被关在 `if not now` 里面，
        一旦有入口以 `--now` 调用（当时是计划任务），`skip_weekends` 就会被
        静默绕过 —— "配了却不起作用"的隐形故障，本项目最忌讳的那类。
        """
        with mock.patch("checkin.core.engine.scheduler.should_skip_today",
                        return_value="周末不执行（skip_weekends=true）"):
            res = Engine(self.cfg, [self._manual()]).run(now=True)
        self.assertIs(res[0].outcome, Outcome.SKIPPED)
        self.assertIn("周末不执行", res[0].message)   # 跳过原因要带出来，不能只报"跳过了"
        self.toast.assert_not_called()          # 跳过了就不该再提醒

    def test_dry_run_does_not_record_state(self):
        Engine(self.cfg, [self._manual()]).run(dry_run=True)
        self.assertFalse(StateStore(self.state_path).done_today("q"))

    def test_probe_does_not_record_state(self):
        Engine(self.cfg, [self._manual()]).run(probe=True)
        self.assertFalse(StateStore(self.state_path).done_today("q"))

    def test_idempotent_skip_when_done_today(self):
        StateStore(self.state_path).record("q", "success")
        res = Engine(self.cfg, [self._manual()]).run(now=True)
        self.assertIs(res[0].outcome, Outcome.SKIPPED)

    def test_circuit_break_after_repeated_failures(self):
        store = StateStore(self.state_path)
        for _ in range(self.cfg.safety.circuit_break_after):
            store.record("q", "error")
        res = Engine(self.cfg, [self._manual()]).run(now=True)
        self.assertIs(res[0].outcome, Outcome.CIRCUIT)

    def test_write_path_is_guarded_by_run_lock(self):
        """真跑时锁被占用 → 不得对任何站点发起动作。"""
        lock = RunLock(os.path.join(self.tmp.name, "run.lock"))
        self.assertTrue(lock.acquire())
        try:
            res = Engine(self.cfg, [self._manual()]).run(now=True)
        finally:
            lock.release()
        self.assertTrue(all(x.outcome is Outcome.SKIPPED for x in res))
        self.assertIn("另一实例", res[0].message)


# ----------------------------------------------------------------------
# manual 站点的幂等（"已提醒"）与开窗时刻闸门
# ----------------------------------------------------------------------

class TestManualIdempotence(unittest.TestCase):
    """manual 站点没有服务端证据，幂等只能靠"今天是否已提醒过"。

    修的是这个症状：以前同一天每跑一次引擎就弹一次窗（手动补跑、任务重试都会触发），
    而 --status 里它的"今日"永远是 `-`，看不出今天到底提醒过没有。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.cfg = AppConfig(
            root=".", chrome=ChromeConfig(),
            safety=SafetyConfig(max_retries=0, pre_delay_range=[0.0, 0.0]),
            state_path=self.state_path, log_dir=os.path.join(self.tmp.name, "logs"))
        patcher = mock.patch("checkin.drivers.reminder.notify._toast")
        self.addCleanup(patcher.stop)
        self.toast = patcher.start()

    def _manual(self, sid="q", **reminder):
        return Recipe(id=sid, name=sid, mode="manual",
                      reminder={"toast_title": "t", **reminder})

    def _run(self, recipe):
        return Engine(self.cfg, [recipe]).run(now=True)[0]

    # --- 幂等 ---------------------------------------------------------

    def test_first_run_prompts_and_records(self):
        r = self._manual()
        res = self._run(r)
        self.assertIs(res.outcome, Outcome.NO_ACTION)
        self.assertEqual(self.toast.call_count, 1)
        self.assertTrue(StateStore(self.state_path).prompted_today("q"))

    def test_second_run_same_day_is_skipped_and_silent(self):
        self._run(self._manual())
        res = self._run(self._manual())
        self.assertIs(res.outcome, Outcome.SKIPPED)
        self.assertIn("已提醒", res.message)
        self.assertEqual(self.toast.call_count, 1, "同一天不应重复弹窗")

    def test_prompt_does_not_inflate_streak(self):
        self._run(self._manual())
        self._run(self._manual())
        self.assertEqual(StateStore(self.state_path).streak("q"), 0,
                         "manual 站点没有服务端证据，不该出现在连续天数里")

    def test_probe_bypasses_the_prompt_gate(self):
        """诊断永远要能跑，不能被"今天提醒过了"挡住 —— 否则没法复查。"""
        self._run(self._manual())
        res = Engine(self.cfg, [self._manual()]).run(probe=True)[0]
        self.assertIsNot(res.outcome, Outcome.SKIPPED)

    def test_dry_run_bypasses_the_prompt_gate_and_does_not_record(self):
        self._run(self._manual())
        res = Engine(self.cfg, [self._manual()]).run(dry_run=True)[0]
        self.assertIsNot(res.outcome, Outcome.SKIPPED)

    def test_gate_can_be_switched_off_by_config(self):
        self.cfg.safety.skip_when_done_today = False
        self._run(self._manual())
        self.assertEqual(self.toast.call_count, 1)
        self._run(self._manual())
        self.assertEqual(self.toast.call_count, 2, "关掉幂等后应放行")

    # --- 开窗时刻闸门 -------------------------------------------------

    def test_not_before_blocks_early_and_does_not_burn_the_prompt(self):
        """开窗前不提醒，且**不消耗**当天的提醒额度 —— 否则开窗后就再也不会提醒了。"""
        now = datetime.now(CST)
        gate_dt = now + timedelta(minutes=2)
        if gate_dt.day != now.day:
            self.skipTest("临近午夜，构造不出'今天稍后'的时刻")
        r = self._manual(not_before=gate_dt.strftime("%H:%M"))

        res = self._run(r)
        self.assertIs(res.outcome, Outcome.SKIPPED)
        self.assertIn("开窗时刻", res.message)
        self.assertEqual(self.toast.call_count, 0)
        self.assertFalse(StateStore(self.state_path).prompted_today("q"),
                         "被时间闸门挡下时不能记'已提醒'")

    def test_not_before_passes_after_the_hour(self):
        res = self._run(self._manual(not_before="00:00"))
        self.assertIs(res.outcome, Outcome.NO_ACTION)
        self.assertEqual(self.toast.call_count, 1)

    def test_time_gate_is_bypassed_by_probe(self):
        r = self._manual(not_before="23:59")
        res = Engine(self.cfg, [r]).run(probe=True)[0]
        self.assertIsNot(res.outcome, Outcome.SKIPPED)

    def test_unparsable_not_before_fails_open(self):
        """格式写错时宁可多发一次提醒，也不静默吞掉（"没提醒"用户察觉不到）。"""
        res = self._run(self._manual(not_before="十点"))
        self.assertIs(res.outcome, Outcome.NO_ACTION)
        self.assertEqual(self.toast.call_count, 1)

    def test_absent_or_blank_not_before_is_no_gate(self):
        """没声明 / 空字符串 / 纯空白 都等于"没有闸门" —— 且不该打格式告警。"""
        for i, value in enumerate((None, "", "   ")):
            with self.subTest(value=value):
                sid = f"q{i}"
                extra = {} if value is None else {"not_before": value}
                res = self._run(self._manual(sid, **extra))
                self.assertIs(res.outcome, Outcome.NO_ACTION)


# ----------------------------------------------------------------------
# 通知语义：它只回答一个问题 —— "今天有没有需要我处理的？"
# ----------------------------------------------------------------------

class TestNotify(unittest.TestCase):
    """回归来源（2026-09-24 修）：完成率的分母曾包进 manual 站点，而 manual 永远返回
    `no_action` —— 于是每天都弹「每日签到完成 1/2 个站点已处理」，看着像永远失败一半；
    更糟的是**真出错时标题照样写"完成"**。会误导人的告警比没有告警更糟，所以钉死。"""

    def setUp(self):
        # 测试不该往屏幕上扔东西：toast 是真弹窗，print 是真输出
        toast = mock.patch("checkin.core.notify._toast")
        self.toast = toast.start()
        self.addCleanup(toast.stop)
        printer = mock.patch("builtins.print")
        self.addCleanup(printer.stop)
        printer.start()

    def _r(self, site, outcome, msg=""):
        return CheckinResult(site, outcome, msg)

    def _sent(self):
        self.assertTrue(self.toast.called, "应当发出一条通知")
        return self.toast.call_args[0][:2]          # (title, body)

    # --- 分类的完整性 -------------------------------------------------

    def test_every_outcome_is_classified(self):
        """两个桶 + "其余" 必须**穷尽且互斥**地覆盖 Outcome。

        将来加了新 Outcome 却忘了归类，它会静默落进"其余"桶 —— 既不报错也不告警，
        正是最难发现的那类错误。所以把"必须显式归类"变成可检测的契约。
        """
        problem, done = set(notify._PROBLEM), set(notify._DONE)
        self.assertFalse(problem & done, "同一个 Outcome 不能既算问题又算完成")
        self.assertEqual(set(Outcome) - problem - done,
                         {Outcome.NO_ACTION, Outcome.SKIPPED},
                         "新增 Outcome 后必须显式决定它落在哪个桶")

    def test_login_bucket_is_a_subset_of_problem(self):
        """`_LOGIN_NEEDED` 必须是 `_PROBLEM` 的子集 —— 两个常量不能各写一份集合。"""
        self.assertTrue(set(notify._LOGIN_NEEDED) <= set(notify._PROBLEM))

    # --- 正常路径：不该报警 -------------------------------------------

    def test_manual_site_is_not_counted_as_failure(self):
        notify.notify_results([
            self._r("workbuddy", Outcome.ALREADY),
            self._r("qoder", Outcome.NO_ACTION, "已发送签到提醒（人工确认）"),
        ])
        title, body = self._sent()
        self.assertEqual(title, "每日签到完成")
        self.assertNotIn("/", body, "不该再出现 N/M 这种会误导人的比例")
        self.assertIn("workbuddy", body)
        self.assertIn("qoder", body)

    def test_all_skipped_says_nothing_to_do(self):
        notify.notify_results([self._r("workbuddy", Outcome.SKIPPED, "今日已完成")])
        title, body = self._sent()
        self.assertEqual(title, "每日签到完成")
        self.assertIn("无需动作", body)

    def test_empty_results_sends_nothing(self):
        notify.notify_results([])
        self.toast.assert_not_called()

    # --- 出问题的路径：标题必须变 -------------------------------------

    def test_failure_changes_the_title(self):
        """真出错时不能再报"完成" —— 看到"完成"人就不会去查。"""
        notify.notify_results([self._r("workbuddy", Outcome.ERROR, "网络错误")])
        title, body = self._sent()
        self.assertNotIn("完成", title)
        self.assertIn("workbuddy", body)

    def test_circuit_break_counts_as_problem(self):
        notify.notify_results([self._r("workbuddy", Outcome.CIRCUIT, "连续失败 3 次")])
        title, _ = self._sent()
        self.assertNotIn("完成", title)

    def test_need_login_tells_you_how_to_fix(self):
        """报错要带可执行的下一步，不能只甩一个 401。"""
        notify.notify_results([self._r("workbuddy", Outcome.NEED_LOGIN, "http=401")])
        title, body = self._sent()
        self.assertNotIn("完成", title)
        self.assertIn("--login", body)

    def test_problem_wins_over_success(self):
        """一个站点成功、另一个失败 —— 不能因为"有成功的"就报完成。"""
        notify.notify_results([
            self._r("workbuddy", Outcome.SUCCESS),
            self._r("other", Outcome.ERROR, "boom"),
        ])
        title, _ = self._sent()
        self.assertNotIn("完成", title)

    # --- QQ 推送（2026-10-14 新增） ------------------------------------
    # 为什么是签到程序自己推、而不是走 WorkBuddy 的通知体系，见 notify.py 顶部长注释。

    def _qq_root(self, payload=None):
        """造一个临时 root，按需写入 data/notify.json。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        if payload is not None:
            os.makedirs(os.path.join(root, "data"), exist_ok=True)
            with open(os.path.join(root, "data", "notify.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
        return root

    def test_qq_skipped_when_not_configured(self):
        """没配 `data/notify.json` = 未开启 ⇒ 静默跳过，对没配的人零影响。"""
        root = self._qq_root()
        with mock.patch.object(notify, "_http_post_json") as post:
            notify.notify_results([self._r("workbuddy", Outcome.SUCCESS)], root=root)
        post.assert_not_called()

    def test_qq_skipped_when_root_is_none(self):
        """老调用方（root 缺省 None）不得因为 QQ 推送而改变行为。"""
        with mock.patch.object(notify, "_http_post_json") as post:
            notify.notify_results([self._r("workbuddy", Outcome.SUCCESS)])
        post.assert_not_called()

    def test_qq_incomplete_config_is_rejected_loudly(self):
        """字段不全时必须**记日志并跳过** —— 静默会让用户以为"配好了但没收到"。"""
        root = self._qq_root({"qq_bot": {"app_id": "1", "app_secret": "s"}})
        with self.assertLogs("checkin.notify", level="WARNING") as cm:
            self.assertIsNone(notify._load_qq_cfg(root))
        self.assertIn("group_openid", "\n".join(cm.output))

    def test_qq_pushes_group_message_with_expected_shape(self):
        """配置齐全时，必须真的用 group_openid 调群消息接口，且带对鉴权头。"""
        root = self._qq_root({"qq_bot": {"app_id": "102", "app_secret": "sec",
                                         "group_openid": "GOPEN"}})
        calls = []

        def fake_post(url, payload, headers=None, timeout=15):
            calls.append((url, payload, headers or {}))
            if "getAppAccessToken" in url:
                return {"access_token": "TOK", "expires_in": 7200}
            return {"id": "msg_1"}

        with mock.patch.object(notify, "_http_post_json", side_effect=fake_post):
            notify.notify_results([self._r("workbuddy", Outcome.SUCCESS),
                                   self._r("trae", Outcome.SUCCESS)], root=root)

        self.assertEqual(len(calls), 2, "先换 token，再发消息")
        send_url, send_body, send_headers = calls[1]
        self.assertIn("/v2/groups/GOPEN/messages", send_url)
        self.assertEqual(send_body.get("msg_type"), 0)
        self.assertEqual(send_headers.get("Authorization"), "QQBot TOK")
        self.assertEqual(send_headers.get("X-Union-Appid"), "102")
        self.assertIn("workbuddy", send_body["content"])

    def test_qq_failure_never_breaks_checkin(self):
        """通知失败**绝不能**影响签到本身 —— 抛出去会让整轮跑挂。"""
        root = self._qq_root({"qq_bot": {"app_id": "1", "app_secret": "s",
                                         "group_openid": "G"}})
        with mock.patch.object(notify, "_http_post_json",
                               side_effect=RuntimeError("network down")):
            notify.notify_results([self._r("workbuddy", Outcome.SUCCESS)], root=root)
        # 没有异常穿出去即为通过；toast 仍应照发
        title, _ = self._sent()
        self.assertEqual(title, "每日签到完成")

    def test_qq_message_has_no_emoji(self):
        """消息必须纯文本 —— 经过 GBK 环境（Windows 控制台/日志）时 emoji 会 UnicodeEncodeError。"""
        text = notify._qq_message(
            [self._r("workbuddy", Outcome.SUCCESS)], [])
        self.assertTrue(all(ord(c) < 0x10000 for c in text))
        self.assertNotIn("✅", text)
        self.assertNotIn("❌", text)
        self.assertIn("workbuddy", text)

    def test_qq_message_switches_title_when_problem(self):
        """出错时标题必须变 —— 与桌面通知同一条原则：报"完成"会让人跳过检查。"""
        err = self._r("trae", Outcome.ERROR, "boom")
        text = notify._qq_message([err], [err])
        self.assertIn("需处理", text)
        self.assertNotIn("每日签到", text)


class TestMissedDays(unittest.TestCase):
    """回归来源（2026-10-17，用户原话"还要我来手工检查"）：

    昨天整天没签到这件事，此前**不会通知任何人** —— 计划任务被判 missed、程序卡在
    某个守候分支、机器没开机……任何一种情况下，第二天程序照常跑、照常报"今日已完成"，
    昨天那个洞**永远不会被提起**。用户唯一的发现途径是自己翻日志。
    这一组守卫钉死"必须回头看一眼、并把缺口说出来"。

    ⚠ 缺口必须在**引擎跑之前**取（`__main__.main` 里 `_collect_gaps` 的位置）：
    今天一旦签上，`last_done_date` 就变成今天，那时再回溯只会得到"没有缺口"。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")
        self.store = StateStore(self.path)

    def _last(self, days_ago):
        """把"上次完成日"伪造成 N 天前（0 = 今天）。"""
        day = (datetime.now(CST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        self.store._site("s").update({"last_done_date": day})
        self.store._save()

    def _day(self, days_ago):
        return (datetime.now(CST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    def test_yesterday_miss_is_detected(self):
        self._last(2)
        self.assertEqual(self.store.missed_days("s"), [self._day(1)])

    def test_no_gap_when_signed_in_yesterday(self):
        self._last(1)
        self.assertEqual(self.store.missed_days("s"), [])

    def test_no_gap_when_already_signed_in_today(self):
        self._last(0)
        self.assertEqual(self.store.missed_days("s"), [])

    def test_never_succeeded_is_not_a_gap(self):
        """从未成功过 ⇒ 无从判断"漏了哪天"，不能凭空报一串日期。"""
        self.assertEqual(self.store.missed_days("s"), [])

    def test_multi_day_gap_lists_every_day(self):
        self._last(4)
        days = self.store.missed_days("s")
        self.assertEqual(len(days), 3)
        self.assertEqual(days[0], self._day(3))
        self.assertEqual(days[-1], self._day(1))

    def test_today_is_never_counted_as_missed(self):
        """今天还没跑完 —— 把今天算进去，等于在结果出来之前就判它失败。"""
        self._last(3)
        self.assertNotIn(today_key(), self.store.missed_days("s"))

    def test_weekends_are_not_counted_when_skipped(self):
        """skip_weekends 配了的话，周末本来就不该跑，报出来是噪音。"""
        self._last(9)
        all_days = self.store.missed_days("s")
        work = self.store.missed_days("s", skip_weekends=True)
        weekend = [d for d in all_days
                   if datetime.strptime(d, "%Y-%m-%d").date().weekday() >= 5]
        self.assertTrue(weekend, "9 天的缺口里必然含周末，否则这条守卫测不到东西")
        self.assertEqual(set(all_days) - set(work), set(weekend))

    def test_gap_is_capped(self):
        """只是防止状态库被改坏后刷出一屏日期，正常路径碰不到。"""
        self._last(400)
        self.assertLessEqual(len(self.store.missed_days("s")), 14)

    def test_unparsable_date_is_ignored(self):
        self.store._site("s").update({"last_done_date": "not-a-date"})
        self.store._save()
        self.assertEqual(self.store.missed_days("s"), [])

    # --- 播报戳记：一天只说一次 -----------------------------------------

    def test_gap_notice_is_empty_initially(self):
        self.assertEqual(self.store.gap_notice_today(), "")

    def test_gap_notice_is_remembered_for_today(self):
        self.store.set_gap_notice("qoder 昨日漏签")
        self.assertEqual(self.store.gap_notice_today(), "qoder 昨日漏签")

    def test_gap_notice_expires_the_next_day(self):
        """过期必须靠**日期**判断 —— 只看"有没有值"会让它变成一次性开关。

        ⚠ 这条守卫的第一版是自己改掉存储里的 `date`（等于替 `set_gap_notice`
        把活干了），于是"戳记根本没写日期"这种缺陷它**照样是绿的** ——
        典型的"别 mock 掉你要验证的那道约束"。现在改成只改「今天」的口径，
        让真实写入路径自己跑。
        """
        self.store.set_gap_notice("qoder 昨日漏签")
        self.assertNotEqual(self.store.gap_notice_today(), "",
                            "同一天必须认 —— 不认的话缺口会被反复重报")
        with mock.patch("checkin.core.state.today_key", return_value=self._day(-1)):
            self.assertEqual(self.store.gap_notice_today(), "",
                             "换了一天就必须失效 —— 否则以后每天的缺口都报不出来")

    def test_gap_notice_survives_a_reload(self):
        """戳记必须落在文件里，不是内存里 —— 一天三次触发是三个**独立进程**。"""
        self.store.set_gap_notice("qoder 昨日漏签")
        self.assertEqual(StateStore(self.path).gap_notice_today(), "qoder 昨日漏签")


class TestMissedDayNotice(unittest.TestCase):
    """缺口要真的进通知（桌面 + QQ）—— 只在状态里算出来、不说出来，等于没做。"""

    def setUp(self):
        toast = mock.patch("checkin.core.notify._toast")
        self.toast = toast.start()
        self.addCleanup(toast.stop)
        printer = mock.patch("builtins.print")
        self.addCleanup(printer.stop)
        printer.start()

    def _sent(self):
        self.assertTrue(self.toast.called, "应当发出一条通知")
        return self.toast.call_args[0][:2]

    def test_gap_changes_the_title_even_when_today_is_fine(self):
        """今天全部成功、但昨天有洞 ⇒ 标题必须提到"漏"。

        报"每日签到完成"会让人以为连续天数还在 —— 那是静默漏签的另一种形态。
        """
        notify.notify_results([CheckinResult("qoder", Outcome.SUCCESS)],
                              gaps={"qoder": ["2026-10-16"]})
        title, body = self._sent()
        self.assertIn("漏", title)
        self.assertNotIn("完成", title)
        self.assertIn("qoder", body)

    def test_no_gap_keeps_the_plain_title(self):
        """没有缺口时行为必须与改动前一致（老调用方不受影响）。"""
        notify.notify_results([CheckinResult("qoder", Outcome.SUCCESS)])
        title, _ = self._sent()
        self.assertEqual(title, "每日签到完成")

    def test_gap_is_appended_when_today_also_failed(self):
        notify.notify_results([CheckinResult("qoder", Outcome.ERROR, "boom")],
                              gaps={"qoder": ["2026-10-16"]})
        title, body = self._sent()
        self.assertIn("处理不了", title)
        self.assertIn("漏", body)

    def test_gap_reaches_the_qq_message(self):
        text = notify._qq_message([CheckinResult("qoder", Outcome.SUCCESS)], [],
                                  {"qoder": ["2026-10-16"]})
        self.assertIn("漏签", text)

    def test_qq_message_without_gap_is_unchanged(self):
        text = notify._qq_message([CheckinResult("qoder", Outcome.SUCCESS)], [])
        self.assertNotIn("漏", text)

    def test_gap_line_mentions_the_site_and_the_span(self):
        self.assertIn("trae", notify.gap_line({"trae": ["2026-10-16"]}))
        self.assertIn("3", notify.gap_line({"trae": ["a", "b", "c"]}))

    def test_empty_gap_line_is_empty(self):
        self.assertEqual(notify.gap_line(None), "")
        self.assertEqual(notify.gap_line({}), "")

    def test_gap_title_counts_the_days(self):
        """标题是唯一一眼扫得到的东西：漏一天说"昨天"，漏多天直接报天数。"""
        self.assertIn("昨天", notify._gap_title({"qoder": ["a"]}))
        self.assertIn("3", notify._gap_title({"qoder": ["a", "b", "c"]}))


class TestCollectGaps(unittest.TestCase):
    """`__main__._collect_gaps` 的选取口径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.cfg = AppConfig(
            root=self.tmp.name, chrome=ChromeConfig(), safety=SafetyConfig(),
            state_path=self.state_path, log_dir=self.tmp.name,
            schedule=ScheduleConfig())

    def _seed(self, sid, days_ago):
        store = StateStore(self.state_path)
        store._site(sid).update({
            "last_done_date":
                (datetime.now(CST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")})
        store._save()

    def _auto(self, sid="qoder", mode="client"):
        # 用 **client**：真实配方（qoder/trae/workbuddy）就是这个 mode。
        # 测试里图省事写 auto，就会掩盖"生产环境口径不匹配"这类静默失效。
        return Recipe(id=sid, name=sid, mode=mode)

    def test_detects_the_gap(self):
        self._seed("qoder", 3)
        self.assertIn("qoder", _collect_gaps(self.cfg, [self._auto()]) or {})

    def test_manual_sites_are_not_reported(self):
        """manual 站点没有服务端证据，last_done_date 长期不动是设计如此。"""
        self._seed("m", 3)
        self.assertIsNone(_collect_gaps(
            self.cfg, [Recipe(id="m", name="m", mode="manual")]))

    def test_only_filter_is_respected(self):
        self._seed("qoder", 3)
        self._seed("trae", 3)
        gaps = _collect_gaps(self.cfg, [self._auto("qoder"), self._auto("trae")],
                             only=["trae"])
        self.assertEqual(list(gaps or {}), ["trae"])

    def test_disabled_site_is_ignored(self):
        self._seed("qoder", 3)
        r = self._auto()
        r.enabled = False
        self.assertIsNone(_collect_gaps(self.cfg, [r]))

    def test_no_gap_returns_none(self):
        self._seed("qoder", 1)
        self.assertIsNone(_collect_gaps(self.cfg, [self._auto()]))

    def test_reported_only_once_per_day(self):
        """一天三个触发是三个**独立进程** —— 同样的话响三遍，人就开始忽略告警。"""
        self._seed("qoder", 3)
        self.assertIsNotNone(_collect_gaps(self.cfg, [self._auto()]))
        self.assertIsNone(_collect_gaps(self.cfg, [self._auto()]),
                          "同一天第二次运行不该再报一遍同样的缺口")

    def test_nothing_is_reported_on_a_skipped_day(self):
        """今天本来就不该跑（周末）时，别拿昨天的洞刷存在感。"""
        self._seed("qoder", 3)
        with mock.patch("checkin.__main__.scheduler.should_skip_today",
                        return_value="周末不执行"):
            self.assertIsNone(_collect_gaps(self.cfg, [self._auto()]))

    # --- 口径必须按**真实配方**写，不能按测试里的便利写 -----------------

    def test_auto_mode_is_also_covered(self):
        """`auto`（浏览器驱动）同样能领到，同样该报缺口。"""
        self._seed("qoder", 3)
        self.assertIn("qoder", _collect_gaps(self.cfg, [self._auto(mode="auto")]) or {})

    def test_real_project_sites_are_all_reported(self):
        """**直接读真实配方**（2026-10-17 这条差点又是一次静默失效）：

        三个站点的 mode 都是 `client`，而第一版口径写成"只认 auto" ⇒
        生产环境永远不报缺口，单元测试却全绿。这里用真实配方 + 造好的缺口，
        断言**每一个启用中的非 manual 站点都在结果里**。
        """
        cfg, recipes = load_config(str(PROJECT_ROOT))
        cfg.state_path = self.state_path                  # 别碰真实状态库
        enabled = [r for r in recipes if r.enabled and r.mode != "disabled"]
        self.assertTrue(enabled, "真实配方里应当有启用中的站点")
        for r in enabled:
            self._seed(r.id, 3)
        gaps = _collect_gaps(cfg, recipes) or {}
        expected = {r.id for r in enabled if r.mode != "manual"}
        self.assertEqual(set(gaps), expected,
                         "启用的非 manual 站点必须全部参与缺口回溯")

    def test_real_project_modes_are_all_known(self):
        """将来配方里出现新 mode 时，必须**显式决定**它要不要报缺口 ——
        漏判会让它静默落在"不报"那一侧，又是一次看不见的失效。"""
        _, recipes = load_config(str(PROJECT_ROOT))
        for r in recipes:
            self.assertIn(r.mode, {"auto", "client", "manual", "disabled"},
                          f"新 mode={r.mode} 必须显式决定是否参与缺口回溯")


class TestTaskInstall(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = load_config(str(PROJECT_ROOT))

    def test_uses_powershell_cmdlet_not_schtasks(self):
        """本机 schtasks.exe 被程序黑名单封死（连 /Query 都跑不了），
        所以注册必须走 in-process 的 ScheduledTasks cmdlet。"""
        s = _install_script(self.cfg)
        self.assertIn("Register-ScheduledTask", s)
        self.assertNotIn("schtasks", s.lower())

    def test_enables_missed_run_recovery(self):
        """-StartWhenAvailable：休眠/关机错过唤醒时刻后，开机补跑一次。"""
        self.assertIn("-StartWhenAvailable", _install_script(self.cfg))

    def test_survives_battery_and_idle(self):
        """等待窗口内随机时刻可能长达 2 小时，不能被拔电源/空闲结束杀掉。

        注意 `-DontStopOnIdleEnd` 落在 `Settings.IdleSettings.StopOnIdleEnd`，
        **不在 Settings 顶层** —— 去顶层查会读到 $null 并误判成"没生效"（踩过）。
        """
        s = _install_script(self.cfg)
        self.assertIn("-AllowStartIfOnBatteries", s)
        self.assertIn("-DontStopIfGoingOnBatteries", s)
        self.assertIn("-DontStopOnIdleEnd", s)

    def test_sets_working_directory_to_project_root(self):
        """必须显式设工作目录：任务的默认 cwd 在 system32 附近。"""
        from checkin.__main__ import _ROOT
        s = _install_script(self.cfg)
        self.assertIn("-WorkingDirectory", s)
        self.assertIn(_ROOT, s)

    def test_trigger_wakes_before_window_without_random_delay(self):
        """触发 = `-At <窗口开始前 10 分钟>`，**绝不带 `-RandomDelay`**。

        **这是 2026-10-10 漏签事故的回归守卫。**
        10-08 为消灭"长等待"曾改用 `-RandomDelay PT2H25M`，结果 10-10 整整
        漏签一天：当日 occurrence 被调度器静默判为 `missed` 后跳次日
        （`NumberOfMissedRuns=1`；16 秒内连读 8 次 `NextRunTime` 得到 8 个
        不同值且全是次日；当天日志文件根本不存在）。
        微软 KB2956042 的标题就是「使用 RandomDelay 参数的计划任务不会运行」。
        ⇒ 对"绝不能静默漏掉"的每日任务，RandomDelay 不可靠，一律不要。
        """
        s = _install_script(self.cfg)
        self.assertIn(f"-At {_psq(_wake_hint(self.cfg.schedule.window_start))}", s)
        self.assertNotIn("-RandomDelay", s)

    def test_no_random_delay_for_any_window(self):
        """换任何窗口配置都不能引入 RandomDelay —— 这条守卫不看具体值。"""
        for start, end in (("10:05", "12:30"), ("07:40", "11:20"), ("09:00", "09:30")):
            with self.subTest(window=f"{start}-{end}"):
                cfg = _cfg(start, end)
                self.assertIn(f"-At {_psq(_wake_hint(start))}", _install_script(cfg))
                self.assertNotIn("-RandomDelay", _install_script(cfg))

    def test_has_three_triggers_covering_the_whole_day(self):
        """必须有三个触发器：主触发 + 窗口结束后的兜底 + 当夜收尾。

        **三个都有血案在身：**
        · 兜底（t2）—— 2026-10-15 真漏签一天：主触发 09:55 因"开机未登录会话"被静默
          判 missed，程序**根本没启动**（当天连日志文件都没有，`NumberOfMissedRuns=1`）。
          只靠 `-StartWhenAvailable` 救不了未登录场景 ⇒ 必须有第二次机会。
        · 收尾（t3）—— 2026-10-17：前两次都撞上"客户端还在无端口形态"（自升级后自重启
          留下来的），那是**当天不会自愈**的状态；到夜里再试一次才可能自己了结。
        已成功的站点由程序自身幂等拦下，不会重复签到。
        """
        for start, end in (("10:05", "12:30"), ("07:40", "11:20"), ("09:00", "09:30")):
            with self.subTest(window=f"{start}-{end}"):
                s = _install_script(_cfg(start, end))
                self.assertIn(f"$t1=New-ScheduledTaskTrigger -Daily -At {_psq(_wake_hint(start))}", s)
                self.assertIn(f"$t2=New-ScheduledTaskTrigger -Daily -At {_psq(_fallback_hint(end))}", s)
                self.assertIn(f"$t3=New-ScheduledTaskTrigger -Daily -At {_psq(_closing_hint(end))}", s)
                self.assertIn("$t=@($t1,$t2,$t3)", s,
                              "三个触发器必须都注册，漏注册就不会真的生效")

    def test_closing_trigger_lands_in_the_evening(self):
        """收尾触发必须落在傍晚之后 —— 它要的是"距离最后一次失败已经过去很久"。

        窗口设到很晚（>=20 点）时就顺延一小时，绝不跑到窗外去。
        """
        self.assertEqual(_closing_hint("12:30"), "21:00")
        self.assertEqual(_closing_hint("19:00"), "21:00")
        self.assertEqual(_closing_hint("21:30"), "22:30")
        self.assertEqual(_closing_hint("坏值"), "21:00")

    def test_closing_trigger_is_not_before_the_fallback(self):
        """收尾必须严格晚于兜底 —— 顺序反了就失去"最后再给一次机会"的意义。"""

        def _min(s):
            h, m = s.split(":")
            return int(h) * 60 + int(m)

        for end in ("12:30", "11:20", "09:30", "21:30"):
            with self.subTest(window_end=end):
                self.assertGreater(_min(_closing_hint(end)), _min(_fallback_hint(end)))

    def test_print_task_matches_install_script(self):
        """`--print-task` 展示的脚本必须和 `--install-task` 实际注册的一致。

        2026-10-15 踩过：给 install 加了兜底触发器，却漏改 print，于是
        `--print-task` 直接 NameError 挂掉（用户复制到的命令和真正注册的不是一套）。
        """
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_task(self.cfg)
        out = buf.getvalue()
        s = _install_script(self.cfg)
        # 关键片段两边都要有（主唤醒、兜底、收尾时刻）
        for frag in (_wake_hint(self.cfg.schedule.window_start),
                     _fallback_hint(self.cfg.schedule.window_end),
                     _closing_hint(self.cfg.schedule.window_end)):
            self.assertIn(frag, out)
            self.assertIn(frag, s)

    def test_uses_pythonw_to_avoid_console_window(self):
        """解释器必须优先 `pythonw.exe`。

        `python.exe` 是控制台子系统 —— 任务一跑就弹黑框（2026-10-08 用户报障：
        黑框从 09:55 一直挂到 11:28）。`pythonw.exe` 没有控制台，同等逻辑不露脸。
        """
        from checkin.__main__ import _task_python
        exe = _task_python()
        self.assertTrue(exe.lower().endswith("pythonw.exe"),
                        f"计划任务解释器应优先 pythonw.exe，实际是 {exe}")
        self.assertIn("pythonw.exe", _install_script(self.cfg))

    def test_action_does_not_pass_now(self):
        """动作**不能**带 `--now`：随机时刻由程序自己在窗口内采样后等待，
        带 `--now` 会跳过采样 ⇒ 每天固定在唤醒那一刻签到，反封堵的随机性归零。
        （`--now` 本身保留，仍供手动补跑使用，见 CLI 参数集。）"""
        s = _install_script(self.cfg)
        self.assertNotIn("--now", s)
        self.assertNotIn("--now", _task_command())

    def test_runs_the_entry_file_directly(self):
        """必须直跑 __main__.py（自挂 sys.path），不能写 `-m checkin`：
        后者要求 cwd 或 PYTHONPATH 指向 src，任务环境下会 No module named checkin。"""
        s = _install_script(self.cfg)
        self.assertIn("__main__.py", s)
        self.assertNotIn("-m checkin", s)

    def test_ps_quote_doubles_single_quotes(self):
        """PowerShell 单引号字符串靠**双写**转义 —— 路径含单引号时不能漏。"""
        self.assertEqual(_psq("a'b"), "'a''b'")
        self.assertEqual(_psq("plain"), "'plain'")

    def test_uninstall_is_idempotent(self):
        """任务不存在时不该报错（可重复卸载）。"""
        s = _uninstall_script()
        self.assertIn("SilentlyContinue", s)
        self.assertIn("Unregister-ScheduledTask", s)


class TestPythonwHygiene(unittest.TestCase):
    """计划任务改用 `pythonw.exe`（无控制台）后的两处兜底。

    起因（2026-10-08 用户报障）：任务原先用 `python.exe`，每天弹一个黑框，
    还要挂到窗口内随机时刻才动手。改成 pythonw 之后，两件原先"被控制台兜住"的
    事会浮出来，必须显式处理 —— 否则黑框会以另一种方式回来：
      1. pythonw 下 `sys.stdout/stderr` 是 None → print()/StreamHandler 直接抛异常；
      2. 无控制台的父进程派生控制台程序（powershell）→ Windows **新建可见控制台窗口**。
    """

    def test_log_setup_tolerates_missing_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("checkin.core.log.sys.stderr", None):
                logger = log_mod.setup(tmp, "INFO", [])
            try:
                kinds = [type(h) for h in logger.handlers]
                self.assertIn(logging.FileHandler, kinds, "无控制台时仍必须写文件日志")
                self.assertNotIn(logging.StreamHandler, kinds,
                                 "无 stderr 时不该挂 StreamHandler（拿到空流会逐条报错）")
                logger.info("无控制台也要能记一条")      # 不抛异常即通过
            finally:
                # 先 close 再 clear：FileHandler 不 close 会一直占着文件句柄，
                # TemporaryDirectory 清理时在 Windows 上直接 WinError 32。
                for h in logger.handlers:
                    h.close()
                logger.handlers.clear()

    def test_toast_hides_its_console(self):
        seen = {}

        def fake_run(*a, **kw):
            seen.update(kw)
            return mock.Mock(returncode=0)

        with mock.patch("checkin.core.notify.subprocess.run", side_effect=fake_run):
            notify._toast("标题", "正文")
        self.assertEqual(seen.get("creationflags"), procenv.NO_WINDOW,
                         "派生的 powershell 必须 CREATE_NO_WINDOW，否则闪黑框")
        self.assertNotEqual(procenv.NO_WINDOW, 0, "Windows 上 CREATE_NO_WINDOW 应可用")


class TestRedactFilter(unittest.TestCase):
    """凭证脱敏过滤器。

    2026-10-08 补测时炸出一个**潜伏 bug**：`redact_keys` 为空列表时，过滤器会拼出
    一个不含捕获组的退化正则，却仍用 `\\1***` 作替换 —— 每条日志都抛
    `re.error: invalid group reference 1`。默认配置里 keys 非空，所以一直没暴露。
    """

    def _record(self, msg):
        return logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)

    def test_empty_keys_does_not_explode(self):
        f = log_mod.RedactFilter([])
        self.assertTrue(f.filter(self._record("token=abc123 普通日志")))

    def test_masks_configured_keys(self):
        f = log_mod.RedactFilter(["token"])
        rec = self._record('请求头 {"token": "abc123"} 已发出')
        self.assertTrue(f.filter(rec))
        self.assertNotIn("abc123", rec.getMessage())
        self.assertIn("***", rec.getMessage())

    def test_masks_bearer_even_without_keys(self):
        f = log_mod.RedactFilter(None)
        rec = self._record("Authorization: Bearer abc.def-ghi")
        self.assertTrue(f.filter(rec))
        self.assertNotIn("abc.def-ghi", rec.getMessage())


class TestClientBridge(unittest.TestCase):
    """client 驱动的 bridge 路径（2026-10-06 新增）。

    背景：客户端内签到有两条件 —— 点 UI 按钮（DOM）或**直接调客户端自己的接口**
    （bridge）。后者不依赖 UI 渲染，是 WorkBuddy 的主路径（它的签到横幅是按需渲染的，
    已签时压根不挂载，"找按钮"无从下手）。
    """

    def _recipe(self, **client_over):
        from checkin.core.models import Recipe
        client = {
            "debug_port": 1,
            "bridge": {"status_js": "S()", "claim_js": "C()"},
        }
        client.update(client_over)
        return Recipe.from_dict({
            "id": "t", "name": "T", "mode": "client",
            "verdict": {
                "transport": "body_code",
                "rules": [{"code": 0, "result": "success"},
                          {"code": 10001, "result": "already"}],
                "http_401_result": "need_login",
                "unknown_result": "no_action",
                "checked_in_flag": "today_checked_in",
                "result_flag": "status",
                "result_map": {"claimed": "success", "already_claimed": "already"},
            },
            "client": client,
        })

    def _driver(self):
        from checkin.drivers.client_claim import ClientDriver
        return ClientDriver()

    def _patch(self, resolver):
        """接住 target/页面，把 bridge 的返回交给 resolver(expr)。"""
        from checkin.drivers.client_claim import ClientDriver
        calls = []
        seen = {}

        def fake_call(page, expr, timeout):
            calls.append(expr)
            return resolver(expr)

        class FakePage:
            def wake(self): pass
            def close(self): pass

        patchers = [
            mock.patch.object(ClientDriver, "_wait_target",
                              return_value={"webSocketDebuggerUrl": "ws://x"}),
            mock.patch.object(ClientDriver, "_bridge_call", side_effect=fake_call),
            mock.patch("checkin.drivers.client_claim.CDPPage",
                       side_effect=lambda url: FakePage()),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return calls, seen

    # ---------------------------------------------------------------- 关键守卫

    def test_already_skips_claim(self):
        """今日已签 ⇒ **绝不调 claim**。

        这是整条路径最要紧的守卫：签到接口不是幂等查询，多调一次就是"多领一次"
        的语义风险。先读 status 再决定，就是为了让这条路径在已签时零副作用。
        """
        from checkin.core.models import Outcome
        calls, _ = self._patch(lambda e: {"code": 0, "data": {"today_checked_in": True,
                                                              "streak_days": 7}})
        r = self._recipe()
        res = self._driver()._claim_via_bridge(r, r.client, 1, "m", r.client["bridge"])
        self.assertEqual(res.outcome, Outcome.ALREADY)
        self.assertEqual(calls, ["S()"], "已签时只该读 status，不得触碰 claim")

    def test_success_needs_confirmation(self):
        """claim 报成功、但复核 status 仍显示未签 ⇒ 降级为 no_action。

        trigger 的响应体格式若将来变了（服务端改字段），不能被它牵着报"成功" ——
        宁可报"没把握"，也不要报一个没有证据的假成功。
        """
        from checkin.core.models import Outcome
        seq = [{"code": 0, "data": {"today_checked_in": False}},   # status：未签
               {"code": 0, "data": {"status": "claimed"}},         # claim：报成功
               {"code": 0, "data": {"today_checked_in": False}}]   # 复核：仍未签 → 不信
        it = iter(seq)
        self._patch(lambda e: next(it))
        r = self._recipe()
        res = self._driver()._claim_via_bridge(r, r.client, 1, "m", r.client["bridge"])
        self.assertEqual(res.outcome, Outcome.NO_ACTION)

    def test_success_confirmed(self):
        """claim 报成功 + 复核确认已签 ⇒ success。"""
        from checkin.core.models import Outcome
        seq = [{"code": 0, "data": {"today_checked_in": False}},
               {"code": 0, "data": {"status": "claimed"}},
               {"code": 0, "data": {"today_checked_in": True}}]
        it = iter(seq)
        self._patch(lambda e: next(it))
        r = self._recipe()
        res = self._driver()._claim_via_bridge(r, r.client, 1, "m", r.client["bridge"])
        self.assertEqual(res.outcome, Outcome.SUCCESS)

    def test_status_timeout_is_error_not_success(self):
        """status 无响应（超时/表达式异常）⇒ ERROR。**绝不能默认成成功。**"""
        from checkin.core.models import Outcome
        self._patch(lambda e: None)
        r = self._recipe()
        res = self._driver()._claim_via_bridge(r, r.client, 1, "m", r.client["bridge"])
        self.assertEqual(res.outcome, Outcome.ERROR)

    def test_route_prefers_bridge(self):
        """配方带 bridge ⇒ `_claim` 走 bridge 分支，不去找 DOM。"""
        from checkin.drivers.client_claim import ClientDriver
        r = self._recipe()
        with mock.patch.object(ClientDriver, "_claim_via_bridge",
                               return_value="BRIDGE") as m:
            out = self._driver()._claim(r, r.client, 1, "m")
        self.assertEqual(out, "BRIDGE")
        m.assert_called_once()

    def test_route_falls_back_to_dom(self):
        """配方没有 bridge ⇒ 仍走原来的 DOM 路径（不破坏 Qoder）。"""
        from checkin.drivers.client_claim import ClientDriver
        r = self._recipe()
        r.client.pop("bridge")
        with mock.patch.object(ClientDriver, "_wait_target", return_value=None), \
             mock.patch.object(ClientDriver, "_log_state", return_value=None):
            out = self._driver()._claim(r, r.client, 1, "m")
        self.assertEqual(out.outcome.value, "no_action")

    # ---------------------------------------------------------------- 执行层

    def test_kick_template_survives_braces(self):
        """表达式里的 `{}` 必须原样送达页面。

        守住一个真实踩点：kick 模板若用 f-string 拼，`post(path, {})` 里的花括号
        会被当成占位符 → 直接抛异常，整条路径静默失效。
        """
        from checkin.drivers.client_claim import ClientDriver
        seen = []

        class FakePage:
            def evaluate(self, expr, await_promise=True):
                seen.append(expr)
                return "started"

        # retries=1：本测试只验"kick 模板没被 f-string 破坏"，轮询超时是预期的，
        # 没必要真去重试（那会白等 3s+6s，把整个验证门拖慢 10 秒）。
        ClientDriver._bridge_call(FakePage(), "post('/x', {a: 1})", timeout=0.3, retries=1)
        self.assertIn("post('/x', {a: 1})", seen[0])

    def test_bridge_call_unwraps_json_string(self):
        """页面回传的 value 是 JSON 字符串（returnByValue 最稳的形态），要能解开。"""
        from checkin.drivers.client_claim import ClientDriver
        payload = '{"code": 0, "data": {"today_checked_in": true}}'
        state = {"n": 0}

        class FakePage:
            def evaluate(self, expr, await_promise=True):
                state["n"] += 1
                if state["n"] == 1:
                    return "started"
                return json.dumps({"value": payload})

        out = ClientDriver._bridge_call(FakePage(), "S()", timeout=5)
        self.assertEqual(out, {"code": 0, "data": {"today_checked_in": True}})

    def test_bridge_call_reports_page_error(self):
        """页面异常要带回**真实 message**，并在驱动侧表现为 None（而不是假数据）。"""
        from checkin.drivers.client_claim import ClientDriver
        state = {"n": 0}

        class FakePage:
            def evaluate(self, expr, await_promise=True):
                state["n"] += 1
                if state["n"] == 1:
                    return "started"
                return json.dumps({"error": "Cannot read properties of undefined"})

        self.assertIsNone(ClientDriver._bridge_call(FakePage(), "S()", timeout=5))

    def test_bridge_call_retries_transient_failure_and_rewakes(self):
        """调用层失败（页面冻结 / WebSocket 超时）要**重试**，且每次重试前重新 wake。

        2026-10-17 实测：客户端被我们重启拉起时窗口在后台，Chromium 冻结页面，
        `await` 的 Promise 不 resolve ⇒ 单次调用必失败；用户手动启动（前台）则不会。
        这就是"我重启就失败、你重启就好"的原因。
        """
        from checkin.drivers.client_claim import ClientDriver
        calls = {"n": 0}

        class FakePage:
            def __init__(self):
                self.wakes = 0
            def wake(self):
                self.wakes += 1

        page = FakePage()

        def fake_once(p, expr, timeout=25):
            calls["n"] += 1
            if calls["n"] < 3:
                return None, True                 # 前两次"调用层失败" ⇒ 可重试
            return {"code": 0}, False

        with mock.patch.object(ClientDriver, "_bridge_call_once",
                               side_effect=fake_once), \
             mock.patch("checkin.drivers.client_claim.time.sleep") as sl:
            out = ClientDriver._bridge_call(page, "S()", timeout=5)
        self.assertEqual(out, {"code": 0})
        self.assertEqual(calls["n"], 3, "失败两次后第三次应成功")
        self.assertEqual(sl.call_count, 2, "每次重试前都要等待")
        self.assertEqual(page.wakes, 2, "每次重试前都要重新 wake（解冻）")

    def test_bridge_call_does_not_retry_expression_error(self):
        """表达式报错（服务端 5xx 等）说明**链路是通的**，重试无意义，必须立刻返回。

        ⚠ 这里**刻意不 mock `_bridge_call_once`** —— 用 FakePage 真实走一遍。
        之前 mock 了底层，结果"表达式错误也重试"的退化抓不到（只表现为整体变慢 20 秒，
        没有断言变红）。断言要落在可观测行为上：**一次都不该多等**。
        """
        from checkin.drivers.client_claim import ClientDriver

        class FakePage:
            def __init__(self):
                self.kicks = 0
                self.wakes = 0
            def evaluate(self, expr, await_promise=True):
                self.kicks += 1
                if self.kicks == 1:
                    return "started"
                return json.dumps({"error": "Request failed with status code 500"})
            def wake(self):
                self.wakes += 1

        page = FakePage()
        with mock.patch("checkin.drivers.client_claim.time.sleep") as sl:
            self.assertIsNone(ClientDriver._bridge_call(page, "S()", timeout=1, retries=3))
        # 0.35s 是轮询间隔（正常），重试等待是 3s/6s —— 只要没有"等待级"sleep 就行
        self.assertTrue(all(c.args and c.args[0] < 1 for c in sl.call_args_list),
                        f"表达式错误时不该有重试等待，实际 sleep：{sl.call_args_list}")
        self.assertEqual(page.wakes, 0, "表达式错误时连 wake 都不该发生")
        self.assertEqual(page.kicks, 2, "只该 kick 一次 + 轮询一次，不该有第二轮")

    def test_bridge_call_surfaces_server_error_body(self):
        """HTTP 错误时必须把**服务端响应体**也带回来（2026-10-13 实测教训）。

        WorkBuddy 当天服务端故障：页面抛出的 message 只有
        `Request failed with status code 500`，而真正的原因
        （`data.code=10001` / `msg="签到配置加载失败，请稍后重试"`）在 `e.response.data` 里。
        少了这一步，日志只会说"表达式异常"，看着像驱动的 bug、其实是服务端故障 ——
        当天为此专门写了个诊断脚本才看清，白绕一圈。

        ⚠ 断言必须精确到 `JSON.stringify(e.response.data)`：第一版只断言
        `"e.response.data" in kick`，而那个串在**条件判断**里也出现 ——
        把取值改成 `e.data` 后断言依然成立，守卫名存实亡（已用"改坏看变红"验证过）。
        """
        from checkin.drivers.client_claim import ClientDriver
        state = {"n": 0}
        body = '{"status":500,"data":{"code":10001,"msg":"签到配置加载失败，请稍后重试"}}'

        class FakePage:
            def evaluate(self, expr, await_promise=True):
                state["n"] += 1
                if state["n"] == 1:
                    self.kick = expr
                    return "started"
                return json.dumps({"error": "Request failed with status code 500",
                                   "detail": body})

        page = FakePage()
        with self.assertLogs("checkin.driver.client", level="WARNING") as cm:
            self.assertIsNone(ClientDriver._bridge_call(page, "post('/x', {})", timeout=5))
        # ① 取值来源必须是 e.response.data（防改成 e.data 之类的近似物）
        self.assertIn("JSON.stringify(e.response.data)", page.kick)
        # ② 取到的值必须真的赋进回传槽位（防只算不用、detail 永远是 undefined）
        self.assertIn("detail:", page.kick)
        # ③ 服务端响应体必须真的出现在日志里 —— 这才是本测试要守的行为
        self.assertIn("签到配置加载失败", "\n".join(cm.output))


class TestClientStaleSnapshot(unittest.TestCase):
    """活动页**快照陈旧**造成的静默漏签（2026-10-06 新增）。

    背景：客户端的活动 Surface 一旦被打开就会一直留着 —— 驱动只断 WebSocket
    （`page.close()`），从不关闭那个 UI。于是它的 DOM 停在**打开那一刻**。
    跨天后服务端已刷新到新窗口，页面却不会自动重载，于是
    "找不到按钮 + 页面含『已领取』"这条判定把「今天其实可领」误判成「今天已领」，
    记为 ALREADY —— 不报错、不提醒，**静默漏签一整天**。

    铁证：当日 10:45:17 驱动报 already（"端口就绪 → 出结论"只隔 **34 毫秒**，
    说明命中的是**早已存在**的 iframe），而客户端日志 02:44:51Z 起 `claimable`
    一直是 `true`，直到 15:31 手动领取后才转 `false`。
    """

    def _driver(self):
        from checkin.drivers.client_claim import ClientDriver
        return ClientDriver()

    def _recipe(self):
        from checkin.core.models import Recipe
        return Recipe.from_dict({
            "id": "t", "name": "T", "mode": "client",
            "client": {"debug_port": 1, "claim_button": "领取",
                       "claimed_markers": ["已领取", "领取成功"],
                       "target_match": "activity-iframe"},
        })

    def _patch(self, snaps, reload_ok=True):
        """把 `evaluate(find)` 依次接到 snaps 上（用尽后重复最后一个）。

        返回 state：记录 reload 次数、点击次数，以及驱动是否真的走到了领取。
        """
        from checkin.drivers.client_claim import ClientDriver
        state = {"i": 0, "reloads": 0, "clicks": 0, "reads": 0}

        class FakePage:
            def wake(self): pass
            def close(self): pass
            def reload(self, wait_sec=0.0):
                state["reloads"] += 1
                return reload_ok
            def evaluate(self, expr, await_promise=True):
                if "pointerdown" in expr:          # 领取用的点击脚本
                    state["clicks"] += 1
                    return "dispatched"
                state["reads"] += 1
                i = min(state["i"], len(snaps) - 1)
                state["i"] += 1
                return snaps[i]

        patchers = [
            mock.patch.object(ClientDriver, "_wait_target",
                              return_value={"webSocketDebuggerUrl": "ws://x"}),
            mock.patch.object(ClientDriver, "_STALE_RECHECK_SEC", 0.05),
            mock.patch.object(ClientDriver, "_STALE_RELOAD_WAIT", 0.0),
            mock.patch("checkin.drivers.client_claim.time.sleep"),
            mock.patch("checkin.drivers.client_claim.CDPPage",
                       side_effect=lambda url: FakePage()),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return state

    def _run(self):
        from checkin.drivers.client_claim import ClientDriver
        r = self._recipe()
        return ClientDriver()._claim(r, r.client, 1, "activity-iframe")

    # ------------------------------------------------------------- 判据本身

    def test_claimable_truth_table(self):
        """「能领」只取决于**按钮本身**；页面文本不参与。

        页面文本是渲染快照，可能跨天陈旧 —— 拿它当判据正是本次事故的成因。
        """
        from checkin.drivers.client_claim import _claimable
        self.assertTrue(_claimable({"found": True, "disabled": False}))
        self.assertFalse(_claimable({"found": True, "disabled": True}))
        self.assertFalse(_claimable({"found": False, "disabled": None}))
        self.assertFalse(_claimable({}))

    # ------------------------------------------------------------- 核心回归

    def test_stale_snapshot_is_reloaded_then_claimed(self):
        """陈旧快照（写着"已领取"）重载后露出按钮 ⇒ 必须继续领取，**不能报 already**。

        这是本次修复的主守卫：漏签一整天，代价是 100 Credits。
        """
        from checkin.core.models import Outcome
        snaps = [
            {"found": False, "body": "专属活动权益\n已领取\n"},          # 昨天的残留 DOM
            {"found": True, "disabled": False, "body": "领取\n"},        # 重载后：可领
            {"found": False, "body": "领取成功，Credits 已到账\n已领取"},  # 点击后
        ]
        state = self._patch(snaps)
        self.assertEqual(self._run().outcome, Outcome.SUCCESS)
        self.assertEqual(state["reloads"], 1, "陈旧候选必须触发一次重载复核")
        self.assertEqual(state["clicks"], 1, "复核通过后必须真的点下去")

    def test_genuinely_claimed_stays_already_and_never_clicks(self):
        """真已领取：重载后依旧没有按钮 ⇒ ALREADY，且**绝不点击**。

        复核不能把"已领取"变成"重复领取" —— 签到接口不是幂等查询。
        """
        from checkin.core.models import Outcome
        stale = {"found": False, "ready": True, "body": "专属活动权益\n已领取\n"}
        state = self._patch([stale])          # 重载后仍是这一份
        self.assertEqual(self._run().outcome, Outcome.ALREADY)
        self.assertEqual(state["clicks"], 0, "已领取时绝不能点")
        self.assertGreaterEqual(state["reloads"], 1)

    def test_ready_without_button_stops_immediately(self):
        """重载后页面**已加载完**（readyState=complete）却没有按钮 ⇒ 立刻收手。

        少了这条判据，"今天确实已领"这种**常态**每天都要白等满 `_STALE_RECHECK_SEC`。
        用"读取次数"而非墙钟计时来断言 —— 测试里 sleep 是被 patch 掉的。
        """
        from checkin.core.models import Outcome
        state = self._patch([{"found": False, "ready": True, "body": "已领取"}])
        self.assertEqual(self._run().outcome, Outcome.ALREADY)
        self.assertEqual(state["reads"], 2, "初次 1 次 + 重载后 1 次，之后应立刻收手")

    def test_not_ready_keeps_polling_until_button_appears(self):
        """页面**还没加载完**时必须继续轮询 —— 提前收手会把"慢"误判成"已领"。"""
        from checkin.core.models import Outcome
        snaps = [
            {"found": False, "ready": False, "body": ""},              # 初次：页面在白屏
            {"found": False, "ready": False, "body": "加载中"},         # 重载后：仍未就绪
            {"found": True, "disabled": False, "body": "领取"},         # 就绪后露出按钮
            {"found": False, "body": "领取成功，Credits 已到账"},
        ]
        state = self._patch(snaps)
        self.assertEqual(self._run().outcome, Outcome.SUCCESS)
        self.assertEqual(state["clicks"], 1)

    def test_reload_failure_keeps_original_verdict(self):
        """重载失败 ⇒ 沿用原判定，不抛异常、不误报成功（复核是尽力而为）。"""
        from checkin.core.models import Outcome
        state = self._patch([{"found": False, "body": "已领取"}], reload_ok=False)
        self.assertEqual(self._run().outcome, Outcome.ALREADY)
        self.assertEqual(state["clicks"], 0)

    def test_fresh_claimable_skips_reload(self):
        """按钮本来就在 ⇒ **不做重载**。

        重载不是免费的（页面要重新加载，实测 ~0.8s），正常路径上不该付这个成本。
        """
        from checkin.core.models import Outcome
        snaps = [
            {"found": True, "disabled": False, "body": "领取\n"},
            {"found": False, "body": "领取成功，Credits 已到账\n"},
        ]
        state = self._patch(snaps)
        self.assertEqual(self._run().outcome, Outcome.SUCCESS)
        self.assertEqual(state["reloads"], 0, "能领时不该重载")
        self.assertEqual(state["clicks"], 1)

    def test_disabled_button_is_also_rechecked(self):
        """按钮置灰也属"可能是陈旧快照"，同样要复核一次。

        不同客户端版本领取后的表现不同：有的把按钮删掉，有的把它置灰。
        """
        from checkin.core.models import Outcome
        snaps = [
            {"found": True, "disabled": True, "body": "已领取\n"},     # 陈旧：置灰
            {"found": True, "disabled": False, "body": "领取\n"},      # 重载后可领
            {"found": False, "body": "领取成功，Credits 已到账\n"},
        ]
        state = self._patch(snaps)
        self.assertEqual(self._run().outcome, Outcome.SUCCESS)
        self.assertEqual(state["reloads"], 1)
        self.assertEqual(state["clicks"], 1)


class TestRoundBudget(unittest.TestCase):
    """整轮时间预算 + 两遍扫描 —— **2026-10-17 三站全军覆没的回归守卫**。

    当天的事故：站点串行执行，qoder 卡在"守候端口 5400s"那个分支上，
    排在它后面的 trae / workbuddy **压根没被执行**；因为进程一直在等，
    连最后的汇总通知都发不出来 —— 用户只能自己翻日志才发现整天没签到。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")

    def _engine_cfg(self, budget=120, cap=1):
        cfg = AppConfig(root=str(PROJECT_ROOT), chrome=ChromeConfig(),
                        safety=SafetyConfig(), state_path=self.state_path,
                        log_dir=os.path.join(self.tmp.name, "logs"))
        cfg.safety.pre_delay_range = [0.0, 0.0]        # 拟人延迟会拖慢测试，这里不需要
        cfg.safety.inter_site_gap_range = [0.0, 0.0]
        cfg.safety.round_budget_sec = budget
        cfg.safety.first_pass_wait_cap_sec = cap
        return cfg

    @staticmethod
    def _client(sid, port, **over):
        client = {"debug_port": port, "wait_port_sec": 5400}
        client.update(over)
        return Recipe.from_dict({"id": sid, "name": sid, "enabled": True,
                                 "mode": "client", "client": client})

    def test_a_stuck_site_does_not_starve_the_others(self):
        """**本组最重要的一条**：第一个站点卡住，后面的站点必须照常签到。

        断言落在"后面的站点真的被 Claim 过"这个**可观测行为**上 ——
        只看"轮次在预算内结束"是不够的：卡住时也会结束，只是白白漏了一整天。
        """
        from checkin.drivers.client_claim import ClientDriver

        stuck = self._client("stuck", 9336)
        fine = self._client("fine", 9337)
        ok = CheckinResult("fine", Outcome.SUCCESS, message="ok")

        # 记录**调用顺序**：这才是不被饿死的结构性证据 ——
        # 先快速放弃卡住的站点 → 服务能服务的 → 最后才回头啃硬骨头。
        # （只看"轮次结束"是抓不到回归的：卡住时也一样会结束，只是别人没被服务过。）
        order: list = []

        def _fake_wait(_port, timeout, poll=0.8):
            order.append(("wait", _port, timeout))
            time.sleep(min(0.05, timeout / 1000.0))   # 等比缩小的"等待"，保留时序关系
            return False

        def _fake_claim(_recipe, _c, port, _match):
            order.append(("claim", _recipe.id))
            return ok

        with mock.patch.object(ClientDriver, "_log_state", return_value=None), \
             mock.patch.object(ClientDriver, "_port_alive", side_effect=lambda p: p == 9337), \
             mock.patch.object(ClientDriver, "_app_running", return_value="cmdline"), \
             mock.patch.object(ClientDriver, "_wait_port", side_effect=_fake_wait), \
             mock.patch.object(ClientDriver, "_remind"), \
             mock.patch.object(ClientDriver, "_claim", side_effect=_fake_claim):
            results = Engine(self._engine_cfg(budget=120, cap=7), [stuck, fine]).run(now=True)

        self.assertEqual([r.site_id for r in results], ["stuck", "fine"])
        self.assertEqual([e[0] for e in order], ["wait", "claim", "wait"],
                         "必须是「先让位 → 服务别人 → 回头补领」，不能一头扎进等待里")
        self.assertLessEqual(order[0][2], 7, "第一次尝试的等待必须被 cap 夹住")
        self.assertGreater(order[2][2], 7, "回头补领时才允许放开等")
        self.assertIs(results[1].outcome, Outcome.SUCCESS)
        self.assertIs(results[0].outcome, Outcome.NO_ACTION)

    def test_first_pass_wait_is_capped_so_the_round_cannot_hang(self):
        """第一遍里单站点的等待必须被压到 cap，而不是吃掉配方的 5400s。"""
        from checkin.drivers.client_claim import ClientDriver

        cfg = self._engine_cfg(budget=120, cap=7)
        cfg.budget = RunBudget(0.0, 1)          # 模拟引擎已在第一遍
        with mock.patch.object(ClientDriver, "_log_state", return_value=None), \
             mock.patch.object(ClientDriver, "_port_alive", return_value=False), \
             mock.patch.object(ClientDriver, "_app_running", return_value="cmdline"), \
             mock.patch.object(ClientDriver, "_wait_port", return_value=False) as wp, \
             mock.patch.object(ClientDriver, "_remind"):
            ClientDriver().run(self._client("s", 9336), cfg)
        self.assertEqual(wp.call_args[0][1], 7,
                         "wait_port_sec=5400 也要先被 first_pass_wait_cap_sec 夹住")

    def test_budget_left_overrides_everything_when_time_runs_out(self):
        """预算快耗尽时，等待必须让步 —— 否则永远出不了结论、也发不出通知。"""
        from checkin.drivers.client_claim import ClientDriver

        cfg = self._engine_cfg(cap=25)
        cfg.budget = RunBudget(time.monotonic() + 3, 2)   # 第二遍，只剩 3 秒
        with mock.patch.object(ClientDriver, "_log_state", return_value=None), \
             mock.patch.object(ClientDriver, "_port_alive", return_value=False), \
             mock.patch.object(ClientDriver, "_app_running", return_value="cmdline"), \
             mock.patch.object(ClientDriver, "_wait_port", return_value=False) as wp, \
             mock.patch.object(ClientDriver, "_remind"):
            ClientDriver().run(self._client("s", 9336), cfg)
        self.assertLessEqual(wp.call_args[0][1], 3)

    def test_no_budget_keeps_the_recipe_value(self):
        """没给预算（手工调用 / 老调用方）⇒ 行为与改造前完全一致。"""
        from checkin.drivers.client_claim import ClientDriver

        cfg = AppConfig(root=str(PROJECT_ROOT), chrome=ChromeConfig(),
                        safety=SafetyConfig(), state_path=self.state_path,
                        log_dir=os.path.join(self.tmp.name, "logs"))
        self.assertIsNone(getattr(cfg, "budget", None))
        with mock.patch.object(ClientDriver, "_log_state", return_value=None), \
             mock.patch.object(ClientDriver, "_port_alive", return_value=False), \
             mock.patch.object(ClientDriver, "_app_running", return_value="cmdline"), \
             mock.patch.object(ClientDriver, "_wait_port", return_value=True), \
             mock.patch.object(ClientDriver, "_claim",
                               return_value=CheckinResult("s", Outcome.SUCCESS)):
            res = ClientDriver().run(self._client("s", 9336), cfg)
        self.assertIs(res.outcome, Outcome.SUCCESS)

    def test_pass_is_recorded_in_the_result_detail(self):
        """结算时把"这是第几遍"带出来 —— 排障时一眼看出有没有走到补领。"""
        from checkin.drivers.client_claim import ClientDriver

        cfg = self._engine_cfg()
        cfg.budget = RunBudget(0.0, 2)
        with mock.patch.object(ClientDriver, "_log_state", return_value=None), \
             mock.patch.object(ClientDriver, "_port_alive", return_value=False), \
             mock.patch.object(ClientDriver, "_app_running", return_value="cmdline"), \
             mock.patch.object(ClientDriver, "_wait_port", return_value=False), \
             mock.patch.object(ClientDriver, "_remind"):
            res = ClientDriver().run(self._client("s", 9336), cfg)
        self.assertEqual(res.detail.get("pass"), 2)

    def test_budget_config_defaults_stay_in_sync(self):
        """`config.yaml` ↔ `SafetyConfig` 两处默认值必须一致。

        这一条是本项目老坑：**失同步是静默的**，只在配置缺失时才暴露。
        """
        from checkin.core import config as cfg_mod
        cfg, _ = cfg_mod.load_config(str(PROJECT_ROOT))
        self.assertEqual(cfg.safety.round_budget_sec, SafetyConfig.round_budget_sec)
        self.assertEqual(cfg.safety.first_pass_wait_cap_sec,
                         SafetyConfig.first_pass_wait_cap_sec)
        self.assertGreater(cfg.safety.round_budget_sec, 0, "默认必须有预算，否则等于没这道闸")

    def test_clamp_applies_the_role_cap_then_the_budget(self):
        """两道夹取必须依次生效，且顺序不能反（角色夹快扫、预算夹上限）。"""
        a = RunBudget(0.0, 1)                              # 不限时 + 快扫
        self.assertEqual(a.clamp(5400, 25), 25)
        b = RunBudget(time.monotonic() + 5, 1)             # 只剩 5s + 快扫
        self.assertLessEqual(b.clamp(5400, 25), 5)         # 期间还会流逝一点点
        self.assertGreater(b.clamp(5400, 25), 4)
        c = RunBudget(time.monotonic() + 600, 2)           # 第二遍不再受角色夹
        self.assertEqual(c.clamp(300, 25), 300)
        d = RunBudget(time.monotonic() - 10, 2)            # 已经超支
        self.assertEqual(d.clamp(5400, 25), 0.0, "耗尽了就是 0，不许为负")
        self.assertEqual(RunBudget(0.0, 1).left(), None)   # 0 = 不限，不是"已耗尽"


class TestClientSelfHealGroundwork(unittest.TestCase):
    """2026-10-17 补的三条自愈路径：僵尸换端口 / CPU 旁证 / 启动入口修复。"""

    def setUp(self):
        from checkin.drivers.client_claim import ClientDriver
        self.d = ClientDriver()

    def _root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        os.makedirs(os.path.join(root, "tools"))
        with open(os.path.join(root, "tools", "win_shortcut_args.py"), "w") as fh:
            fh.write("# stub\n")
        return root

    @staticmethod
    def _client(sid="demo", **over):
        client = {"debug_port": 9338, "process_match": "Demo.exe", "wait_port_sec": 0}
        client.update(over)
        return Recipe.from_dict({"id": sid, "name": sid, "mode": "client", "client": client})

    # ------------------------------------------------ 僵尸端口 ⇒ 换端口

    def test_zombie_port_switches_to_a_clean_port(self):
        """僵尸 socket 拿不回来 ⇒ **必须用新端口**重启，否则重启多少次都是同一个死局。"""
        from checkin.drivers.client_claim import ClientDriver

        r = self._client(restart_on_bind_failure=True)
        ok = CheckinResult("demo", Outcome.SUCCESS, message="ok")
        with mock.patch.object(self.d, "_log_state", return_value=None), \
             mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running",
                               return_value='"x.exe" --remote-debugging-port=9338'), \
             mock.patch.object(self.d, "_wait_port", return_value=False), \
             mock.patch.object(ClientDriver, "_port_is_zombie", return_value=True), \
             mock.patch.object(self.d, "_reserve_clean_port", return_value=9341), \
             mock.patch.object(self.d, "_safe_to_restart", return_value=True), \
             mock.patch.object(self.d, "_restart_client", return_value=True) as rs, \
             mock.patch.object(self.d, "_claim", return_value=ok):
            res = self.d.run(r, _cfg())
        self.assertIs(res, ok)
        self.assertEqual(rs.call_args[0][1], 9341,
                         "僵尸端口下仍用旧端口拉起 = 必然再失败一次")

    def test_non_zombie_port_reuses_the_same_port(self):
        """只是没绑上（端口本身是干净的）⇒ 不必换端口。"""
        from checkin.drivers.client_claim import ClientDriver

        r = self._client(restart_on_bind_failure=True)
        ok = CheckinResult("demo", Outcome.SUCCESS, message="ok")
        with mock.patch.object(self.d, "_log_state", return_value=None), \
             mock.patch.object(self.d, "_port_alive", return_value=False), \
             mock.patch.object(self.d, "_app_running",
                               return_value='"x.exe" --remote-debugging-port=9338'), \
             mock.patch.object(self.d, "_wait_port", return_value=False), \
             mock.patch.object(ClientDriver, "_port_is_zombie", return_value=False), \
             mock.patch.object(self.d, "_reserve_clean_port") as alt, \
             mock.patch.object(self.d, "_safe_to_restart", return_value=True), \
             mock.patch.object(self.d, "_restart_client", return_value=True) as rs, \
             mock.patch.object(self.d, "_claim", return_value=ok):
            self.d.run(r, _cfg())
        alt.assert_not_called()
        self.assertEqual(rs.call_args[0][1], 9338)

    def test_port_is_zombie_needs_a_dead_listener_pid(self):
        """僵尸判据 = netstat 有人在 LISTEN **且** 那个 PID 已死。缺一不可。"""
        from checkin.drivers.client_claim import ClientDriver

        with mock.patch.object(ClientDriver, "_port_listener_pid", return_value=4242), \
             mock.patch.object(ClientDriver, "_pid_alive", return_value=False):
            self.assertTrue(self.d._port_is_zombie(9338))
        with mock.patch.object(ClientDriver, "_port_listener_pid", return_value=4242), \
             mock.patch.object(ClientDriver, "_pid_alive", return_value=True):
            self.assertFalse(self.d._port_is_zombie(9338), "PID 活着就还没被占死")
        with mock.patch.object(ClientDriver, "_port_listener_pid", return_value=None):
            self.assertFalse(self.d._port_is_zombie(9338), "没 LISTENING 行 = 不是僵尸")

    def test_write_recipe_port_keeps_the_comments(self):
        """换端口要**改写注释里那行**，而不是重写整个 YAML —— 配方大半价值在注释里。"""
        import yaml
        from checkin.drivers.client_claim import ClientDriver

        root = self._root()
        os.makedirs(os.path.join(root, "recipes"))
        path = os.path.join(root, "recipes", "demo.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# 踩坑注释：端口是一次性资源\n"
                     "client:\n  debug_port: 9338\n  close_after: false\n")
        self.assertTrue(ClientDriver._write_recipe_port(root, "demo", 9351))
        text = open(path, encoding="utf-8").read()
        self.assertIn("# 踩坑注释", text, "注释不能被冲掉")
        self.assertIn("close_after: false", text)
        self.assertEqual(yaml.safe_load(text)["client"]["debug_port"], 9351)
        self.assertNotIn("9338", text)

    def test_write_recipe_port_refuses_files_it_cannot_pin_down(self):
        """找不到目标行、或出现多处 ⇒ 拒绝改写（宁可不动手，也不能写坏配方）。"""
        from checkin.drivers.client_claim import ClientDriver

        root = self._root()
        os.makedirs(os.path.join(root, "recipes"))
        path = os.path.join(root, "recipes", "demo.yaml")
        for label, body in (("没有目标行", "client:\n  exe: 'x.exe'\n"),
                            ("目标行不止一处", "client:\n  debug_port: 9338\n"
                                              "other:\n  debug_port: 9339\n")):
            with self.subTest(case=label):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
                self.assertFalse(ClientDriver._write_recipe_port(root, "demo", 9351))
                self.assertEqual(open(path, encoding="utf-8").read(), body,
                                 "拒绝时必须一个字节都不动")

    def test_write_recipe_port_ignores_lines_inside_comments(self):
        """注释里提到 debug_port 不算一处 —— 配方注释极多，误伤就把知识库冲掉了。"""
        from checkin.drivers.client_claim import ClientDriver

        root = self._root()
        os.makedirs(os.path.join(root, "recipes"))
        path = os.path.join(root, "recipes", "demo.yaml")
        body = ("# 注释里也写了 debug_port: 9330，别改我\n"
                "client:\n  debug_port: 9338\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.assertTrue(ClientDriver._write_recipe_port(root, "demo", 9351))
        text = open(path, encoding="utf-8").read()
        self.assertIn("# 注释里也写了 debug_port: 9330，别改我", text)
        self.assertIn("debug_port: 9351", text)

    # ------------------------------------------------ 忙闲关的 CPU 旁证

    def test_busy_gate_accepts_cpu_evidence_when_probe_is_blind(self):
        """busy_js 取不到结论（端口不通时必然）⇒ **退到进程 CPU 旁证**，空闲才放行。

        没有这条退路，用户 2026-10-14 授权的"该重启重启"就永远执行不了 ——
        因为 busy_js 恰恰在需要重启的场景里跑不起来（详见 `_busy_state` 的说明）。
        """
        r = self._client(busy_probe={"busy_js": "false", "wait_busy_sec": 60})
        with mock.patch.object(self.d, "_client_busy", return_value=None), \
             mock.patch.object(self.d, "_wait_client_idle", return_value=False) as mon:
            self.assertTrue(
                self.d._safe_to_restart(r, r.client, 9338, "m",
                                        idle_fallback=lambda: False))
            self.assertFalse(
                self.d._safe_to_restart(r, r.client, 9338, "m",
                                        idle_fallback=lambda: True),
                "CPU 显示有活干 ⇒ 不许重启")
        # 忙的时候是"等它跑完"，不是"立刻动手" —— 这条粒度与 TestBusyGate 同口径
        mon.assert_called_once()

    def test_busy_gate_still_refuses_when_both_sources_are_blind(self):
        """两条情报都说不清 ⇒ 依然一票否决。**不可知 ≠ 空闲**，这条原则不能松。"""
        r = self._client(busy_probe={"busy_js": "false", "wait_busy_sec": 60})
        with mock.patch.object(self.d, "_client_busy", return_value=None):
            self.assertFalse(self.d._safe_to_restart(r, r.client, 9338, "m",
                                                     idle_fallback=lambda: None))
            self.assertFalse(self.d._safe_to_restart(r, r.client, 9338, "m"))

    def test_cpu_busy_threshold(self):
        """CPU 判据：观测窗内的实际消耗与阈值比较，取不到样本就返回 None（看不清）。"""
        r = self._client(busy_probe={"cpu_window_sec": 0.01, "idle_cpu_sec": 5})
        with mock.patch.object(self.d, "_cpu_total", side_effect=[10.0, 12.0]):
            self.assertFalse(self.d._cpu_busy(r.client), "1 核·秒 < 5 ⇒ 空闲")
        with mock.patch.object(self.d, "_cpu_total", side_effect=[10.0, 99.0]):
            self.assertTrue(self.d._cpu_busy(r.client), "89 核·秒 ⇒ 明显在干活")
        with mock.patch.object(self.d, "_cpu_total", return_value=None):
            self.assertIsNone(self.d._cpu_busy(r.client), "取不到样本 = 看不清")

    def test_cpu_script_filters_and_sums_both_times(self):
        s = self.d._cpu_script("Trae CN.exe")
        self.assertIn("Win32_Process", s)
        self.assertIn("KernelModeTime", s)
        self.assertIn("UserModeTime", s)
        self.assertIn("Name = 'Trae CN.exe'", s)
        self.assertIn("Measure-Object -Sum", s)
        # 进程名里的单引号要靠双写转义，否则整段 PS 脚本会被截断
        self.assertIn("Name = 'a''b'", self.d._cpu_script("a'b"))

    # ------------------------------------------------ 启动入口修复

    def test_repair_entry_runs_once_per_day_and_needs_the_flag(self):
        """没开启 repair_launch_entry 的站点绝不动手；开启了也**一天只修一次**。

        ⚠ 刻意**不 patch** `_repair_done_today` / `_mark_repair_today` —— 让"一天一次"
        这个约束真的跑起来：写戳文件 → 第二次读取时才拦得住。
        （用 mock 把这两步替掉，那条守卫就是个没人守的空壳。）
        """
        root = self._root()
        cfg = AppConfig(root=root, chrome=ChromeConfig(), safety=SafetyConfig(),
                        state_path=os.path.join(root, "state.json"), log_dir=root)
        proc = mock.Mock(stdout="自检通过", stderr="", returncode=0)
        with mock.patch("checkin.drivers.client_claim.subprocess.run",
                        return_value=proc) as run:
            self.d._repair_launch_entry("demo", {"repair_launch_entry": True}, cfg)
            self.assertEqual(run.call_count, 1)
            self.d._repair_launch_entry("demo", {"repair_launch_entry": True}, cfg)
            self.assertEqual(run.call_count, 1, "同一天同一站点只修一次")
            self.d._repair_launch_entry("demo", {}, cfg)
            self.assertEqual(run.call_count, 1, "没开 repair_launch_entry 的站点不该被碰")
            self.d._repair_launch_entry("another", {"repair_launch_entry": True}, cfg)
            self.assertEqual(run.call_count, 2, "别的站点有自己的额度")

    def test_repair_entry_also_fixes_the_autostart_key_when_asked(self):
        """有开机自启项的站点必须连 Run 键一起修 —— 那条路根本不经过快捷方式。"""
        from checkin.drivers.client_claim import ClientDriver

        root = self._root()
        cfg = AppConfig(root=root, chrome=ChromeConfig(), safety=SafetyConfig(),
                        state_path=os.path.join(root, "state.json"), log_dir=root)
        proc = mock.Mock(stdout="自检通过", stderr="", returncode=0)
        with mock.patch.object(self.d, "_repair_done_today", return_value=False), \
             mock.patch.object(ClientDriver, "_mark_repair_today"), \
             mock.patch("checkin.drivers.client_claim.subprocess.run",
                        return_value=proc) as run:
            self.d._repair_launch_entry(
                "demo", {"repair_launch_entry": True, "repair_autostart": True}, cfg)
        self.assertEqual(run.call_count, 2)
        self.assertIn("--autostart", run.call_args_list[1][0][0])

    def test_repair_entry_forces_utf8_on_the_child(self):
        """子进程必须用 utf-8 吐 stdout —— 否则 cp936 字节被按 utf-8 解，
        日志整行变成 U+FFFD，看不出修复到底动没动手。

        2026-10-18 的日志就是这样：`[qoder] 启动入口修复：\ufffd\ufffd...`，
        一句中文都没留下。
        """
        from checkin.drivers.client_claim import ClientDriver

        root = self._root()
        cfg = AppConfig(root=root, chrome=ChromeConfig(), safety=SafetyConfig(),
                        state_path=os.path.join(root, "state.json"), log_dir=root)
        proc = mock.Mock(stdout="自检通过", stderr="", returncode=0)
        # ⚠ 必须把环境**清空**再验：本机开发会话天生带 PYTHONIOENCODING=utf-8，
        # 不清就靠继承也能拿到这个值 ⇒ 守卫永远绿，而真正的受害者（计划任务的
        # pythonw 环境）恰恰没有这个变量 ⇒ 每天定时跑必乱码。这条 2026-10-18 实测。
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(self.d, "_repair_done_today", return_value=False), \
             mock.patch.object(ClientDriver, "_mark_repair_today"), \
             mock.patch("checkin.drivers.client_claim.subprocess.run",
                        return_value=proc) as run:
            self.d._repair_launch_entry("demo", {"repair_launch_entry": True}, cfg)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("env", {}).get("PYTHONIOENCODING"), "utf-8",
                         "子进程环境必须**显式**带上 utf-8，不能指望继承父进程的")

    def test_repair_entry_trusts_the_exit_code_not_the_text(self):
        """成功与否只认**退出码**（工具是 `return 0 if ok else 1`）。

        为什么不能在 stdout 里找"自检通过"：那个判据同时被两件事击穿 ——
        文案一改就失效，编码一糊就失效。2026-10-18 实测后者已经发生了：
        入口其实是修好的（工具 stdout 含"参数已正确，跳过"），但乱码让含 自检通过
        的判定**恒为 False** ⇒ 日志里从头到尾没出现过"已修好启动入口"。
        """
        from checkin.drivers.client_claim import ClientDriver

        root = self._root()
        cfg = AppConfig(root=root, chrome=ChromeConfig(), safety=SafetyConfig(),
                        state_path=os.path.join(root, "state.json"), log_dir=root)
        cases = [
            # (退出码, stdout,                期望是否算修好, 说明)
            (0, "\ufffd\ufffd\ufffd（编码已糊，看不懂）", True, "糊了但退 0 = 真成功"),
            (1, "自检通过", False, "文案写着成功，但退 1 = 没修成"),
            (0, "", True, "静默成功也认"),
        ]
        for code, out, want, note in cases:
            with self.subTest(note=note):
                proc = mock.Mock(stdout=out, stderr="", returncode=code)
                with mock.patch.object(self.d, "_repair_done_today",
                                       return_value=False), \
                     mock.patch.object(ClientDriver, "_mark_repair_today"), \
                     mock.patch("checkin.drivers.client_claim.subprocess.run",
                                return_value=proc):
                    self.assertIs(self.d._repair_launch_entry(
                        "demo", {"repair_launch_entry": True}, cfg), want, note)

    def test_repair_stamp_is_per_site_and_per_day(self):
        today = datetime.now(CST).strftime("%Y-%m-%d")
        root = self._root()
        self.assertFalse(self.d._repair_done_today(root, "demo"))
        self.d._mark_repair_today(root, "demo")
        self.assertTrue(self.d._repair_done_today(root, "demo"))
        self.assertFalse(self.d._repair_done_today(root, "other"),
                         "别的站点有自己的戳，不能共用")
        stamp = json.load(open(os.path.join(root, "data", "entry_repair.json"),
                               encoding="utf-8"))
        self.assertEqual(stamp["demo"], today)


if __name__ == "__main__":
    unittest.main(verbosity=2)
