"""client 驱动：驱动**桌面客户端**自己完成签到（Qoder CN 用此模式）。

与另两个驱动的分工
------------------
  auto   —— 走 HTTP 接口（需要自己有 token，不借道客户端）
  manual —— 只提醒，不碰任何东西
  client —— 把客户端拉起来，**用它的 UI 完成签到**（复用客户端已持有的登录态与设备身份）

`client` 存在的理由：有些活动的领取渠道**只有桌面端**，且鉴权 token 由客户端主进程注入
（Web 端拿不到）。这时既不能走 HTTP，也不该退化成"只提醒"—— 正确做法是让客户端自己去点。

前置知识（踩过的坑，详见 core/procenv.py 与 HANDOFF）
----------------------------------------------------
1. **必须以干净环境启动客户端**。Agent 环境里的 `ELECTRON_RUN_AS_NODE=1` 会让 Electron
   应用退化成 Node，静默失败。→ 一律走 `core.procenv.spawn()`。
2. **要点的 UI 常常在 OOPIF 里**（独立 target，`type=="iframe"`），父页面读不到内容。
   → `CDPPage.connect(..., target_types=["page","iframe"])`。
3. **点击要用完整事件序列**，`el.click()` 对绑 `pointerdown` 的 UI 无效。

不打断用户
----------
若客户端**已在运行**（用户在写代码），我们**绝不杀它** —— 杀掉重启会打断工作，
而且重启用例下也未必能接管。此时降级为"发提醒请你手动领"。
只有"客户端没在跑"时才由我们拉起，并在领取后（默认）关掉，把环境还原。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..browser.cdp import CDPPage, list_targets
from ..core import notify, procenv
from ..core.config import expand
from ..core.models import CheckinResult, Outcome, Recipe
from ..core.state import CST
from . import Driver

log = logging.getLogger("checkin.driver.client")

_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 在目标 iframe 内查找按钮。返回 {found, disabled, claimed, body}
_JS_PROBE = r"""
(() => {
  const btns = [...document.querySelectorAll('button')];
  const b = btns.find(x => ((x.innerText||'').trim() === __LABEL__));
  const body = (document.body ? document.body.innerText : '');
  if (!b) return {found: false, body: body.slice(0, 800)};
  return {found: true, disabled: b.disabled === true ||
          b.getAttribute('aria-disabled') === 'true', body: body.slice(0, 800)};
})()
"""

# 完整 pointer/mouse 事件序列 —— 该 UI 绑 pointerdown，单靠 el.click() 不触发
_JS_CLICK = r"""
(() => {
  const b = [...document.querySelectorAll('button')].find(
    x => ((x.innerText||'').trim() === __LABEL__));
  if (!b) return 'no-button';
  b.scrollIntoView({block: 'center'});
  const r = b.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const base = {bubbles: true, cancelable: true, composed: true, view: window,
                clientX: cx, clientY: cy, screenX: cx, screenY: cy, button: 0, detail: 1};
  const seq = [
    ['pointerover',  PointerEvent, {...base, buttons: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true}],
    ['pointerenter', PointerEvent, {...base, buttons: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true}],
    ['mouseover',    MouseEvent,   {...base, buttons: 0}],
    ['pointerdown',  PointerEvent, {...base, buttons: 1, pointerId: 1, pointerType: 'mouse', isPrimary: true}],
    ['mousedown',    MouseEvent,   {...base, buttons: 1}],
    ['pointerup',    PointerEvent, {...base, buttons: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true}],
    ['mouseup',      MouseEvent,   {...base, buttons: 0}],
    ['click',        MouseEvent,   {...base, buttons: 0}]
  ];
  for (const [type, Ctor, init] of seq) b.dispatchEvent(new Ctor(type, init));
  return 'dispatched';
})()
"""

# 「把活动入口点出来」用的点击脚本。与 _JS_CLICK 只差**找元素的方式**：
# 活动页里的领取按钮有固定文案，而客户端外壳上的入口只有 aria-label 可依。
# 事件序列必须与 _JS_CLICK 完全一致 —— 同一个 UI 框架，同样绑 pointerdown。
_JS_CLICK_ARIA = r"""
(() => {
  const label = __LABEL__;
  const b = [...document.querySelectorAll('[aria-label]')].find(
    x => ((x.getAttribute('aria-label') || '')).includes(label));
  if (!b) return 'no-button';
  b.scrollIntoView({block: 'center'});
  const r = b.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const base = {bubbles: true, cancelable: true, composed: true, view: window,
                clientX: cx, clientY: cy, screenX: cx, screenY: cy, button: 0, detail: 1};
  const seq = [
    ['pointerover',  PointerEvent, {...base, buttons: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true}],
    ['pointerenter', PointerEvent, {...base, buttons: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true}],
    ['mouseover',    MouseEvent,   {...base, buttons: 0}],
    ['pointerdown',  PointerEvent, {...base, buttons: 1, pointerId: 1, pointerType: 'mouse', isPrimary: true}],
    ['mousedown',    MouseEvent,   {...base, buttons: 1}],
    ['pointerup',    PointerEvent, {...base, buttons: 0, pointerId: 1, pointerType: 'mouse', isPrimary: true}],
    ['mouseup',      MouseEvent,   {...base, buttons: 0}],
    ['click',        MouseEvent,   {...base, buttons: 0}]
  ];
  for (const [type, Ctor, init] of seq) b.dispatchEvent(new Ctor(type, init));
  return 'dispatched';
})()
"""


def _read_tail(path: str, limit: int = 300_000) -> str:
    """读文件末尾若干字节。

    campaign 记录写在日志**末尾**，而 main.log 从几百 KB 到几 MB 不等；
    逐会话全读的话，会话一多（客户端每次启动都新建一个）就会明显变慢，
    而我们要的状态一定在尾部。
    """
    size = os.path.getsize(path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        if size > limit:
            fh.seek(size - limit)
            fh.readline()          # 丢掉可能被字节截断的半行
        return fh.read()


class ClientDriver(Driver):
    mode = "client"

    # ------------------------------------------------------------ 对外入口

    def run(self, recipe: Recipe, cfg, dry_run: bool = False,
            probe: bool = False) -> CheckinResult:
        sid = recipe.id
        c: Dict[str, Any] = dict(recipe.client or {})
        if not c:
            return CheckinResult(sid, Outcome.ERROR,
                                 message="mode=client 但配方里没有 client 段")

        port = int(c.get("debug_port", 9334))
        match = str(c.get("target_match") or "")
        gate = str((recipe.reminder or {}).get("not_before") or "")   # 开窗时刻，如 "10:00"

        if dry_run or probe:
            return self._diagnose(sid, c, port, match, gate)

        # ① 零打扰预检：不启动客户端，先读客户端日志判断本窗口是否已领。
        #    定时任务最常见的场景就是"用户早就手动领过了"——这一步能省掉整次启动。
        lg = self._log_state(c, gate)
        if lg and lg.get("claimed_in_window"):
            log.info("[%s] 日志显示本窗口已领取（%s），无需启动客户端", sid, lg.get("at"))
            return CheckinResult(sid, Outcome.ALREADY,
                                 message=f"本窗口已领取（依据客户端日志 {lg.get('at')}）",
                                 detail={"source": "client_log", **lg})

        # ② 端口已开着（上次没关干净 / 用户自己带的端口）→ 直接接管
        if self._port_alive(port):
            log.info("[%s] 调试端口 %s 已就绪，直接接管", sid, port)
            res = self._claim(recipe, c, port, match)
            return res

        # ③ 客户端在运行但没有调试端口 → 不杀（会打断用户），降级提醒
        running = self._app_running(c)
        if running:
            log.warning("[%s] 客户端已在运行且未开调试端口，无法接管，降级为提醒（%s）",
                        sid, running[:120])
            self._remind(recipe)
            return CheckinResult(sid, Outcome.NO_ACTION,
                                 message="客户端正在运行，无法自动接管，已提醒你手动领取",
                                 detail={"reason": "already_running_without_debug_port"})

        # ④ 客户端没在跑 → 我们拉起来，领完再关掉
        exe = self._resolve_exe(c)
        if not exe:
            return CheckinResult(sid, Outcome.ERROR,
                                 message="找不到客户端可执行文件（检查 client.exe / client.exe_glob）")
        timeout = int(c.get("launch_timeout_sec", 60))
        log.info("[%s] 启动客户端: %s", sid, exe)
        proc = procenv.spawn([exe, f"--remote-debugging-port={port}"])
        try:
            if not self._wait_port(port, timeout):
                return CheckinResult(sid, Outcome.ERROR,
                                     message=f"客户端已启动但 {timeout}s 内调试端口未就绪",
                                     detail={"pid": proc.pid})
            return self._claim(recipe, c, port, match)
        finally:
            if bool(c.get("close_after", True)):
                self._shutdown(port)

    # ------------------------------------------------------------ 领取主流程

    def _claim(self, recipe: Recipe, c: Dict[str, Any], port: int, match: str) -> CheckinResult:
        sid = recipe.id
        find, click, markers = self._js(c)

        tgt = self._wait_target(port, match, int(c.get("target_timeout_sec", 25)))
        if tgt is None:
            # 入口没出现，有两种完全不同的成因，必须分开处理：
            #   a) 本窗口已领 → 服务端不再下发入口，再等也没用（下面的日志判定会认出来）；
            #   b) **客户端整天开着** → 入口是否自动打开，由"客户端启动那一刻"的服务端状态
            #      决定；开窗时进程早就在跑，入口于是永远不会自己出现。这是零打扰路径（②）
            #      的**结构性缺口** —— 2026-09-26 真踩到：当天没领到，靠手动补领。
            # 对 b) 主动把入口点出来再给一次机会；点不出来就退回原行为，不劣化。
            log.info("[%s] 活动入口未自动出现，尝试从客户端 UI 调出", sid)
            self._open_entry(port, c)
            tgt = self._wait_target(port, match, int(c.get("entry_timeout_sec", 20)))
        if tgt is None:
            # 到这一步是真的取不到了。最常见的原因是"本窗口已经领过了"，
            # 但也可能是活动结束/窗口未到 —— 交给人判断，不要瞎重试。
            lg = self._log_state(c, str((recipe.reminder or {}).get("not_before") or ""))
            if lg and lg.get("claimed_in_window"):
                return CheckinResult(sid, Outcome.ALREADY,
                                     message="活动入口未出现，日志显示本窗口已领取",
                                     detail=lg)
            return CheckinResult(sid, Outcome.NO_ACTION,
                                 message="活动入口未出现（已领取 / 未开窗 / 活动已结束），请人工确认",
                                 detail={"target_match": match})

        page = CDPPage(tgt["webSocketDebuggerUrl"])
        try:
            pre = page.evaluate(find, await_promise=False) or {}
            body = str(pre.get("body") or "")

            if not pre.get("found"):
                if any(m in body for m in markers):
                    return CheckinResult(sid, Outcome.ALREADY, message="界面显示本窗口已领取",
                                         detail={"body": body[:200]})
                if "登录" in body:
                    return CheckinResult(sid, Outcome.NEED_LOGIN,
                                         message="客户端未登录，请先登录后重试",
                                         detail={"body": body[:200]})
                return CheckinResult(sid, Outcome.NO_ACTION,
                                     message="活动页里找不到领取按钮（客户端可能已升级改版）",
                                     detail={"body": body[:300]})

            if pre.get("disabled"):
                return CheckinResult(sid, Outcome.ALREADY,
                                     message="领取按钮已置灰（本窗口已领取）",
                                     detail={"body": body[:200]})

            dispatched = page.evaluate(click, await_promise=False)
            if dispatched != "dispatched":
                return CheckinResult(sid, Outcome.ERROR,
                                     message=f"点击派发失败: {dispatched}")
            time.sleep(3.0)

            post = page.evaluate(find, await_promise=False) or {}
            post_body = str(post.get("body") or "")
            confirmed = any(m in post_body for m in markers)
            detail = {"click": "js-pointer-sequence", "confirmed": confirmed,
                      "body": post_body[:300]}
            if confirmed:
                return CheckinResult(sid, Outcome.SUCCESS, message="已点击领取并确认到账", detail=detail)
            # 点下去了但读不到确认文案：**不能报成功**。宁可报"不确定"，让人去看一眼。
            return CheckinResult(sid, Outcome.NO_ACTION,
                                 message="已点击领取，但未读到确认文案，请人工核对",
                                 detail=detail)
        finally:
            page.close()

    # ------------------------------------------------------------ 状态探测

    def _diagnose(self, sid: str, c: Dict[str, Any], port: int, match: str,
                  gate: str) -> CheckinResult:
        parts: List[str] = []
        detail: Dict[str, Any] = {}

        port_up = self._port_alive(port)
        parts.append(f"端口{port}={'开' if port_up else '关'}")
        detail["port_alive"] = port_up

        if port_up:
            tgt = self._wait_target(port, match, 3)
            detail["target_present"] = bool(tgt)
            parts.append(f"活动入口={'在' if tgt else '不在'}")
            if tgt:
                find, _, _ = self._js(c)
                page = CDPPage(tgt["webSocketDebuggerUrl"])
                try:
                    info = page.evaluate(find, await_promise=False) or {}
                    detail["button_found"] = bool(info.get("found"))
                    detail["body"] = str(info.get("body") or "")[:400]
                    parts.append(f"领取按钮={'可点' if info.get('found') else '无'}")
                finally:
                    page.close()
        else:
            running = self._app_running(c)
            detail["app_running"] = bool(running)
            parts.append(f"客户端进程={'在跑' if running else '未跑'}")
            lg = self._log_state(c, gate)
            if lg:
                detail["log"] = lg
                parts.append(f"日志claimable={lg.get('claimable')}@{lg.get('at')}")

        exe = self._resolve_exe(c)
        detail["exe"] = exe
        parts.append(f"exe={'找到' if exe else '缺失'}")
        return CheckinResult(sid, Outcome.NO_ACTION,
                             message="[诊断] " + " · ".join(parts), detail=detail)

    def _log_state(self, c: Dict[str, Any], gate: str) -> Optional[Dict[str, Any]]:
        """从客户端主进程日志里读服务端最近一次下发的 campaign 状态。

        为什么值得做：日志是**持久**的，不启动客户端就能判断"本窗口是否已领取"。
        定时任务绝大多数时候遇到的就是"用户已经手动领过了"，这一步能直接省掉启动。

        日志行样例（时间戳为 UTC）：
            [2026-09-25T03:52:12.375Z] [INFO] [main] [Campaign] 活动状态响应解析完成
              {"normalizationResult":"accepted","durationMs":72,"showCampaign":true,"claimable":false}

        **为什么要扫很多个会话（而不是只看最近一两个）**：
        客户端每次启动都会新建一个 session 目录，而**"启动即关"的短会话根本不会写
        campaign 行**。实测踩过：在窗口期内跑几次验证就攒下 6 个这样的空会话，
        把真正带状态的会话挤出了"只扫前 5 个"的窗口，于是预检静默失效 ——
        表现为"配置没问题、索引也测过是对的，但就是不生效"。
        用户随手开一下客户端再关掉，同样会制造这种空会话。
        ⇒ 所以按时间倒序**跳过空会话继续找**，直到找到有 campaign 行的那一个。
        """
        dd = str(c.get("data_dir") or "")
        if not dd:
            return None
        logs = os.path.join(expand(dd), "logs")
        if not os.path.isdir(logs):
            return None
        try:
            sessions = sorted((d for d in os.listdir(logs)
                               if os.path.isdir(os.path.join(logs, d))), reverse=True)
        except OSError:
            return None
        for name in sessions[:40]:
            f = os.path.join(logs, name, "main.log")
            if not os.path.isfile(f):
                continue
            try:
                text = _read_tail(f)
            except OSError:
                continue
            last = None
            for ln in text.splitlines():
                if "[Campaign]" not in ln or "解析完成" not in ln:
                    continue
                m = re.search(r'"claimable":\s*(true|false)', ln)
                if not m:
                    continue
                ts = re.match(r"\[(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\]", ln)
                at = None
                if ts:
                    try:
                        at = (datetime.fromisoformat(ts.group(1).replace("Z", "+00:00"))
                              .astimezone(CST))
                    except ValueError:
                        at = None
                last = {"claimable": m.group(1) == "true",
                        "at": at.strftime("%Y-%m-%d %H:%M:%S") if at else None,
                        "_at_dt": at, "session": name}
            if last:
                last["claimed_in_window"] = self._claimed_in_window(
                    last.get("claimable"), last.get("_at_dt"), gate)
                last.pop("_at_dt", None)
                return last
        return None

    @staticmethod
    def _claimed_in_window(claimable: Optional[bool], at: Optional[datetime],
                           gate: str) -> bool:
        """日志这条记录是否足以断定"**当前活动窗口**内已领取"。

        光看 `claimable=false` 不够 —— 它可能来自上一个窗口的陈旧记录。
        必须同时确认记录时刻落在当前窗口内：窗口起点 = 今天的开窗时刻，
        若现在还没到开窗时刻，则起点回退到昨天。
        """
        if claimable is not False or at is None:
            return False
        hh, _, mm = (gate or "10:00").partition(":")
        try:
            g_h, g_m = int(hh), int(mm or 0)
        except ValueError:
            g_h, g_m = 10, 0
        now = datetime.now(CST)
        start = now.replace(hour=g_h, minute=g_m, second=0, microsecond=0)
        if now < start:
            start -= timedelta(days=1)
        return at >= start

    # ------------------------------------------------------------ 进程/端口

    def _app_running(self, c: Dict[str, Any]) -> str:
        r"""返回主进程的命令行（空串 = 没在跑）。

        两个必须的过滤，都是实测踩出来的：

        1) **必须按进程名过滤，不能只按 CommandLine 匹配。**
           查询脚本本身会作为 `powershell.exe` 的命令行参数存在，里面含
           `*Qoder CN.exe*` 这个字面量 —— 于是"匹配 CommandLine"会**匹配到
           powershell.exe 自己**。真踩过：驱动因此永远认为"客户端在运行"，
           永远走"降级提醒"分支，**从不自动领取**（配好了也是白配）。
        2) **必须排除 native-messaging-host 子进程**。它们的命令行是
           `xxx.exe "...\qoder-app-host.cjs"`，常年由浏览器扩展拉起，
           和主进程不是一回事 —— 不排除的话"没开客户端"也会被判成"在运行"。
        """
        name = str(c.get("process_match") or "").strip()
        if not name:
            exe = self._resolve_exe(c)
            name = os.path.basename(exe) if exe else ""
        if not name:
            return ""
        script = self._app_running_script(name)
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=25)
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("进程查询失败：%s", e)
            return ""
        for line in (out.stdout or "").splitlines():
            ln = line.strip()
            if ln:
                return ln
        return ""

    @staticmethod
    def _app_running_script(name: str) -> str:
        """构造"这台机器上主进程在不在"的 PowerShell（抽成纯函数是为了可测）。"""
        safe = name.replace("'", "''")
        return (
            f"Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.Name -eq '{safe}' -and $_.ProcessId -ne $PID -and "
            f"$_.CommandLine -notlike '*app-host.cjs*' -and "
            f"$_.CommandLine -notlike '*--type=*' }} | "
            f"ForEach-Object {{ $_.CommandLine }}"
        )

    @staticmethod
    def _port_alive(port: int) -> bool:
        try:
            with _LOCAL_OPENER.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                r.read()
            return True
        except Exception:
            return False

    def _wait_port(self, port: int, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._port_alive(port):
                return True
            time.sleep(0.8)
        return False

    @staticmethod
    def _wait_target(port: int, match: str, timeout: int) -> Optional[Dict[str, Any]]:
        """等活动入口 target 出现。

        入口是 OOPIF（`type=="iframe"`），**只在服务端 claimable=true 时**才会被客户端
        自动打开；领取成功后这个 target 会整个消失。所以它出现后要尽快操作。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                for t in list_targets(port, ["page", "iframe"]):
                    if match and match in (t.get("url") or ""):
                        return t
            except Exception as e:
                log.debug("列举 target 失败：%s", e)
            time.sleep(0.7)
        return None

    @staticmethod
    def _main_target(port: int, hint: str) -> Optional[Dict[str, Any]]:
        """取客户端**主窗口**（外壳 UI）的 target。

        入口点击必须在主文档里做：活动页自己是 OOPIF，独立上下文里
        `window.parent === window` ⇒ 它会忽略所有消息（详见 recipes 头注释），
        所以在活动页里面操作等于白做。
        """
        if not hint:
            return None
        try:
            for t in list_targets(port, ["page"]):
                if hint in (t.get("url") or ""):
                    return t
        except Exception as e:                                  # noqa: BLE001
            log.debug("列举 target 失败：%s", e)
        return None

    def _open_entry(self, port: int, c: Dict[str, Any]) -> None:
        """从客户端外壳把活动入口**点出来**（入口平时藏在用量面板后面）。

        为什么必须做：入口是否在启动时自动打开，由**启动那一刻**的服务端状态决定。
        客户端整天开着（实测 uptime 13 小时）时，开窗后入口不会自己出现 ——
        驱动干等一轮 timeout 只能报 no_action，**当天就白跑了**。

        要素全部来自配方（`open_entry_labels` / `main_target_match`），
        代码里不出现产品名 —— 这是本项目的分层约定：站点知识只进 recipes/*.yaml。
        任何一步失败都只记日志、不抛异常：点不出来就退回原来的 no_action，行为不劣化。
        """
        labels = list(c.get("open_entry_labels") or [])
        if not labels:
            return
        main = self._main_target(port, str(c.get("main_target_match") or ""))
        if main is None:
            log.debug("找不到客户端主窗口 target，跳过入口点击")
            return
        try:
            delay = float(c.get("entry_delay_sec", 1.5))
        except (TypeError, ValueError):
            delay = 1.5
        page = CDPPage(main["webSocketDebuggerUrl"])
        try:
            for label in labels:
                js = _JS_CLICK_ARIA.replace(
                    "__LABEL__", json.dumps(str(label), ensure_ascii=False))
                try:
                    r = page.evaluate(js, await_promise=False)
                except Exception as e:                          # noqa: BLE001 —— 点不到不能阻断领取
                    log.debug("点击入口 %r 失败：%s", label, e)
                    continue
                log.info("点击入口 %r → %s", label, r)
                time.sleep(delay)
        finally:
            page.close()

    def _shutdown(self, port: int) -> None:
        """用 CDP `Browser.close` 优雅关闭**我们刚启动的**实例。

        注意：这里**没有**"按进程名强杀"的回退（旧注释曾这么写，与实现不符）。
        这是刻意的 —— 项目铁律是"绝不杀用户的客户端"，只关我们自己拉起来的那个。
        因此端口不通时（例如启动被单实例锁顶掉、或端口从没开起来）本函数**静默无操作**，
        不会误伤任何正在运行的用户进程。
        """
        conn = None
        try:
            with _LOCAL_OPENER.open(f"http://127.0.0.1:{port}/json/version", timeout=4) as r:
                ws_url = json.loads(r.read().decode("utf-8")).get("webSocketDebuggerUrl")
            if ws_url:
                import websocket
                conn = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
                conn.send(json.dumps({"id": 1, "method": "Browser.close"}))
                try:
                    conn.settimeout(5)
                    while True:
                        conn.recv()
                except Exception:
                    pass
        except Exception as e:
            log.debug("Browser.close 失败：%s", e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------------ 杂项

    def _resolve_exe(self, c: Dict[str, Any]) -> str:
        """定位可执行文件。优先 `exe_glob`（版本目录会滚动，glob 能自动跟上）。"""
        g = str(c.get("exe_glob") or "")
        if g:
            hits = glob.glob(expand(g))
            if hits:
                return max(hits, key=self._version_key)
        e = str(c.get("exe") or "")
        return expand(e) if e else ""

    @staticmethod
    def _version_key(path: str) -> tuple:
        m = re.search(r"(\d+(?:\.\d+)*)", path)
        return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)

    @staticmethod
    def _js(c: Dict[str, Any]):
        label = json.dumps(str(c.get("claim_button") or "领取"), ensure_ascii=False)
        markers = list(c.get("claimed_markers") or ["已领取", "领取成功"])
        return (_JS_PROBE.replace("__LABEL__", label),
                _JS_CLICK.replace("__LABEL__", label),
                markers)

    @staticmethod
    def _remind(recipe: Recipe) -> None:
        rem = recipe.reminder or {}
        notify._toast(rem.get("toast_title") or f"{recipe.name} 提醒",
                      rem.get("toast_body") or "请手动完成今日签到。")
