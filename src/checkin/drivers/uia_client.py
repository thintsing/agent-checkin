"""uia 驱动：用 Windows UI Automation 操作桌面客户端的**原生控件**。

## 这条路存在的理由
有个客户端（`mode: uia` 的配方）把 CDP 自己堵死了 —— 打包版
`app.commandLine.removeSwitch("remote-debugging-port"/"inspect"/...)`，
外加 `node:inspector` 反调试（命中就 `app.exit(1)`）。见该配方里的取证段。
但它的 Chromium **没有**禁用辅助功能树，所以 UIA 仍能读到真实控件
（实测读到 `ButtonControl '立即领取'`）。

## 三条实测结论（2026-11-20 真机，别凭直觉推翻）
1. `AutomationElement.FromHandle(hwnd)` 只拿到 **13 个空壳节点**（命名控件 0 个）；
   必须从 `GetRootControl()` 逐层 `GetChildren()` **才会激活**辅助功能树
   （同一窗口实测变成 127 节点 / 68 个命名控件）。
2. **坐标点击无效**：`mouse_event` 打不动 Electron 窗口（社区同名技能文档也这么记）；
   只有**控件级 `ctrl.Click()`** 有效。⇒ 本驱动不提供坐标兜底，
   宁可失败也不做"看起来点了其实没点"的动作。
3. 点「立即领取」后账户菜单会**关闭**；复核必须**重开菜单**再读按钮名。

## 判据（防"名存实亡"与假成功）
- 按钮名**精确匹配**（`立即领取` / `今日已领`）。用模糊"含领取"会误命中侧边栏
  任务名 —— 真实存在一个叫「TeleAgent积分自动领取」的任务/会话，踩过。
- `success` 的唯一依据是**复核时按钮变成 `今日已领`**；复核不到就 `no_action`，
  绝不假成功（与 client bridge 的二次复核同一原则）。

## 它不做的事
- 不重启客户端（UIA 没有"端口绑不上"这个概念；启动/拉起不属于本驱动职责）。
- 不读任何鉴权文件、不伪造身份、不改客户端二进制 —— 只用系统辅助功能通道。
"""
from __future__ import annotations

import ctypes
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

from ..core.models import CheckinResult, Outcome, Recipe
from . import Driver

log = logging.getLogger("checkin.driver.uia")

_DEFAULT_MAX_DEPTH = 32
_DEFAULT_NODE_LIMIT = 9000
_DEFAULT_SETTLE = 1.6
_DEFAULT_VERIFY_SETTLE = 2.0
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_ACCOUNT_PATTERN = r"^user_\d+$"


