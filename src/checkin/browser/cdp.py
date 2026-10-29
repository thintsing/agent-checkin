"""极简 Chrome DevTools Protocol 客户端：发现页面 -> WebSocket -> Runtime.evaluate。"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import websocket  # websocket-client

log = logging.getLogger("checkin.cdp")

# 同 launcher：本地回环必须绕过代理。
# 环境里的 HTTP_PROXY（且无 NO_PROXY）会把 127.0.0.1 也代理走，DevTools HTTP
# 端点因此回 502。系统代理的注册表例外名单救不了这里 —— 详见 launcher.py 的注释。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(port: int, path: str) -> Any:
    with _LOCAL_OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def list_page_targets(port: int) -> List[Dict[str, Any]]:
    return [t for t in _http_json(port, "/json") if t.get("type") == "page"]


def list_targets(port: int, types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """列出调试端口上的 target；`types=None` 表示不过滤。

    为什么需要它：有些要操作的 UI **不在主渲染进程里**，而是在一个
    **OOPIF（out-of-process iframe）** 中 —— 它以独立 target 的形式出现在 `/json` 里
    （`type == "iframe"`）。此时在父页面里 `querySelector` 只能看到 `<iframe>` 元素本身，
    读不到内容，必须直连它自己的 `webSocketDebuggerUrl`。
    """
    items = _http_json(port, "/json")
    if types is None:
        return list(items)
    return [t for t in items if t.get("type") in types]


class CDPPage:
    """连接到一个页面 target，提供 evaluate（支持 await Promise）。"""

    def __init__(self, ws_url: str):
        self._ws = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
        self._id = 0

    @classmethod
    def connect(cls, port: int, url_match: Optional[List[str]] = None,
                target_types: Optional[List[str]] = None) -> "CDPPage":
        """附着到匹配的 target。

        `target_types` 默认 `["page"]`（保持既有行为）。要接管 OOPIF 里的 UI 时传
        `["page", "iframe"]`，并让 `url_match` 命中那个 iframe 的 URL。
        """
        pages = list_targets(port, target_types or ["page"])
        if not pages:
            raise RuntimeError("调试端口上没有可附着的 target")
        chosen = None
        if url_match:
            for t in pages:
                if any(m in (t.get("url") or "") for m in url_match):
                    chosen = t
                    break
        chosen = chosen or pages[0]
        log.debug("附着 target: [%s] %s", chosen.get("type"), chosen.get("url"))
        return cls(chosen["webSocketDebuggerUrl"])

    def _cmd(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 出错: {msg['error']}")
                return msg.get("result", {})

    def navigate(self, url: str) -> None:
        self._cmd("Page.enable")
        self._cmd("Page.navigate", {"url": url})

    def evaluate(self, expression: str, await_promise: bool = True) -> Any:
        res = self._cmd("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        })
        exc = res.get("exceptionDetails")
        if exc:
            raise RuntimeError(f"页面脚本异常: {exc.get('text')}")
        return res.get("result", {}).get("value")

    def current_url(self) -> str:
        return str(self.evaluate("location.href", await_promise=False) or "")

    def wake(self) -> None:
        """把被 Chromium 冻结的后台页恢复为 active。

        为什么必须做（2026-09-27 实测，WorkBuddy 连续两天失败的真凶）：

        Chromium 会把**长时间处于后台/隐藏**的标签页冻结（Page Lifecycle → frozen），
        页面的 Task Queue 被挂起。此时：
          - 同步的 `Runtime.evaluate`（如 `1+1`、`document.visibilityState`）**照常返回**；
          - 但任何 `await` 的 Promise（例如页面里的 `fetch`）**永远不 resolve**。
        于是 CDP 侧看起来就是"WebSocket 读超时"，极易被误判成网络故障。

        本项目专用浏览器是**长驻**的（为了保住登录态、避免每天弹窗），
        所以它会一直被后台冻结 —— 只要复用它，签到必然超时。
        （2026-09-26 10:27 启动的实例，到 09-27 已冻结约 25 小时。）

        `Page.setWebLifecycleState("active")` 能立即解冻；实测解冻后同一个 fetch
        由"超时"变成 **0.1s 返回 200**。注意：`visibilityState` 仍是 hidden，
        那是"标签是否可见"，与"生命周期是否冻结"是两回事 —— 别拿它当判据。
        """
        try:
            self._cmd("Page.setWebLifecycleState", {"state": "active"})
            return
        except Exception as e:
            log.debug("setWebLifecycleState 失败，退回 bringToFront: %s", e)
        try:
            self._cmd("Page.bringToFront")
        except Exception as e:
            log.debug("唤醒页面失败（忽略，由上层超时兜底）: %s", e)

    def press_escape(self) -> None:
        """发一次真实 Escape 键事件（keyDown + keyUp，走 Input 域 = 受信任事件）。

        为什么需要（2026-10-29）：客户端外壳的**弹出层**（用户菜单 / 用量面板）
        是 toggle 语义 —— 上一轮尝试把菜单留在打开态时，这一轮再点同一个入口
        会把它**关上**，后面的菜单项就全都不存在了。每轮入口点击前先 ESC 归零；
        幂等：没有弹出层时它什么也不做。
        """
        for params in (
            {"type": "keyDown", "key": "Escape", "code": "Escape",
             "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27},
            {"type": "keyUp", "key": "Escape", "code": "Escape",
             "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27},
        ):
            try:
                self._cmd("Input.dispatchKeyEvent", params)
            except Exception as e:                          # noqa: BLE001
                log.debug("dispatchKeyEvent 失败（忽略）: %s", e)

    def reload(self, wait_sec: float = 3.0) -> bool:
        """重新加载当前页面（让页面重新向服务端要一次状态）。

        为什么需要（2026-10-06 实测，代价是**漏签一整天**）：

        客户端里的活动 Surface **一旦被打开就会一直留着** —— 我们只断 WebSocket
        （`close()`），从不关闭那个 UI。于是它的 DOM 停在**打开那一刻**的状态。
        跨天后服务端已刷新到新窗口，页面却不会自动重载 ⇒ 靠读 DOM 判
        "是否已领取"会拿到**陈旧快照**，把"今天能领"误判成"今天已领"
        （实测：当日 10:45 报 already，而客户端日志同日 10:44 起 `claimable`
         一直为 true，直到 15:31 手动领取后才转 false）。

        重载是让快照重新新鲜的**最直接**手段：它把"服务端当下的答案"重新下发一遍，
        既不需要额外的接口，也不依赖日志结构。

        ⚠ 走页面内的 `location.reload()`，**不是** `Page.reload` ——
        后者实测在 OOPIF 上直接报错：`Command can only be executed on top-level
        targets`（活动页恰恰是 OOPIF，2026-10-06 踩到）。而 `location.reload()`
        是在该 iframe 自己的上下文里执行，不受 top-level 限制，
        也不会波及宿主应用（不可能去重载整个 IDE 外壳 —— 那会打断用户）。

        失败只返回 False（调用方沿用原判定），不抛异常 —— 复核失败不该劣化主流程。
        """
        try:
            self.evaluate("location.reload()", await_promise=False)
        except Exception as e:
            log.debug("页面重载失败（忽略，调用方沿用原判定）: %s", e)
            return False
        time.sleep(wait_sec)
        return True

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def _browser_ws(port: int) -> str:
    try:
        v = _http_json(port, "/json/version")
    except Exception as e:
        log.debug("取 browser ws 失败：%s", e)
        return ""
    return str(v.get("webSocketDebuggerUrl") or "")


def open_target(port: int, url: str, wait_sec: float = 8.0,
                poll: float = 0.3) -> Optional[Dict[str, Any]]:
    """在客户端自己的 profile 里**新开一个页面**并等它可用；失败返回 None。

    为什么需要它（2026-10-21 接入豆包工作时被逼出来的）：
    有些签到入口只在**客户端内置浏览器**里（`https://…/pc/agent-explore` 这类活动页），
    平时并不存在 —— 计划任务跑的时候没有人替我们点开菜单。而 `Target.createTarget`
    能在**同一个 profile（同一份登录态）**里开一个页面，用完关掉：
      · 不必点任何 UI（那个菜单是 Radix 组件，合成 click 无效，只能发真实鼠标事件，
        而坐标依赖窗口尺寸，脆得很）；
      · 不必赌"用户此刻正好开着那个标签页"；
      · 也不去动用户已经打开的那个标签页（我们只关自己开的这个）。

    ⚠ 必须按 **targetId** 认领自己开的那个：同一个 URL 上可能同时存在用户已开的
    webview target，靠 `url_match` 会认错人，进而把用户的标签页关掉。
    """
    wsurl = _browser_ws(port)
    if not wsurl:
        return None
    tid = ""
    try:
        ws = websocket.create_connection(wsurl, timeout=20, suppress_origin=True)
    except Exception as e:
        log.debug("连 browser ws 失败：%s", e)
        return None
    try:
        ws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                            "params": {"url": url}}))
        deadline = time.time() + 15
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                tid = str((msg.get("result") or {}).get("targetId") or "")
                if not tid:
                    log.debug("Target.createTarget 失败：%s", msg.get("error"))
                break
    except Exception as e:
        log.debug("Target.createTarget 异常：%s", e)
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    if not tid:
        return None
    # 等它出现在 /json 里，然后**再让页面安静一会儿**。
    # 这里刻意**不**自己开一条 CDP 连接去探 `document.readyState`：试过，探测连接关闭后
    # 正式连接会立刻被掐（`WinError 10053`），三次重试全废。宁可多等 3 秒。
    # 刚 createTarget 出来的页面还在导航，此刻 evaluate 进去的表达式可能连执行上下文
    # 都没建好 —— 表现是"kick 发出去了、槽位永远等不到"，直到 bridge 超时
    # （2026-10-21 实测第一次白等 25s，第二次才成）。
    tgt = None
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        try:
            for t in list_targets(port):
                if t.get("id") == tid and t.get("webSocketDebuggerUrl"):
                    tgt = t
                    break
        except Exception:
            pass
        if tgt is not None:
            break
        time.sleep(poll)
    if tgt is None:
        log.debug("新建的 target %s 在 %.1fs 内没有出现", tid, wait_sec)
        return None
    time.sleep(3.0)
    return tgt


def close_target(port: int, target_id: str) -> bool:
    """关掉 `open_target` 开出来的那个页面。失败只记日志 —— 关不掉不该劣化主流程。"""
    wsurl = _browser_ws(port)
    if not wsurl or not target_id:
        return False
    try:
        ws = websocket.create_connection(wsurl, timeout=15, suppress_origin=True)
    except Exception as e:
        log.debug("关标签页时连不上 browser ws：%s", e)
        return False
    try:
        ws.send(json.dumps({"id": 1, "method": "Target.closeTarget",
                            "params": {"targetId": target_id}}))
        deadline = time.time() + 8
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return True
    except Exception as e:
        log.debug("Target.closeTarget 异常：%s", e)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return False
