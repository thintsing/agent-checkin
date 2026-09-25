"""核心逻辑离线单元测试 —— 不联网、不开浏览器、不触发任何签到。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v

设计约定：这里只测"纯逻辑"（判定语义 / 调度 / 状态 / 退避 / 注入脚本 / 锁 /
引擎的只读与闸门分支）。凡是需要真实浏览器与登录态的部分，属于活体测试，
放 test_cdp_live.py。
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
import unittest
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from checkin.__main__ import (                                # noqa: E402
    _install_script,
    _psq,
    _uninstall_script,
    _wake_hint,
)
from checkin.browser import page_script                       # noqa: E402
from checkin.core import jitter, notify, scheduler           # noqa: E402
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
        """唤醒时刻 = 窗口开始前 10 分钟（纯算术，与配置值无关）。"""
        from checkin.__main__ import _wake_hint
        self.assertEqual(_wake_hint("10:05"), "09:55")   # 当前实际配置
        self.assertEqual(_wake_hint("07:40"), "07:30")
        self.assertEqual(_wake_hint("00:05"), "00:00")   # 不产生负时刻
        self.assertEqual(_wake_hint("13:00"), "12:50")


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
        for key in ("exe_glob", "debug_port", "target_match", "data_dir", "process_match"):
            self.assertTrue(c.get(key), f"client.{key} 不能为空")
        self.assertIsInstance(c["debug_port"], int)
        # 必须用 glob：客户端版本目录会滚动累积（0.3.4 / 0.4.1 / 0.4.2 …），
        # 写死路径会在它自更新后**静默**失效 —— 那是这条路线最大的长期成本。
        self.assertIn("*", c["exe_glob"])
        self.assertTrue(c["close_after"] is True,
                        "领完必须关掉 —— 调试端口是本机任何程序都能接管会话的口子")


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

    def test_driver_for_resolves_client(self):
        from checkin.drivers import driver_for
        drv = driver_for("client")
        self.assertIsNotNone(drv)
        self.assertEqual(drv.mode, "client")


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


# ----------------------------------------------------------------------
# 计划任务注册（只验脚本内容，不真跑 —— 真跑会往系统里塞任务）
# ----------------------------------------------------------------------

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

    def test_wakes_before_the_window(self):
        hint = _wake_hint(self.cfg.schedule.window_start)
        self.assertIn(f"-At {hint}", _install_script(self.cfg))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