class UiaClientDriver(Driver):
    mode = "uia"

    # ------------------------------------------------------------------ 入口
    def run(self, recipe: Recipe, cfg, dry_run: bool = False,
            probe: bool = False) -> CheckinResult:
        u: Dict[str, Any] = recipe.uia or {}
        try:
            auto = self._import_auto()
        except RuntimeError as exc:
            return CheckinResult(recipe.id, Outcome.ERROR, message=str(exc),
                                 detail={"reason": "missing_dependency"})

        timeout = self._timeout(u, cfg)
        auto.SetGlobalSearchTimeout(float(u.get("search_timeout_sec") or 8))

        win = self._find_window(auto, u)
        if win is None:
            return CheckinResult(
                recipe.id, Outcome.NO_ACTION,
                message=f"{recipe.name} 的客户端窗口未找到（可能未启动）",
                detail={"reason": "no_window"})

        max_depth = int(u.get("max_depth") or _DEFAULT_MAX_DEPTH)
        settle = float(u.get("settle_sec") or _DEFAULT_SETTLE)
        claimable = str(u.get("claimable_name") or "立即领取")
        claimed = str(u.get("claimed_name") or "今日已领")

        # 探测/预演：只读，不抢前台、不点任何东西。
        # 先直读；读不到就**不去开菜单**（开菜单本身是一次 UI 动作）。
        if probe or dry_run:
            name, _ = self._find_claim(win, u, max_depth)
            if name == claimed:
                return CheckinResult(recipe.id, Outcome.ALREADY,
                                     message=f"今日已领（{claimed}）",
                                     detail={"button": name, "probe": True})
            if name == claimable:
                return CheckinResult(recipe.id, Outcome.NO_ACTION,
                                     message=f"当前可领取（{claimable}），预演/探测未点击",
                                     detail={"button": name, "clickable": True,
                                             "probe": True})
            return CheckinResult(
                recipe.id, Outcome.NO_ACTION,
                message="未读到签到按钮（菜单未展开，探测不主动开菜单）",
                detail={"reason": "button_not_visible", "probe": True})

        # ------------------------------------------------ 正常执行：置前 → 找按钮
        self._restore_and_front(win)
        started = time.monotonic()

        name, ctrl = self._find_claim(win, u, max_depth)
        menu_opened = False
        if name is None:
            menu_opened, account = self._open_account_menu(win, u, max_depth, settle)
            if not menu_opened:
                return CheckinResult(
                    recipe.id, Outcome.NO_ACTION,
                    message="账户菜单未打开（账号入口未找到，可能未登录或已改版）",
                    detail={"reason": "no_account_entry",
                            "account_pattern": str(u.get("account_entry_pattern")
                                                   or _DEFAULT_ACCOUNT_PATTERN)})
            log.info("[%s] 已通过 UIA 打开账户菜单（入口='%s'）", recipe.id, account)
            name, ctrl = self._find_claim(win, u, max_depth)
            if name is None:
                return CheckinResult(
                    recipe.id, Outcome.NO_ACTION,
                    message="菜单已开但读不到领取按钮（可能活动未开始或界面改版）",
                    detail={"reason": "no_button", "menu_opened": True})

        log.info("[%s] UIA 读到按钮：%s", recipe.id, name)

        if name == claimed:
            return CheckinResult(recipe.id, Outcome.ALREADY,
                                 message=f"今日已领（{claimed}），未重复点击",
                                 detail={"button": name, "menu_opened": menu_opened})

        if ctrl is None:
            return CheckinResult(
                recipe.id, Outcome.NO_ACTION,
                message="按钮控件句柄为空，放弃点击（不猜坐标）",
                detail={"reason": "no_control_handle", "button": name})

        # ---------------------------------------------------------- 点击并复核
        try:
            ctrl.Click()
        except Exception as exc:                       # noqa: BLE001 - 兜住 COM 异常
            return CheckinResult(recipe.id, Outcome.ERROR,
                                 message=f"控件点击失败：{exc}",
                                 detail={"reason": "click_failed", "button": name})

        log.info("[%s] 已执行控件级 Click（%s），等待 %.1fs 后复核",
                 recipe.id, name, float(u.get("verify_settle_sec") or _DEFAULT_VERIFY_SETTLE))
        time.sleep(float(u.get("verify_settle_sec") or _DEFAULT_VERIFY_SETTLE))

        left = timeout - (time.monotonic() - started)
        if left <= 0:
            return CheckinResult(recipe.id, Outcome.NO_ACTION,
                                 message="点击后超出本轮时间上限，未能复核（不记成功）",
                                 detail={"reason": "verify_timeout", "verified": False})

        name_after, _ = self._find_claim(win, u, max_depth)
        if name_after != claimed:
            # 菜单会自行关闭 —— 复核必须重开一次，否则永远看不到真实状态
            reopened, _acct = self._open_account_menu(win, u, max_depth, settle)
            if reopened:
                name_after, _ = self._find_claim(win, u, max_depth)

        if name_after == claimed:
            return CheckinResult(recipe.id, Outcome.SUCCESS,
                                 message=f"已点击领取并复核到账（{claimed}）",
                                 detail={"button_after": name_after, "verified": True})
        return CheckinResult(
            recipe.id, Outcome.NO_ACTION,
            message="点击后未能复核到账（按钮未变为已领，不记成功）",
            detail={"reason": "verify_failed", "verified": False,
                    "button_after": name_after})

    # ------------------------------------------------------------ 基础工具
    @staticmethod
    def _import_auto():
        """延迟导入：UIA 依赖缺失时不能把整个应用拖崩。"""
        try:
            import uiautomation as auto  # noqa: PLC0415 - 故意延迟
        except Exception as exc:                       # noqa: BLE001
            raise RuntimeError(
                f"缺少 UIA 依赖（pip install uiautomation）：{exc}") from exc
        return auto

    @staticmethod
    def _timeout(u: Dict[str, Any], cfg) -> float:
        """本轮允许花多久 —— 配方给上限，再由调度预算夹一刀。"""
        want = float(u.get("timeout_sec") or _DEFAULT_TIMEOUT)
        budget = getattr(cfg, "budget", None)
        if budget is not None and hasattr(budget, "clamp"):
            try:
                want = budget.clamp(want, cap_in_pass1=float(
                    u.get("timeout_cap_pass1_sec") or want))
            except Exception:                          # noqa: BLE001
                pass
        return max(1.0, want)

    @staticmethod
    def _find_window(auto, u: Dict[str, Any]):
        """按窗口标题子串（可选再校类名）找主窗口。

        ⚠ 不用 `FromHandle` —— 那条路只拿到窗口空壳，读不到内部控件。
        """
        want = str(u.get("window_title_contains") or "").strip()
        want_cls = str(u.get("window_class") or "").strip()
        if not want and not want_cls:
            return None
        for w in auto.GetRootControl().GetChildren():
            try:
                name = w.Name or ""
                cls = w.ClassName or ""
            except Exception:                          # noqa: BLE001
                continue
            if want and want not in name:
                continue
            if want_cls and cls != want_cls:
                continue
            return w
        return None

    @staticmethod
    def _walk(el, max_depth: int = _DEFAULT_MAX_DEPTH,
              limit: int = _DEFAULT_NODE_LIMIT):
        """深度优先遍历控件树（**这一步才会激活 Chromium 的辅助功能树**）。"""
        stack = [(el, 0)]
        seen = 0
        while stack:
            cur, depth = stack.pop()
            seen += 1
            if seen > limit:
                return
            yield cur
            if depth >= max_depth:
                continue
            try:
                children = cur.GetChildren()
            except Exception:                          # noqa: BLE001
                continue
            for child in reversed(children):
                stack.append((child, depth + 1))

    def _find_claim(self, win, u: Dict[str, Any], max_depth: int):
        """精确匹配领取按钮，返回 (name, control) 或 (None, None)。

        名字表来自配方；**不做模糊匹配**（侧边栏有「TeleAgent积分自动领取」这种任务名）。
        """
        names = [str(n) for n in (u.get("claim_button_names") or []) if str(n).strip()]
        for key in ("claimable_name", "claimed_name"):
            val = str(u.get(key) or "").strip()
            if val and val not in names:
                names.append(val)
        if not names:
            return None, None

        found: Dict[str, Any] = {}
        for el in self._walk(win, max_depth):
            try:
                nm = el.Name
            except Exception:                          # noqa: BLE001
                continue
            if nm in names and nm not in found:
                found[nm] = el
                if len(found) == len(names):
                    break

        for pref in (str(u.get("claimable_name") or ""),
                     str(u.get("claimed_name") or "")):
            if pref and pref in found:
                return pref, found[pref]
        if found:
            name = next(iter(found))
            return name, found[name]
        return None, None

    def _open_account_menu(self, win, u: Dict[str, Any], max_depth: int,
                           settle: float) -> Tuple[bool, str]:
        """点开账户菜单，返回 (是否点到了入口控件, 命中的文本)。"""
        pattern = str(u.get("account_entry_pattern") or _DEFAULT_ACCOUNT_PATTERN)
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(_DEFAULT_ACCOUNT_PATTERN)

        for el in self._walk(win, max_depth):
            try:
                nm = el.Name or ""
            except Exception:                          # noqa: BLE001
                continue
            if not nm or not rx.search(nm):
                continue
            try:
                el.Click()
            except Exception as exc:                   # noqa: BLE001
                log.warning("[%s] 账户入口控件点击失败：%s", getattr(el, "Name", ""), exc)
                return False, nm
            time.sleep(settle)
            return True, nm
        return False, ""

    @staticmethod
    def _restore_and_front(win) -> None:
        """还原并置前 —— 控件点击要求窗口可交互；失败不致命（后续会自然报错）。"""
        try:
            hwnd = win.NativeWindowHandle
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)                 # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.4)
        except Exception as exc:                       # noqa: BLE001
            log.debug("置前失败（继续尝试控件点击）：%s", exc)
