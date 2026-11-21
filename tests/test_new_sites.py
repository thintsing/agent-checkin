"""2026-11-19 新接入站点的守卫 —— 直接读**真实配方**，不写死便利值。

为什么要单独一份：这两个站点是同一次侦察里定案的，一条能自动化（MiniMax Code，
走客户端自己的 axios 实例），一条被应用主动禁掉调试端口（TeleAgent）。最容易被
"凭直觉改回去"的正是这两处：
  · 把 MiniMax 的 target_match / status 判据改成猜出来的值 ⇒ 每天静默 no_action；
  · 给 TeleAgent 补一个 client 段"试试看" ⇒ 对称守卫会逼出一套每天空转重启
    用户客户端的无效配置。
所有断言都从磁盘上的真实配方取值（见 tests/test_core.py 里"名存实亡"的教训）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from checkin.core.config import load_config  # noqa: E402


def _recipe(site_id: str):
    _, recipes = load_config(str(PROJECT_ROOT))
    return next(r for r in recipes if r.id == site_id)


def _recipe_text(filename: str) -> str:
    return (PROJECT_ROOT / "recipes" / filename).read_text(encoding="utf-8")


class TestMiniMaxCodeRecipe(unittest.TestCase):
    def setUp(self):
        self.r = _recipe("minimax-code")

    def test_enabled_client_with_its_own_port(self):
        self.assertTrue(self.r.enabled)
        self.assertEqual(self.r.mode, "client")
        self.assertEqual(self.r.client["debug_port"], 9346)

    def test_target_match_is_the_real_cdp_value(self):
        """2026-11-19 真机 /json/list：主窗口 = app://./archon。

        写成 https://agent.minimax.io（静态侦察时的错误推测）会永不命中，
        每天静默 no_action —— 这是本项目踩过多次的"名存实亡"。
        """
        self.assertEqual(self.r.client["target_match"], "app://./archon")
        self.assertEqual(self.r.client["main_target_match"], "app://./archon")

    def test_bridge_returns_top_level_code_via_clients_own_axios(self):
        b = self.r.client["bridge"]
        for key in ("status_js", "claim_js"):
            js = str(b[key])
            self.assertIn("JSON.stringify({code:", js,
                          f"{key}: 驱动只认顶层 code，必须返回 JSON 字符串")
            self.assertIn("req(98304)", js,
                          f"{key}: 必须走客户端自己的 axios 实例（签名由它的拦截器生成）")
        self.assertIn("/minimax-cloud/api/v1/signin/status", str(b["status_js"]))
        self.assertIn("/minimax-cloud/api/v1/signin/claim", str(b["claim_js"]))

    def test_checked_in_flag_is_actually_produced_by_status_js(self):
        """verdict 读的字段，status_js 必须真的算出来。

        客户端自己的判据（/out/_next/static/chunks/69780-*.js）：
            s(e) = e.days.some(d => d.is_today && d.status === 3)
        字段写了、JS 里没生成 ⇒ already_checked_in 恒 False ⇒ 已签也会重复调 claim。
        """
        self.assertEqual(self.r.verdict.checked_in_flag, "checked_in")
        js = str(self.r.client["bridge"]["status_js"])
        self.assertIn("checked_in", js)
        self.assertIn("d.is_today === true", js)
        self.assertIn("d.status === 3", js)

    def test_never_closes_the_users_app(self):
        self.assertIs(self.r.client["close_after"], False,
                      "用户全天在用的桌面应用，close_after 代码默认是 true，必须显式关掉")

    def test_symmetric_self_heal_fields_are_present(self):
        """对称性守卫要求所有 client 配方齐备；这里再钉一次本站的具体取值。"""
        c = self.r.client
        self.assertGreater(int(c.get("wait_port_sec") or 0), 0)
        self.assertIs(c.get("restart_on_bind_failure"), True)
        probe = c.get("busy_probe") or {}
        self.assertTrue(str(probe.get("busy_js") or "").strip())
        self.assertGreater(int(probe.get("wait_busy_sec") or 0), 0)
        # 判据词刻意不含 loading/spinner（Electron 常驻）与 execution/progress
        # （项目里 Trae 空闲就带这两个词，栽过一次）
        self.assertNotIn("spinner", str(probe["busy_js"]))
        self.assertNotIn("execution", str(probe["busy_js"]))


class TestTeleAgentUiaRoute(unittest.TestCase):
    """2026-11-20 定案：CDP 被应用自己堵死，改走 Windows 辅助功能通道（mode: uia）。

    这组断言钉死「为什么是 uia、uia 的判据长什么样」，防止有人：
      · 看到配方 enabled 就以为有 CDP 端口，反手补一个 client 段
        ⇒ 对称守卫逼出一套每天空转重启用户客户端的无效配置；
      · 把按钮名改成模糊匹配 ⇒ 点到侧边栏那个「TeleAgent积分自动领取」任务/会话；
      · 以"换个办法了"为由删掉取证注释 ⇒ 下一个人把反调试再侦察一遍。
    """

    def setUp(self):
        self.r = _recipe("teleagent")

    def test_enabled_with_uia_mode(self):
        self.assertTrue(self.r.enabled)
        self.assertEqual(self.r.mode, "uia")
        self.assertTrue(self.r.uia, "uia 段不能为空，否则驱动没有任何判据可用")

    def test_no_client_section_means_no_port_self_heal(self):
        """没有 client 段 ⇒ 不会被"所有 client 配方必须开自愈"的对称守卫
        逼出一套每天空转重启用户客户端的配置。"""
        self.assertEqual(self.r.client, {})

    def test_window_and_account_matching_use_real_values(self):
        """窗口标题用子串（真机是「TeleAgent 星辰超级智能体」）；
        账号用正则（换账号/重装也能命中，而不是写死 user_98008872）。"""
        self.assertIn("TeleAgent", self.r.uia["window_title_contains"])
        self.assertEqual(self.r.uia["window_class"], "Chrome_WidgetWin_1")
        self.assertEqual(self.r.uia["account_entry_pattern"], r"^user_\d+$")

    def test_button_names_are_exact_not_fuzzy(self):
        """按钮名必须精确。侧边栏真实存在「TeleAgent积分自动领取」这条任务，
        模糊匹配"含领取"会点到它。"""
        self.assertEqual(list(self.r.uia["claim_button_names"]), ["立即领取", "今日已领"])
        self.assertEqual(self.r.uia["claimable_name"], "立即领取")
        self.assertEqual(self.r.uia["claimed_name"], "今日已领")

    def test_evidence_is_still_recorded(self):
        """不允许凭一句"换个办法"就把 CDP 结论抹掉 —— 结论必须有可复核取证。"""
        text = _recipe_text("teleagent.yaml")
        for token in ("removeSwitch", "AntiDebug", "node:inspector",
                      "checkDayTask", "dailySignData", "GetRootControl"):
            self.assertIn(token, text, f"配方里缺少取证关键词 {token!r}")

    def test_shortcut_app_table_has_no_teleagent(self):
        """APPS 表若留条目，recipe_debug_port 读不到配方端口会回落到默认 9337。"""
        spec = importlib.util.spec_from_file_location(
            "win_shortcut_args_under_test",
            PROJECT_ROOT / "tools" / "win_shortcut_args.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertNotIn("teleagent", mod.APPS)


class TestUiaDriverMatching(unittest.TestCase):
    """不连真机也要钉住的判据：**精确匹配**，绝不把侧边栏任务名当按钮。

    真机上这两者同时存在（`ButtonControl 'TeleAgent积分自动领取'` 与
    `ButtonControl '立即领取'`），所以"模糊含领取"不是理论风险。
    """

    class _FakeCtrl:
        def __init__(self, name, children=None):
            self.Name = name
            self.ControlTypeName = "ButtonControl"
            self._children = children or []

        def GetChildren(self):
            return self._children

    def _driver(self):
        from checkin.drivers.uia_client import UiaClientDriver
        return UiaClientDriver()

    def test_lookalike_task_name_is_not_matched(self):
        drv = self._driver()
        win = self._FakeCtrl("TeleAgent 星辰超级智能体", [
            self._FakeCtrl("TeleAgent积分自动领取"),   # 任务名，绝不能被当成按钮
            self._FakeCtrl("立即领取"),
        ])
        name, ctrl = drv._find_claim(
            win,
            {"claim_button_names": ["立即领取", "今日已领"],
             "claimable_name": "立即领取", "claimed_name": "今日已领"},
            32)
        self.assertEqual(name, "立即领取")
        self.assertIsNotNone(ctrl)
        self.assertEqual(ctrl.Name, "立即领取")

    def test_claimed_state_is_recognised_without_clicking(self):
        drv = self._driver()
        win = self._FakeCtrl("TeleAgent 星辰超级智能体", [
            self._FakeCtrl("今日已领"),
        ])
        name, _ = drv._find_claim(
            win,
            {"claim_button_names": ["立即领取", "今日已领"],
             "claimable_name": "立即领取", "claimed_name": "今日已领"},
            32)
        self.assertEqual(name, "今日已领")

    def test_no_button_returns_none(self):
        drv = self._driver()
        win = self._FakeCtrl("TeleAgent 星辰超级智能体", [
            self._FakeCtrl("新建任务"),
            self._FakeCtrl("立即添加"),          # 技能市场的按钮，不能误认成签到按钮
        ])
        name, ctrl = drv._find_claim(
            win,
            {"claim_button_names": ["立即领取", "今日已领"],
             "claimable_name": "立即领取", "claimed_name": "今日已领"},
            32)
        self.assertIsNone(name)
        self.assertIsNone(ctrl)


class TestUiaLaunchGuard(unittest.TestCase):
    """窗口不在时要能自己唤起主窗口 —— 但 **probe 绝不许启动客户端**。

    2026-11-21 的事故：客户端进程一直在跑，但主窗口被关掉（缩托盘），UIA 顶层枚举里
    没有它 ⇒ 全天 10 次 `no_action / 客户端窗口未找到`，一次都没签上。
    修法是"再启动一次 exe 唤起窗口"，不是重启进程。
    """

    class _FakeAuto:
        def SetGlobalSearchTimeout(self, *_a, **_k):
            pass

    def test_recipe_enables_launch_with_a_real_path(self):
        r = _recipe("teleagent")
        self.assertIs(r.uia.get("launch_if_window_missing"), True)
        self.assertTrue(str(r.uia.get("app_path") or "").lower().endswith("teleagent.exe"),
                        "app_path 必须指向客户端 exe，否则唤起就是一纸空文")
        self.assertGreater(int(r.uia.get("launch_wait_sec") or 0), 0)

    def _driver(self):
        from checkin.drivers.uia_client import UiaClientDriver
        drv = UiaClientDriver()
        drv._import_auto = lambda: self._FakeAuto()
        drv._find_window = lambda auto, u: None
        return drv

    def test_probe_never_launches_client(self):
        """probe/dry-run 只读：宁可报"窗口不在"，也不许动用户的进程。"""
        drv = self._driver()
        calls = []
        drv._launch = lambda u: calls.append(1) or True
        res = drv.run(_recipe("teleagent"), None, dry_run=False, probe=True)
        self.assertEqual(calls, [], "probe 下不许启动客户端")
        self.assertEqual(res.outcome.value, "no_action")
        self.assertEqual(res.detail.get("reason"), "no_window")

    def test_missing_window_triggers_launch_and_reports_it(self):
        drv = self._driver()
        calls = []
        drv._launch = lambda u: calls.append(1) or True
        drv._wait_window = lambda auto, u: None
        res = drv.run(_recipe("teleagent"), None, dry_run=False, probe=False)
        self.assertEqual(len(calls), 1, "窗口不在且配方开启时必须尝试唤起")
        self.assertTrue(res.detail.get("launched"), "detail 要能看出「已经试过启动」")
        self.assertIn("已尝试启动", res.message)


class TestShortcutPortIsSingleSourceOfTruth(unittest.TestCase):
    def test_every_app_reads_the_port_from_its_own_recipe(self):
        """工具读到的端口 == 配方解析出的端口（防 9343/9345 那种错配重演）。"""
        spec = importlib.util.spec_from_file_location(
            "win_shortcut_args_ports",
            PROJECT_ROOT / "tools" / "win_shortcut_args.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for app, meta in mod.APPS.items():
            text = _recipe_text(meta["recipe"])
            m = re.search(r"^\s*debug_port:\s*(\d+)", text, re.M)
            self.assertIsNotNone(m, f"{app}: 配方里找不到 debug_port 行")
            self.assertEqual(int(m.group(1)), mod.recipe_debug_port(app),
                             f"{app}: 工具读到的端口与配方不一致")


if __name__ == "__main__":
    unittest.main()
