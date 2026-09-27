"""极简 Chrome DevTools Protocol 客户端：发现页面 -> WebSocket -> Runtime.evaluate。"""
from __future__ import annotations

import json
import logging
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

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass
