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

不打断用户（2026-10-14 收窄为一处例外）
----------------------------------------
若客户端**已在运行**（用户在写代码），默认**不动它** —— 杀进程重启会打断工作。
只有"客户端没在跑"时才由我们拉起，并在领取后（默认）关掉，把环境还原。

⚠ **唯一例外**：端口**绑定失败**（命令行已带 `--remote-debugging-port=<本端口>`、
端口却不存在）是**已知不会自愈**的死局 —— 守候再久也没用（`listen()` 失败后客户端
会静默继续跑一整条生命周期）。此时若配方开启 `restart_on_bind_failure`，驱动会
**自行重启客户端并当场重试**（用户 2026-10-14 明确授权："该重启重启，不要摆着"）。
边界：只此一种场景、且必须配方显式开启；实现见 `_restart_client`。
"""
from __future__ import annotations

import ctypes
import glob
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from ..browser.cdp import CDPPage, close_target, list_targets, open_target
from ..core import notify, procenv
from ..core.config import expand
from ..core.models import CheckinResult, Outcome, Recipe, RunBudget
from ..core.state import CST
from . import Driver

log = logging.getLogger("checkin.driver.client")

_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 在目标 iframe 内查找按钮。返回 {found, ready, disabled, body}
#
# `ready`（readyState === 'complete'）是给「陈旧快照复核」用的：
# 重载后页面若已加载完却仍没有按钮，那就是服务端的最终答案，可以立刻收手 ——
# 否则"今天确实已领"这种**常态**每天都要白等满复核超时。
_JS_PROBE = r"""
(() => {
  const btns = [...document.querySelectorAll('button')];
  const b = btns.find(x => ((x.innerText||'').trim() === __LABEL__));
  const body = (document.body ? document.body.innerText : '');
  return {found: !!b, ready: document.readyState === 'complete',
          disabled: b ? (b.disabled === true || b.getAttribute('aria-disabled') === 'true') : null,
          body: body.slice(0, 800)};
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


# 「回到主工作区」用的点击脚本：按 innerText **精确**匹配。
# 与 `_JS_CLICK_ARIA` 的唯一区别是**找元素的方式** —— 客户端的「返回应用」按钮
# 只有文本、**没有 aria-label**（2026-10-11 实测），aria 那一版够不到它。
# 事件序列必须与 `_JS_CLICK` 完全一致（同一 UI 框架，同样绑 pointerdown）。
_JS_CLICK_TEXT = r"""
(() => {
  const label = __LABEL__;
  const b = [...document.querySelectorAll('button,[role="button"],a')].find(
    x => ((x.innerText || '') + '').trim() === label);
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


def _float_cfg(c: Dict[str, Any], key: str, fallback: float) -> float:
    """从配方 `client` 段读一个浮点超时/延迟，坏值一律退回默认。

    配方是**人写的 YAML**，写成 `1,5`、`"3s"` 或留空都可能发生；
    这里绝不让它把整条领取流程炸掉 —— 读不出就用默认值继续。
    """
    try:
        return float(c.get(key, fallback))
    except (TypeError, ValueError):
        return float(fallback)


def _claimable(snapshot: Dict[str, Any]) -> bool:
    """快照是否表示"现在就能点领取"：按钮**存在**且**未置灰**。

    刻意只看按钮本身，不把页面文本（"已领取" / "领取成功"）当作"能领"的判据 ——
    那是**渲染快照**，可能是跨天陈旧的。为什么这件事代价很大、
    以及为什么必须先复核再判 already，见 `ClientDriver._recheck_stale`。
    """
    return bool(snapshot.get("found")) and not snapshot.get("disabled")


class ClientDriver(Driver):
    mode = "client"

    # ------------------------------------------------------------ 对外入口

    def run(self, recipe: Recipe, cfg, dry_run: bool = False, probe: bool = False,
            **_) -> CheckinResult:
        """⚠ 本签名被 AGENTS.md 冻结为跨 Agent 接缝，不能加具名参数。

        "本轮还能等多久"（`RunBudget`）由引擎挂在 `cfg.budget` 上带下来 ——
        这样既守住了冻结接口，又让 client 驱动能知道整轮还剩多少时间。
        """
        sid = recipe.id
        c: Dict[str, Any] = dict(recipe.client or {})
        if not c:
            return CheckinResult(sid, Outcome.ERROR,
                                 message="mode=client 但配方里没有 client 段")

        # `budget` 是**整轮共享的时间预算**（引擎挂在 cfg.budget 上带下来）。
        # None = 老调用方/手工调用，行为与过去完全一致（守候多久全听配方）。
        budget = getattr(cfg, "budget", None)
        if not isinstance(budget, RunBudget):
            budget = None
        cap = self._first_pass_cap(cfg) if budget is not None else None

        port = int(c.get("debug_port", 9337))   # 兜底值仅在配方缺字段时生效；真实值一律读配方
        match = str(c.get("target_match") or "")
        gate = str((recipe.reminder or {}).get("not_before") or "")   # 开窗时刻，如 "10:00"

        if dry_run or probe:
            return self._diagnose(recipe, c, port, match, gate)

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

        # ③ 客户端在运行但没有调试端口 —— **本项目的结构性缺口**。
        #    调试端口只能在启动时绑定，而客户端是用户全天在用的主力工具，
        #    可能连续数天不重启 ⇒ 一旦某次它以无端口形态起来，之后**每天**都静默漏签。
        #    2026-10-10 就是这个成因：10-09 13:15 启动的实例一路跑到次日都没重启，
        #    当天自动签到只能降级为"提醒"，实际没领到。
        #
        #    默认不杀它（那会打断用户手头的活）——先**按配方守候端口**：
        #    用户中途自己重启了客户端，端口一出现我们立刻接管补领。
        #    `wait_port_sec` 缺省 0 = 不守候，保持原行为（对 Qoder 等无此场景的站点无影响）。
        running = self._app_running(c)
        if running:
            # 客户端在跑但端口不通。**必须先分清是哪种"不通"** —— 成因与对策都不同，
            # 混为一谈就会白等（2026-10-14 真漏签一天：守候 90 分钟，端口根本没可能出现）。
            port_arg = "--remote-debugging-port=" + str(port)
            if port_arg in running:
                # 命令行**已带**本端口参数却连不上 ⇒ 那次启动的**端口绑定失败**。
                # 详见 `_resume_broken_port` —— 那里还要再分「僵尸」与「没绑上」。
                return self._resume_broken_port(recipe, cfg, c, port, match,
                                                budget, cap)

            # ③-b 客户端在跑，但**压根没带端口参数** —— 无端口形态起来的实例
            #     （开机自启 / 客户端升级后自重启 / 修好快捷方式之前起的那一次）。
            #     同样不会自愈：调试端口只能在**启动时**绑定，运行中加不上去。
            #   ⇒ 只做两件有用的事：① 修好启动入口，让它**下次**启动就带端口；
            #     ② 在预算内守候（用户中途重启客户端我们就接管）；仍未得了就**立刻让位**
            #     （后面还有别的站点等着跑，见 models.RunBudget）。
            self._repair_launch_entry(sid, c, cfg)
            wait_sec = self._wait_budget(c, cfg, "wait_port_sec", budget, cap)
            if wait_sec >= 1:
                log.warning("[%s] 客户端在运行但未开调试端口（%s）；按预算守候端口最多 %ds",
                            sid, running[:100], wait_sec)
                if self._wait_port(port, wait_sec, poll=10):
                    log.info("[%s] 端口 %s 已出现（客户端已被重启），接管补领", sid, port)
                    return self._claim(recipe, c, port, match)
                log.warning("[%s] 守候 %ds 结束，端口始终未出现", sid, wait_sec)
            # 走到这里说明"守候没等到"。是否**自行重启**由配方显式授权决定
            # （`restart_when_portless`），且仍要过"忙闲关" —— 详见 AGENTS.md 铁律 7。
            if bool(c.get("restart_when_portless")) and self._safe_to_restart(
                    recipe, c, port, match, budget, cap,
                    idle_fallback=lambda: self._cpu_busy(c)):
                if self._restart_client(c, port):
                    log.info("[%s] 已自动重启客户端且端口 %s 就绪，接管补领", sid, port)
                    return self._claim(recipe, c, port, match)
            log.warning("[%s] 客户端已在运行且未开调试端口，无法接管，降级为提醒（%s）",
                        sid, running[:120])
            self._remind(recipe)
            return CheckinResult(sid, Outcome.NO_ACTION,
                                 message="客户端正在运行，无法自动接管，已提醒你手动领取",
                                 detail={"reason": "already_running_without_debug_port",
                                         "waited_sec": wait_sec,
                                         "pass": budget.pass_index if budget else 0})

        # ④ 客户端**没在跑** → 我们拉起来，领完再关掉（`close_after` 默认 true）。
        #
        # ⚠ 2026-10-20 修：这一段原本**错位**在 `_resume_broken_port()` 里、位于一个
        #   无条件 `return` 之后 ⇒ 从未执行过，且让 run() 落到函数末尾返回 `None`。
        #   后果不是"少一个分支"，而是**整轮炸掉**：引擎 `last.outcome` 抛 AttributeError
        #   ⇒ 进程退出码 1 ⇒ 后面的站点全不跑、通知一条都发不出去。
        #   触发条件极其常见：用户晚上把客户端关了。是最典型的静默失败。
        exe = self._resolve_exe(c)
        if not exe:
            return CheckinResult(sid, Outcome.ERROR,
                                 message="找不到客户端可执行文件（检查 client.exe / client.exe_glob）")
        timeout = int(c.get("launch_timeout_sec", 60))
        exe = self._ensure_launchable(exe)
        log.info("[%s] 客户端未运行，启动客户端: %s", sid, exe)
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

    def _resume_broken_port(self, recipe: Recipe, cfg, c: Dict[str, Any], port: int,
                            match: str, budget: Optional[RunBudget],
                            cap: Optional[float]) -> CheckinResult:
        """命令行已带 `--remote-debugging-port=<本端口>`、端口却连不上时的处置。

        成因（2026-10-14 实测，代价是漏签一天）：用户重启客户端时，旧实例
        `11:54:28` 才 `app.quit()`、新实例 `11:54:29` 就起来了 —— 相隔 1 秒，
        旧实例还没把 9338 释放完，于是新实例 `listen()` 失败后**静默继续跑**
        （不崩、不退出、也不再重试）。此后那个实例整个生命周期都没有端口
        ⇒ **守候是无用的**，端口不会自己出现，除非再重启一次客户端。

        但仍先给一小段静默期：客户端可能刚起来、还没 listen 完，
        那一刻判"绑定失败"会是误判。

        处置顺序：**先分清两种"不通"**（僵尸 vs 未绑定），再决定是否自行重启：
          · 僵尸 socket（LISTENING 记在**已死 PID** 名下）⇒ 本端口已经拿不回来，
            必须**换端口**，否则重启多少次都是同一个死局；
          · 单纯没绑上 ⇒ 用原端口重启即可。
        """
        sid = recipe.id
        if self._wait_port(port, self._PORT_BIND_GRACE_SEC, poll=1.0):
            log.info("[%s] 端口 %s 延迟就绪（客户端刚启动），接管补领", sid, port)
            return self._claim(recipe, c, port, match)

        # 静默期过了仍不通 ⇒ listen() 失败，**不会自愈**。
        # 用户 2026-10-14 授权"该重启重启" ⇒ 配方开启时自动重启并当场重试，
        # 但**重启前必须先过"忙闲"关**（详见 `_safe_to_restart`）。
        if bool(c.get("restart_on_bind_failure")) and self._safe_to_restart(
                recipe, c, port, match, budget, cap,
                idle_fallback=lambda: self._cpu_busy(c)):
            use = port
            if self._port_is_zombie(port):
                alt = self._reserve_clean_port(sid, port, cfg)
                if alt:
                    log.warning("[%s] 端口 %s 已被僵尸 socket 占死，换用 %s", sid, port, alt)
                    use = alt
            if self._restart_client(c, use):
                log.info("[%s] 已自动重启客户端且端口 %s 就绪，接管补领", sid, use)
                return self._claim(recipe, c, use, match)

        log.warning(
            "[%s] 客户端已带 --remote-debugging-port=%s 启动但端口连不上 ⇒ "
            "该次启动的端口绑定失败（多为重启时新旧实例交接太近、旧实例未释放端口）。"
            "请重启客户端一次即可恢复；本次不守候（守候也不会自愈）", sid, port)
        self._remind(recipe)
        return CheckinResult(sid, Outcome.NO_ACTION,
                             message="客户端启动时端口绑定失败，请重启客户端后补领",
                             detail={"reason": "debug_port_bind_failed",
                                     "pass": budget.pass_index if budget else 0})

    def _busy_state(self, port: int, match: str, c: Dict[str, Any],
                    idle_fallback: Optional[Callable[[], Optional[bool]]] = None
                    ) -> Optional[bool]:
        """取一次忙闲判断：优先 `busy_js`，取不到时可选地求助 `idle_fallback`。

        **为什么要有 fallback（2026-10-17 发现的一处"名存实亡"）**：
        `busy_js` 必须连着调试端口才能执行 —— 而"端口不通"正是我们唯一想重启的场景。
        于是这个忙闲关的实际效果是：**永远返回 None ⇒ 永远判定"看不清" ⇒ 从不重启**。
        用户 2026-10-14 授权的"该重启重启"，从到写下那一刻起就**没能真正执行过一次**。

        这不违反"看不清就不重启"的原则 —— 那条原则约束的是**没有证据时不要赌**；
        这里补的是一条**本来就该有的证据**（进程 CPU），不是放宽判定。
        """
        busy = self._client_busy(port, match, c)
        if busy is None and idle_fallback is not None:
            busy = idle_fallback()
            if busy is not None:
                log.info("忙闲探测取不到（端口不通），改用进程 CPU 旁证：%s",
                         "忙" if busy else "空闲")
        return busy

    @staticmethod
    def _cpu_script(name: str) -> str:
        """构造"该客户端全部进程累计 CPU 时间（100ns）"的 PowerShell（抽成纯函数以便可测）。"""
        safe = name.replace("'", "''")
        return (
            "$ns = Get-CimInstance Win32_Process -Filter \"Name = '" + safe + "'\" "
            "-ErrorAction SilentlyContinue | "
            "ForEach-Object { [double]$_.KernelModeTime + [double]$_.UserModeTime };"
            "if ($ns) { '{0}' -f [int64](($ns | Measure-Object -Sum).Sum) }"
        )

    @staticmethod
    def _powershell_exe() -> str:
        r"""`powershell.exe` 的**绝对路径**（找不到才退回裸名）。

        为什么不用裸名（2026-10-25 实测）：`powershell.exe` 躺在
        `%SystemRoot%\System32\WindowsPowerShell\v1.0\` 下，而**那个目录不在 PATH 里**
        （PATH 里的是 `System32`）。计划任务的完整环境里碰巧能找到，裁剪过 PATH 的
        环境（比如从某些 shell 派生的子进程）会直接 OSError ⇒ `_app_running`
        **静默返回空串** ⇒ 驱动据此判定"客户端没在跑"，接着走"直接拉起"甚至报错。
        这种失败只表现为一个空字符串，看不出是查询压根没跑起来 —— 正是本项目
        反复声讨的"名存实亡"。
        """
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        return exe if os.path.exists(exe) else "powershell"

    def _cpu_total(self, name: str) -> Optional[float]:
        """读一次 CPU 累计秒数；取不到（PowerShell 不可用 / 进程不存在）返回 None。"""
        if not name:
            return None
        try:
            out = subprocess.run([self._powershell_exe(), "-NoProfile", "-NonInteractive",
                                  "-Command", self._cpu_script(name)],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=25,
                                 creationflags=procenv.NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("CPU 探针失败：%s", e)
            return None
        raw = (out.stdout or "").strip().splitlines()
        if not raw:
            return None
        try:
            return float(raw[-1]) / 10_000_000.0     # 100ns → 秒
        except ValueError:
            return None

    def _cpu_busy(self, c: Dict[str, Any]) -> Optional[bool]:
        """用**进程 CPU** 判断客户端是否在干活 —— 端口不通时唯一还能拿到的旁证。

        判据：一个 `window` 秒的观测窗内，该客户端**全部进程**合计消耗的 CPU 时间
        是否超过 `idle_cpu_sec`。取舍偏向保守 —— Electron 应用再空闲也会有些许心跳，
        所以阈值远高于那种噪声；宁可判成"忙"多等一会儿，也不能把正在干活的应用干掉。
        """
        probe = c.get("busy_probe") if isinstance(c.get("busy_probe"), dict) else {}
        name = str(c.get("process_match") or "").strip()
        if not name:
            exe = self._resolve_exe(c)
            name = os.path.basename(exe) if exe else ""
        if not name:
            return None
        window = _float_cfg(probe, "cpu_window_sec", 5.0)
        thresh = _float_cfg(probe, "idle_cpu_sec", 1.0)
        before = self._cpu_total(name)
        if before is None:
            return None
        time.sleep(max(0.5, window))
        after = self._cpu_total(name)
        if after is None:
            return None
        used = max(0.0, after - before)
        log.debug("CPU 观测窗 %.1fs 内该客户端消耗 %.2f 核·秒（阈值 %.2f）",
                  window, used, thresh)
        return used >= thresh

    @staticmethod
    def _first_pass_cap(cfg) -> Optional[float]:
        """第一遍（快扫）里单站点等待的上限；读不到配置就用 25 秒。"""
        try:
            return float(getattr(cfg.safety, "first_pass_wait_cap_sec", 25) or 25)
        except (TypeError, ValueError):
            return 25.0

    def _wait_budget(self, c: Dict[str, Any], cfg, key: str,
                     budget: Optional[RunBudget],
                     cap: Optional[float]) -> int:
        """配方里某个"等待秒数"在**本轮到底能给多少**。

        `budget is None`（手工调用 / 单测）⇒ 原样返回配方值，保持向后兼容。
        """
        want = int(_float_cfg(c, key, 0))
        if budget is None:
            return max(0, want)
        return int(budget.clamp(want, cap))

    # --------------------------------------------- 端口本身救不回来 ⇒ 换端口

    # 僵尸羁押：换端口时向上扫描的范围。端口是"一次性资源"，向上找没被沾过的。
    _PORT_SCAN_SPAN = 60

    @classmethod
    def _port_is_zombie(cls, port: int) -> bool:
        """netstat 里有人在 LISTENING，但那个 PID 已经死了 ⇒ 僵尸 socket。

        形态（本项目已遇到三次：9334 / 9335 / 9337）：带端口的实例退出时，监听
        socket 被某个**继承了句柄的子进程**继续持有，内核 TCP 表里却仍记在**已死 PID**
        名下。表现为"LISTENING 还在、却连不上也绑不上"，而且**重启机器之前拿不回来**。
        ⇒ 识别到它就**必须换端口**，同一端口重启多少次都是白搭。
        """
        pid = cls._port_listener_pid(port)
        return pid is not None and not cls._pid_alive(pid)

    @staticmethod
    def _port_bindable(port: int) -> bool:
        """这个端口现在能不能被真正 bind —— 唯一可靠的"可用性"判据。

        为什么不用"裸 bind 失败"判僵尸：活跃端口同样绑不上（内存笔记里 9336
        就报过 10048）。但反过来成立 —— **能 bind 的一定还没被占住**，
        这一步把"看起来空闲"和"真的空闲"区分开，僵尸也逃不掉。
        """
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                s.listen(1)
                return True
            finally:
                s.close()
        except OSError:
            return False

    def _reserve_clean_port(self, sid: str, port: int, cfg) -> Optional[int]:
        """挑一个**真能 bind** 的干净端口，并把它写回配方。

        为什么必须写回配方而不是另存一份：`debug_port` 是**单一事实源** ——
        驱动连接它、`tools/win_shortcut_args.py` 写快捷方式也读它。两边各留一份
        必然漂移（本项目因"同一结论两处写法"吃过亏）。
        """
        for cand in range(port + 1, port + self._PORT_SCAN_SPAN + 1):
            if self._port_listener_pid(cand) is not None:
                continue
            if self._port_alive(cand):          # 有 HTTP 应答 ⇒ 是别人的实例
                continue
            if not self._port_bindable(cand):
                continue
            if self._write_recipe_port(cfg.root, sid, cand):
                return cand
            return None
        log.warning("[%s] 端口 %s 附近 %d 个候选都不干净，无法自动换端口",
                    sid, port, self._PORT_SCAN_SPAN)
        return None

    @staticmethod
    def _write_recipe_port(root: str, sid: str, port: int) -> bool:
        """把 `recipes/<sid>.yaml` 里的 `debug_port:` 改成新值（**保留注释与其余内容**）。

        刻意用「行正则」而不是 yaml 重排：配方里大半是**踩坑注释**，重写会把它们
        冲掉。目标行形如 `  debug_port: 9340`，注释行以 `#` 开头不会命中。
        原子替换（tmp + os.replace），失败一律只记日志。
        """
        path = os.path.join(root or ".", "recipes", f"{sid}.yaml")
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            new, n = re.subn(r"^(\s*debug_port\s*:\s*)\d+",
                             lambda m: m.group(1) + str(port), text, flags=re.M)
            if n != 1:
                log.warning("配方 %s 里的 debug_port 有 %d 处（应为 1 处），拒绝改写",
                            path, n)
                return False
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(new)
            os.replace(tmp, path)
            log.warning("已把 %s 的 debug_port 改写为 %s（端口被僵尸占死，必须换）",
                        path, port)
            return True
        except (OSError, ValueError) as e:
            log.warning("改写配方端口失败（%s）：%s", path, e)
            return False

    # ------------------------------------------ 启动入口自愈（下一次就带端口）

    _REPAIR_STAMP = os.path.join("data", "entry_repair.json")

    def _repair_launch_entry(self, sid: str, c: Dict[str, Any], cfg) -> Optional[bool]:
        """把这个 client 的**启动入口**（快捷方式 / 自启动项）改回带调试端口。

        为什么必须有（2026-10-17 又来一次）：客户端**自升级**后会自己重启一次，
        重启不吃我们的配方 —— 于是新实例命令行里没有 `--remote-debugging-port`，
        之后**每天**都接管不了。当天日志原话：
        `[qoder] 客户端在运行但未开调试端口（...\\.qoder-versions\\0.4.8\\...）`。
        手工修一次只对"下一次启动"有效，而下次 upgrades 又会把它改回去 ⇒ 必须自动修。

        ⚠ 这是**会动到用户机器**的动作，所以三重门槛：
          ① 配方必须显式开启 `repair_launch_entry`；
          ② 同一天同一站点只做一次（戳记录在 `data/entry_repair.json`）；
          ③ 只调本仓库自己的工具（`tools/win_shortcut_args.py`，带备份可 `--revert`）。
        """
        if not bool(c.get("repair_launch_entry")):
            return None
        root = str(getattr(cfg, "root", "") or "")
        if not root or self._repair_done_today(root, sid):
            return None
        tool = os.path.join(root, "tools", "win_shortcut_args.py")
        if not os.path.isfile(tool):
            log.debug("[%s] 找不到启动入口修复工具，跳过", sid)
            return None
        cmds: List[List[str]] = [[sys.executable, tool, "--app", sid]]
        if bool(c.get("repair_autostart")):
            cmds.append([sys.executable, tool, "--app", sid, "--autostart"])
        # 子进程 stdout 走管道时 Python 会退回 **locale 编码**（本机 cp936/GBK）。
        # 父进程按 utf-8 解 ⇒ 全部降级成 U+FFFD ⇒ ① 日志不可读 ② 下面那道
        # 「自检通过」子串判定**恒为 False**（2026-10-18 日志里就是乱码，且当天
        # 从未打出过"已修好启动入口"）。强制让子进程用 utf-8，管道两端才对得上。
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        ok = False
        for argv in cmds:
            try:
                p = subprocess.run(argv, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=90,
                                   env=env, creationflags=procenv.NO_WINDOW)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("[%s] 修复启动入口失败：%s", sid, e)
                continue
            # 判据用 **退出码**，不在 stdout 里找中文子串：工具是 `return 0 if ok else 1`，
            # 退出码不随文案改动、也不受任何编码影响 —— 比字符串匹配硬得多。
            line = (p.stdout or p.stderr or "").strip().replace("\n", " ")
            ok = ok or (p.returncode == 0)
            log.info("[%s] 启动入口修复（exit=%s）：%s", sid, p.returncode,
                     line[:200])
        if ok:
            log.warning("[%s] 已修好启动入口 —— 客户端下次启动即带调试端口 %s，届时自动接管",
                        sid, c.get("debug_port"))
        self._mark_repair_today(root, sid)
        return ok

    @classmethod
    def _repair_stamp_path(cls, root: str) -> str:
        return os.path.join(root, cls._REPAIR_STAMP)

    @classmethod
    def _repair_done_today(cls, root: str, sid: str) -> bool:
        try:
            with open(cls._repair_stamp_path(root), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        return str((data or {}).get(sid) or "") == datetime.now(CST).strftime("%Y-%m-%d")

    @classmethod
    def _mark_repair_today(cls, root: str, sid: str) -> None:
        path = cls._repair_stamp_path(root)
        data: Dict[str, Any] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
            if isinstance(old, dict):
                data = old
        except (OSError, ValueError):
            pass
        data[sid] = datetime.now(CST).strftime("%Y-%m-%d")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError as e:
            log.debug("记录启动入口修复日期失败：%s", e)

    # ------------------------------------------------------------ 领取主流程

    def _claim(self, recipe: Recipe, c: Dict[str, Any], port: int, match: str) -> CheckinResult:
        sid = recipe.id
        # 两条路可选，由配方决定：
        #   bridge  —— 在页面上下文里执行配方给的 JS 表达式，**直接调客户端自己的接口**；
        #   DOM     —— 找 target、找按钮、发事件序列（Qoder 走这条）。
        # bridge 更稳：不依赖 UI 是否渲染、不受 shadow DOM / 服务端下发文案变化影响。
        bridge = c.get("bridge")
        if isinstance(bridge, dict) and bridge.get("status_js"):
            # 有些站点的入口**平时根本不存在**（只在客户端内置浏览器里按需打开的活动页），
            # 计划任务跑的时候没人替我们点开 ⇒ 配方用 `open_target.url` 声明"我自己开一个"。
            # 自己开的好处：不点 UI、不依赖用户是否开着、也不会误关用户那个标签页
            # （按 targetId 认领，见 cdp.open_target 的注释）。
            opened = None
            oc = c.get("open_target") if isinstance(c.get("open_target"), dict) else {}
            if oc.get("url"):
                opened = open_target(port, str(oc["url"]),
                                     wait_sec=float(oc.get("wait_sec", 8)))
                if opened is None:
                    # 开不出来就是"今天做不了"，**绝不能**退回用户已有的标签页去猜，
                    # 更不能报 success —— 那是没有证据的成功。
                    return CheckinResult(sid, Outcome.ERROR,
                                         message="活动页打不开（createTarget 未就绪）",
                                         detail={"open_target": str(oc["url"])})
            try:
                return self._claim_via_bridge(recipe, c, port, match, bridge,
                                              target=opened)
            finally:
                if opened is not None and bool(oc.get("close_after", True)):
                    close_target(port, str(opened.get("id") or ""))
        find, click, markers = self._js(c)
        # 「签到 UI 就在客户端主窗口里」—— 与 Qoder 那种**独立活动页 target** 相对。
        # Trae CN 的入口是主工作区里的**账号菜单浮层**，没有自己的 target：
        #   · target 必然命中主窗口 ⇒ 不会走 "入口未自动出现" 那条分支；
        #   · 但按钮藏在浮层后面，必须先点入口展开，否则永远 found=False；
        #   · 且**绝不能重载**：主窗口就是用户的工作区（详见下面 `_recheck_stale` 处）。
        main_ui = bool(c.get("claim_in_main_window"))

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
        elif main_ui:
            # 签到 UI 在主窗口内 ⇒ target 已经命中，但按钮还藏在浮层后面。
            # 先按配方的 `open_entry_labels` 把入口点开，下面的 find/click 才找得到按钮。
            log.info("[%s] 签到入口在客户端主窗口内，先展开入口", sid)
            self._open_entry(port, c)
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

            # 「能不能领」只看**按钮本身**。不行就先做一次「陈旧快照」复核再下结论 ——
            # 活动 Surface 打开后会一直留着（我们只断 WebSocket，从不关它），
            # 里面的 DOM 停在打开那一刻；跨天后它还写着"已领取"，而服务端早已
            # 刷新到新窗口。不复核就会把"今天能领"误判成"今天已领"，
            # 且记为 ALREADY、不触发任何提醒（2026-10-06 真漏签一整天）。
            if not _claimable(pre):
                if main_ui:
                    # 主窗口就是用户的**工作区** —— 重载它会打断手头的活（VSCode 系
                    # 重载会重建整个 workbench）。而主窗口内的菜单浮层每次点开都是
                    # **实时渲染**的，不存在 Qoder 那种「跨天存活的陈旧快照」，
                    # 所以这里不做重载复核，直接按当前 DOM 下结论。
                    log.debug("主窗口 UI 站点，跳过陈旧快照复核（重载代价不可接受）")
                else:
                    fresh = self._recheck_stale(page, find)
                    if fresh is not None:
                        pre, body = fresh, str(fresh.get("body") or "")

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

    # ------------------------------------------------------ 陈旧快照复核

    # 「命令行已带端口参数、但端口连不上」的静默期上限（秒）。
    # 客户端可能刚启动、还没 listen 完 —— 那一刻判"绑定失败"会是误判，故先等这么久。
    # 超过则说明 listen 失败（ Electron 侧静默继续跑，不会自愈），立刻转人工重启。
    _PORT_BIND_GRACE_SEC = 45

    # 重载后等待「按钮出现」的上限。活动页实测加载 ~0.8s
    # （客户端日志 `活动 Surface 加载完成 durationMs: 730~833`），给足余量。
    _STALE_RECHECK_SEC = 8.0
    # 发起重载后先静等这么久再开始读，避免在 loading 期空转。
    _STALE_RELOAD_WAIT = 1.5

    def _recheck_stale(self, page: CDPPage, find: str) -> Optional[Dict[str, Any]]:
        """重载活动页，确认"已领取"不是**跨天陈旧快照**造成的误判。

        背景（2026-10-06 实测，代价是漏签一整天）：

        客户端里的活动 Surface **一旦打开就一直留着** —— 驱动只断 WebSocket
        （`page.close()`），从不关闭那个 UI。于是它的 DOM 停在**打开那一刻**：
        昨天领过 → 页面写着"已领取"、按钮消失 → `_JS_PROBE` 返回 `found=False`。

        跨天后服务端已刷新到新窗口，**页面却不会自动重载**。此时"找不到按钮 +
        页面含『已领取』"这条判定就把「今天其实可领」误判成「今天已领」，
        结果记为 ALREADY —— 不报错、不提醒，**静默漏签**。

        铁证：当日 10:45:17 驱动报 already，而"端口就绪 → 出结论"只隔了 **34 毫秒**，
        说明 `_wait_target` 命中的是一个**早已存在**的 iframe；同时客户端日志显示
        02:44:51Z 起 `claimable` 一直是 `true`，直到 15:31 手动领取后才转 `false`。

        返回：重载后**出现领取按钮**的快照；重载失败或按钮始终不出现 → None
        （None = 复核没能推翻原判定，调用方沿用原来的 already / no_action）。
        """
        if not page.reload(self._STALE_RELOAD_WAIT):
            return None
        deadline = time.time() + self._STALE_RECHECK_SEC
        while time.time() < deadline:
            try:
                pre = page.evaluate(find, await_promise=False) or {}
            except Exception as e:         # noqa: BLE001 —— 重载期间读取失败属正常
                log.debug("重载后读取失败（页面可能仍在加载，继续等）: %s", e)
                pre = {}
            if pre.get("found"):
                log.info("重载后出现领取按钮 —— 原快照陈旧，继续领取")
                return pre
            # 页面**已加载完成**却仍没有按钮 ⇒ 这就是服务端的最终答案，立刻收手。
            # 少了这一步，"今天确实已领"（常态）每天都要白等满超时才出结论。
            if pre.get("ready"):
                log.debug("重载完成但仍无领取按钮 —— 原判定成立")
                return None
            time.sleep(0.4)
        return None

    # ------------------------------------------------------------ bridge 路径

    _BRIDGE_SLOT = "__ck_bridge_result"

    def _claim_via_bridge(self, recipe: Recipe, c: Dict[str, Any], port: int,
                          match: str, bridge: Dict[str, Any],
                          target: Optional[Dict[str, Any]] = None) -> CheckinResult:
        """在客户端页面上下文里执行配方给的表达式，直接调它自己的接口完成签到。

        为什么不点 UI（2026-10-06 WorkBuddy 实测后的选择）：
        签到横幅（`.daily-checkin--menu-banner`）是**按需渲染**的 —— 只有服务端下发
        banner 数据时才挂载。实测当天「今日已签」时它根本没渲染，菜单里只剩两个跳转入口，
        "找按钮再点"这条路**无从下手**；而客户端自己的 http 通道始终在，且与客户端
        签到走的是**同一条路**（客户端日志实证：`[TuringSdk] check-in outgoing
        path=/v2/billing/meter/daily-checkin hasToken=true`）。对它来说 bridge 才是主路径。

        判定**完全复用**配方的 verdict（code 规则 + result_flag 业务词表），
        驱动不认识任何产品名 —— 表达式内容属于站点知识，写在配方里。
        """
        sid = recipe.id
        timeout = int(c.get("bridge_timeout_sec", 25))

        # `target` 非空 = 调用方已经自己开好了页面（open_target），直接用它的连接，
        # 不再按 URL 去 `/json` 里瞎找 —— 同一个 URL 上可能有用户自己开着的那个。
        tgt = target or self._wait_target(port, match, int(c.get("target_timeout_sec", 25)))
        if tgt is None:
            return CheckinResult(sid, Outcome.NO_ACTION,
                                 message="客户端主窗口未出现（target_match 未命中）",
                                 detail={"target_match": match})
        page = CDPPage(tgt["webSocketDebuggerUrl"])
        try:
            page.wake()
            # 1) 先只读取状态 —— 已签就收手，绝不重复调 trigger。
            st = self._bridge_call(page, str(bridge["status_js"]), timeout)
            if st is None:
                return CheckinResult(sid, Outcome.ERROR,
                                     message="客户端 bridge 无响应（status 超时或表达式异常）")
            sdata = st.get("data") if isinstance(st.get("data"), dict) else {}
            if recipe.verdict.already_checked_in(sdata):
                return CheckinResult(sid, Outcome.ALREADY,
                                     message="今日已签到（客户端 bridge 状态）",
                                     detail={"source": "client_bridge",
                                             "streak_days": sdata.get("streak_days")})

            claim_js = bridge.get("claim_js")
            if not claim_js:
                return CheckinResult(sid, recipe.verdict.map(None, st.get("code")),
                                     message="bridge 仅配置了 status（code=%s）" % st.get("code"),
                                     detail={"source": "client_bridge"})

            # 2) 领取
            cl = self._bridge_call(page, str(claim_js), timeout)
            if cl is None:
                return CheckinResult(sid, Outcome.ERROR,
                                     message="客户端 bridge 无响应（claim 超时或表达式异常）")
            cdata = cl.get("data") if isinstance(cl.get("data"), dict) else {}
            outcome = (recipe.verdict.map_flags(cdata)
                       or recipe.verdict.map(None, cl.get("code")))

            # 3) 复核：只有**再读一次 status 确认已签**，才敢把 success 说出口。
            #    trigger 的响应体格式若将来变了（服务端改字段），这里能兜住 ——
            #    宁可多一次只读调用，也不要报一个没有证据的"成功"。
            if outcome == Outcome.SUCCESS:
                st2 = self._bridge_call(page, str(bridge["status_js"]), timeout)
                d2 = (st2 or {}).get("data")
                if not recipe.verdict.already_checked_in(d2 if isinstance(d2, dict) else {}):
                    log.warning("[%s] claim 报成功但复核未确认已签，降级为 no_action", sid)
                    outcome = Outcome.NO_ACTION

            detail = {"source": "client_bridge", "claim_code": cl.get("code"),
                      "claim_status": cdata.get("status")}
            zh = {Outcome.SUCCESS: "已领取", Outcome.ALREADY: "本窗口已领取"}.get(outcome)
            msg = ("%s（客户端 bridge）" % zh if zh
                   else "客户端 bridge 返回未判定结果（code=%s）" % cl.get("code"))

            # 4) 让**界面**也跟上（可选开关 `refresh_ui_after_claim`，默认关）。
            #
            # 为什么需要：客户端 UI 的签到状态是**页面加载时**拉一次的（Dumate 实测：
            # 组件的 `useEffect` 只在挂载时调 `hydrateStatus()`，之后既不轮询、
            # focus/visibilitychange 也不重拉 —— 两个事件都实测过，日志里零新增请求）。
            # 我们在 bridge 里把接口调完了，它的 store 一无所知 ⇒ 用户看到"立即签到"
            # 还在原地，会以为**根本没签上**（2026-10-21 用户原话："百度搭子并没有签到成功"），
            # 甚至手动再点一次 —— 而 UI 的 claim 是**本地乐观 +500**，服务端却不会再加，
            # 于是账面比实际多 500，比"显示滞后"更糟。
            #
            # 只在**真的新领取**时刷（already 不刷：UI 本来就没变化，白闪一下）；
            # 且**忙就跳过**（`_cpu_busy` 返回 None 也算忙）—— 宁可界面滞后，
            # 也不打断用户正在跑的会话。
            if outcome == Outcome.SUCCESS and bool(c.get("refresh_ui_after_claim", False)):
                if self._cpu_busy(c) is False:
                    try:
                        page.evaluate("location.reload()", await_promise=False)
                        log.info("[%s] 界面已重新加载，签到状态随之刷新", sid)
                    except Exception as e:  # reload 会掐断当前 CDP 连接，属预期
                        log.info("[%s] 界面刷新已发出（连接随之断开，属预期）: %s", sid, e)
                else:
                    log.info("[%s] 客户端正忙或状态不明，跳过界面刷新（服务端已确认领取）", sid)

            return CheckinResult(sid, outcome, message=msg, detail=detail)
        finally:
            page.close()

    @classmethod
    def _bridge_call_once(cls, page: CDPPage, expr: str, timeout: int) -> tuple:
        """执行配方给的 JS 表达式并取回它 resolve 出来的 JSON。

        为什么不用 `evaluate(..., await_promise=True)`（2026-10-06 实测）：
        桥上抛出的异常会让 CDP 只回一个 `exceptionDetails.text="Uncaught"`，连原始
        message 都拿不到；而一旦 Promise 永不 resolve（页面被冻结的经典症状），
        整条 WebSocket 读会一直卡住。改成"kick + 轮询全局槽位"：
          · kick 立刻返回，不阻塞；
          · 表达式自身的异常在页面里被 try/catch 抓住 → 带上真实 message 回传；
          · 轮询有 deadline，超时可控，不会把连接拖死。

        注意：kick 模板用**字符串拼接**而不是 f-string —— 表达式里天然含 `{}`
        （如 `post(path, {})`），f-string 会把它们当占位符直接报错。
        """
        slot = cls._BRIDGE_SLOT
        kick = (
            "window." + slot + " = {pending: true};\n"
            "(async () => {\n"
            "  try { window." + slot + " = {value: await (\n"
            + expr + "\n)}; }\n"
            # 顺带把**服务端响应体**带出来。HTTP 错误时 axios 类库把 body 放在
            # `e.response.data`，而服务端往往在那里说明真正原因 —— 2026-10-13 实测：
            # WorkBuddy 服务端故障，页面抛的 message 只有 "Request failed with status
            # code 500"，真正的原因（data.code=10001 / msg="签到配置加载失败，请稍后重试"）
            # 全在 e.response.data 里。少了它，日志只说"表达式异常"，看着像驱动的 bug，
            # 其实是服务端故障 —— 为此白写了一个诊断脚本。
            "  catch (e) { let d = null;\n"
            "    try { d = (e && e.response && e.response.data) ?\n"
            "      JSON.stringify(e.response.data) : null; } catch (x) { d = null; }\n"
            "    window." + slot + " = {error: String((e && e.message) || e),\n"
            "                          detail: (d && d.length > 400) ? d.slice(0, 400) : d}; }\n"
            "})();\n"
            "'started'"
        )
        try:
            page.evaluate(kick, await_promise=False)
        except Exception as e:                                  # noqa: BLE001
            log.warning("bridge kick 失败：%s", e)
            return None, True                    # 调用层失败 ⇒ **可重试**

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.35)
            try:
                raw = page.evaluate("JSON.stringify(window." + slot + ")",
                                    await_promise=False)
            except Exception as e:                              # noqa: BLE001
                log.debug("bridge 轮询失败：%s", e)
                continue
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(obj, dict) or obj.get("pending"):
                continue
            if "error" in obj:
                detail = obj.get("detail")
                log.warning("bridge 表达式异常：%s%s", obj["error"],
                            (" ｜ 服务端响应体：" + str(detail)) if detail else "")
                return None, False               # 服务端明确报错 ⇒ **别重试**
            val = obj.get("value")
            if isinstance(val, dict):
                return val, False
            if isinstance(val, str):
                try:
                    parsed = json.loads(val)
                except ValueError:
                    return None, False
                return (parsed if isinstance(parsed, dict) else None), False
            return None, False
        log.warning("bridge 表达式超时（%ss）", timeout)
        return None, True                        # 轮询超时 ⇒ **可重试**

    @classmethod
    def _bridge_call(cls, page, expr, timeout=25, retries=3):
        """带重试的 bridge 调用。返回解析后的 dict，或 None（失败）。

        **为什么要重试**（2026-10-17 实测）：客户端被我们**重启拉起**时，窗口在后台，
        Chromium 会把页面**冻结**（Page Lifecycle → frozen）—— 此时 `await` 的 Promise
        永远不 resolve，CDP 侧表现为"WebSocket 读超时"。`page.wake()` 能解冻，
        但**刚拉起的那一两分钟内可能还没稳**，单次调用必然失败。
        用户手动启动的实例窗口在前台，不会冻结 —— 这就是"我重启就失败、你重启就好"的原因。

        **只有"调用层失败"才重试**（kick 失败 / 轮询超时）；
        表达式本身报错（服务端 5xx 等）说明链路是通的，重试无意义，直接返回。
        """
        for attempt in range(1, max(1, retries) + 1):
            val, retryable = cls._bridge_call_once(page, expr, timeout)
            if val is not None:
                return val
            if not retryable or attempt >= retries:
                return None
            wait = 3 * attempt                          # 3s、6s —— 别把客户端问得太频
            log.info("bridge 第 %d/%d 次未取到结果，%ds 后重试", attempt, retries, wait)
            time.sleep(wait)
            try:
                page.wake()                             # 重试前再解冻一次
            except Exception:                                   # noqa: BLE001
                pass
        return None

    # ------------------------------------------------------------ 状态探测

    def _diagnose(self, recipe: Recipe, c: Dict[str, Any], port: int, match: str,
                  gate: str) -> CheckinResult:
        sid = recipe.id
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
                page = CDPPage(tgt["webSocketDebuggerUrl"])
                try:
                    # bridge 站点（Trae / WorkBuddy）：直接读桥的状态，**不点 DOM**。
                    # 诊断据此就能回答"今天到底领没领"，比 DOM 探针可靠得多。
                    bridge = c.get("bridge") if isinstance(c.get("bridge"), dict) else None
                    if bridge and bridge.get("status_js"):
                        st = self._bridge_call(page, str(bridge["status_js"]), 10)
                        sdata = st.get("data") if isinstance((st or {}).get("data"), dict) else {}
                        detail["bridge_status"] = st
                        signed = recipe.verdict.already_checked_in(sdata)
                        detail["checked_in"] = signed
                        parts.append(f"bridge={'已签' if signed else '未签'}")
                    else:
                        find, _, _ = self._js(c)
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
        """读客户端日志，判断"**当前活动窗口**内是否已领取"。

        两种日志形态，由配方分流：
          · 单文件 —— 配方给了 `client.log_state`（WorkBuddy：
            `%USERPROFILE%\\.workbuddy\\logs\\main.log`，JSON 行，判据 `today_checked_in`）；
          · 多会话目录 —— 老形态（Qoder：`logs/<session>/main.log`，判据 `[Campaign]`/`claimable`）。
        两者返回**同一个契约**：`{claimable, at, claimed_in_window, ...}`，
        调用方（`run()` 的分支①）据此直接判 already，不需要关心是哪一种。
        """
        spec = c.get("log_state")
        if isinstance(spec, dict) and spec.get("file"):
            return self._log_state_from_file(spec, gate)
        return self._log_state_sessions(c, gate)

    def _log_state_sessions(self, c: Dict[str, Any], gate: str) -> Optional[Dict[str, Any]]:
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
    def _window_start(gate: str) -> datetime:
        """当前活动窗口的起点时刻。

        开窗时刻 = 配方给的 `gate`（如 "10:00"）；若现在还没到它，起点回退到昨天 ——
        因为"本窗口是否已领"在开窗前问的其实是**上一个窗口**。
        """
        hh, _, mm = (gate or "10:00").partition(":")
        try:
            g_h, g_m = int(hh), int(mm or 0)
        except ValueError:
            g_h, g_m = 10, 0
        now = datetime.now(CST)
        start = now.replace(hour=g_h, minute=g_m, second=0, microsecond=0)
        if now < start:
            start -= timedelta(days=1)
        return start

    @staticmethod
    def _claimed_in_window(claimable: Optional[bool], at: Optional[datetime],
                           gate: str) -> bool:
        """日志这条记录是否足以断定"**当前活动窗口**内已领取"。

        光看 `claimable=false` 不够 —— 它可能来自上一个窗口的陈旧记录。
        必须同时确认记录时刻落在当前窗口内（窗口起点见 `_window_start`）。
        """
        if claimable is not False or at is None:
            return False
        return at >= ClientDriver._window_start(gate)

    def _log_state_from_file(self, spec: Dict[str, Any], gate: str
                             ) -> Optional[Dict[str, Any]]:
        """单文件日志形态的通用解析（由配方的 `client.log_state` 驱动）。

        WorkBuddy 的签到状态写在**单个** `logs/main.log` 里（不像 Qoder 每次启动
        新建一个会话子目录），每行一条 JSON，样例（时间戳是 UTC）：

            {"timestamp":"2026-10-10T08:28:14.803Z","level":"info","scope":"queue-diag",
             "message":["[Checkin] refreshStatus -> active",
                        {"uiState":"available","today_checked_in":false,"streak_days":10}]}

        配方给出三样东西：`marker`（行标识）、`claimed_field`（字段名）、
        `claimed_value`（该字段等于什么才算"已领"）。

        刻意用「字段名正则 + 数值比较」而不是解析整行 JSON：客户端日志里有非严格
        JSON 的行，整行解析会直接抛错；正则只依赖"某个键的值"，抗噪更好，
        也让新增站点只需改配方、不动代码。
        """
        path = expand(str(spec.get("file") or ""))
        if not path or not os.path.isfile(path):
            return None
        marker = str(spec.get("marker") or "")
        field = str(spec.get("claimed_field") or "")
        if not field:
            return None
        want = bool(spec.get("claimed_value", True))
        try:
            text = _read_tail(path)
        except OSError:
            return None

        field_pat = re.compile(r'"%s"\s*:\s*(true|false)' % re.escape(field))
        ts_pat = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
        last = None
        for ln in text.splitlines():
            if marker and marker not in ln:
                continue
            m = field_pat.search(ln)
            if not m:
                continue
            at = None
            t = ts_pat.search(ln)
            if t:
                try:
                    at = (datetime.fromisoformat(t.group(1).replace("Z", "+00:00"))
                          .astimezone(CST))
                except ValueError:
                    at = None
            last = {"claimed_raw": m.group(1) == "true", "_at_dt": at}
        if not last:
            return None

        at = last.pop("_at_dt", None)
        claimed_now = last.pop("claimed_raw") == want
        # 对外统一成与 Qoder 分支相同的契约：`claimable=False` ≡ "本窗口已领"
        return {"claimable": not claimed_now,
                "at": at.strftime("%Y-%m-%d %H:%M:%S") if at else None,
                "claimed_in_window": self._claimed_in_window(not claimed_now, at, gate)}

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
            out = subprocess.run([self._powershell_exe(), "-NoProfile", "-NonInteractive",
                                  "-Command", script],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=25,
                                 # pythonw（无控制台）下不声明它，powershell 会新建一个
                                 # 可见控制台窗口闪一下 —— 就是用户报障的那个黑框。
                                 creationflags=procenv.NO_WINDOW)
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
    def _port_listener_pid(port: int) -> Optional[int]:
        """netstat 里该端口 **LISTENING 行的 PID**；没有 LISTENING 行则返回 None。

        这是判断"端口到底通不通"的权威判据之一。2026-10-15 的教训：
        HTTP 层会**同时被两种假象骗到** ——
          · 无监听时，沙箱对本地连接返回 502（curl 退出码 0，看着像"通"）；
          · 僵尸 socket 时，HTTP 直接挂起超时。
        只有 `netstat LISTENING` + `PID 活性` 两个证据都对上，才是真端口。
        """
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20,
                                 creationflags=procenv.NO_WINDOW)
            text = (out.stdout or b"").decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return None                      # 查不到就当没这条证据（降级为只信 HTTP）
        for ln in text.splitlines():
            if f":{port} " in ln and "LISTENING" in ln:
                parts = ln.split()
                try:
                    return int(parts[-1])
                except (ValueError, IndexError):
                    return None
        return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """PID 是否还活着 —— 实现在 `core.procenv.pid_alive`（同一套判据全项目共用）。"""
        return procenv.pid_alive(pid)

    @classmethod
    def _port_alive(cls, port: int) -> bool:
        """端口是否真的可用。

        **两道判据都要过**（2026-10-15 加固，当天被假端口骗了半小时）：
          ① HTTP `/json/version` 真的返回；
          ② netstat 里 LISTENING 的 PID 还活着。
        僵尸 socket（LISTENING 记在已死 PID 名下）会让客户端 `listen()` 失败后
        静默没端口 —— 这种端口必须判为不可用，否则后面每一步都会白跑。
        """
        try:
            with _LOCAL_OPENER.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                r.read()
        except Exception:
            return False
        pid = cls._port_listener_pid(port)
        if pid is not None and not cls._pid_alive(pid):
            log.warning("端口 %s 的监听者 PID %s 已死 ⇒ 僵尸 socket，判为不可用", port, pid)
            return False
        return True

    def _wait_port(self, port: int, timeout: int, poll: float = 0.8) -> bool:
        """轮询等端口就绪。

        `poll` 分两档，因为两种用法的时间尺度差三个数量级：
          · 0.8s —— 我们自己刚启动客户端，等它 bind 端口（十几秒级）；
          · 10s  —— **守候用户重启客户端**（分钟~小时级，见 `run()` 分支③）。
            用 0.8s 去守候两小时 = 九千次 HTTP 探测，纯属浪费。
        """
        deadline = time.time() + timeout
        while True:
            if self._port_alive(port):
                return True
            left = deadline - time.time()
            if left <= 0:
                return False
            # ⚠ 必须按**剩余时间**夹一下：本轮预算常常只剩几秒，而长守候档的 poll 是 10s。
            #   不夹的话"让我等 1 秒"会真的睡满 10 秒 —— 一路攒下来整轮就跑不动了。
            time.sleep(min(poll, left))

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

    def _back_to_home(self, page, c: Dict[str, Any], settle: float) -> None:
        """若客户端停在**子页面**（如设置页 `#/settings`），先点「返回」回到主工作区。

        为什么需要（2026-10-11 实测**真失败**）：活动入口**只存在于主工作区**。
        客户端重启后可能落在设置页，那一层连 `open_entry_labels` 的影子都没有
        ⇒ 两个入口全 `no-button` ⇒ 等预算耗尽 ⇒ `no_action` ⇒ **当天漏签**。
        （当天是靠人工把窗口切回主工作区才补上的。）

        **幂等**：主工作区里**不存在** `home_labels` 里的按钮 ⇒ 直接 `no-button`，
        什么都不做。找不到/点不到都静默返回，绝不因它抛异常而阻断领取。
        """
        for label in (c.get("home_labels") or []):
            js = _JS_CLICK_TEXT.replace(
                "__LABEL__", json.dumps(str(label), ensure_ascii=False))
            try:
                if page.evaluate(js, await_promise=False) == "dispatched":
                    log.info("客户端停在子页面，点击 %r 返回主工作区", label)
                    time.sleep(settle)
                    return
            except Exception as e:                          # noqa: BLE001
                log.debug("返回主工作区失败：%s", e)

    def _open_entry(self, port: int, c: Dict[str, Any]) -> None:
        """从客户端外壳把活动入口**点出来**（入口平时藏在用量面板后面）。

        为什么必须做：入口是否在启动时自动打开，由**启动那一刻**的服务端状态决定。
        客户端整天开着（实测 uptime 13 小时）时，开窗后入口不会自己出现 ——
        驱动干等一轮 timeout 只能报 no_action，**当天就白跑了**。

        要素全部来自配方（`open_entry_labels` / `main_target_match`），
        代码里不出现产品名 —— 这是本项目的分层约定：站点知识只进 recipes/*.yaml。
        任何一步失败都只记日志、不抛异常：点不出来就退回原来的 no_action，行为不劣化。

        **每个入口"轮询到可点为止"，而不是"盲等固定秒数后只试一次"**（2026-10-10 改）：
        面板/浮层是异步渲染的，渲染耗时随机器负载浮动；盲等会在慢的时候刚好没等到 ——
        10-10 真实故障：点『查看我的用量』后面板要 **3 秒**才渲染出礼物图标，
        而配方写的是 `entry_delay_sec: 1.5` ⇒ 第二个入口 `no-button` ⇒ 当天 no_action。
        轮询版对渲染快慢都自适应，且不引入新的失败面（找不到就一直等到预算耗尽，
        与原先"等不到就放弃"的终态一致）。
        """
        labels = list(c.get("open_entry_labels") or [])
        if not labels:
            return
        main = self._main_target(port, str(c.get("main_target_match") or ""))
        if main is None:
            log.debug("找不到客户端主窗口 target，跳过入口点击")
            return
        settle = _float_cfg(c, "entry_delay_sec", 1.5)     # 点中后让下一层渲染的静默期
        budget = _float_cfg(c, "entry_timeout_sec", 20.0)  # 单个入口"等它出现"的上限
        page = CDPPage(main["webSocketDebuggerUrl"])
        try:
            # 先确保在主工作区：客户端可能停在设置页等子页面，那一层没有活动入口
            # （2026-10-11 实测：重启后落在设置页 ⇒ 所有入口 no-button ⇒ 当天漏签）
            self._back_to_home(page, c, settle)
            for label in labels:
                js = _JS_CLICK_ARIA.replace(
                    "__LABEL__", json.dumps(str(label), ensure_ascii=False))
                deadline = time.monotonic() + budget
                while True:
                    try:
                        r = page.evaluate(js, await_promise=False)
                    except Exception as e:                  # noqa: BLE001 —— 点不到不能阻断领取
                        log.debug("点击入口 %r 失败：%s", label, e)
                        break
                    if r == "dispatched":
                        log.info("点击入口 %r → dispatched", label)
                        time.sleep(settle)
                        break
                    if time.monotonic() >= deadline:
                        log.info("点击入口 %r → 未出现（已等 %.0fs）", label, budget)
                        break
                    time.sleep(0.5)
        finally:
            page.close()

    def _shutdown(self, port: int) -> None:
        """用 CDP `Browser.close` 优雅关闭**我们刚启动的**实例。

        注意：这里**没有**"按进程名强杀"的回退 —— 它只做**优雅**关闭（CDP `Browser.close`），
        端口不通时静默无操作。需要"连用户实例一起重启"的场景走 `_restart_client()`
        （用户 2026-10-14 明确授权），那个方法才带强制关闭，且**只在配方开启
        `restart_on_bind_failure` 时被调用**。
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

    # -------------------------------------------------- 客户端重启（自愈）

    def _restart_client(self, c: Dict[str, Any], port: int) -> bool:
        """**完全退出**目标客户端，再带调试端口重新拉起 —— 端口绑定失败时的自愈。

        为什么需要（2026-10-14 Trae 真漏签一天）：客户端用「重启」时，
        `app.quit()` 与拉起新实例只隔 **1 秒**，旧实例还没把端口释放完
        ⇒ 新实例 `listen()` 失败后**静默继续跑**（不崩、不退出、不再重试），
        此后那个实例**整个生命周期都没有端口** —— 守候再久也不会自愈，
        唯一的解法就是**再来一次干净的重启**。

        用户 2026-10-14 明确授权："该重启重启"。但分寸仍然保留：
          ① 只在配方开启 `restart_on_bind_failure` 时才可能被调用（不做无谓打断）；
          ② 先走 CDP **优雅**关闭；
          ③ 关完**必须确认进程真的没了、端口真的释放了**，否则重新拉起会再踩同一个
             "新旧实例交接"的竞态 —— 那等于白重启。
        """
        exe = self._resolve_exe(c)
        if not exe:
            log.warning("[restart] 找不到可执行文件，无法重启")
            return False
        name = str(c.get("process_match") or os.path.basename(exe))
        log.warning("[restart] 端口 %s 绑定失败（不会自愈），按授权重启客户端：%s", port, name)

        # ① 优雅关闭：端口通时有效；不通时它自己静默无操作
        self._shutdown(port)

        # ② 强制关闭 —— 这是**唯一会动到用户正在使用的实例**的路径（已获授权）
        if self._app_running(c):
            self._kill_by_name(name)
            for _ in range(20):                     # 最多等 10s 让进程真的退干净
                if not self._app_running(c):
                    break
                time.sleep(0.5)
        if self._app_running(c):
            log.warning("[restart] 进程仍在运行，重启失败（不动它，交给人工）")
            return False

        # ③ 等端口释放。这一步**不能省** —— 抢在旧 socket 释放前拉起，
        #    新实例会再次 listen 失败，等于白重启（正是今天漏签的成因）。
        for _ in range(20):
            if not self._port_alive(port):
                break
            time.sleep(0.5)
        time.sleep(1.0)

        # ④ 带端口重新拉起
        try:
            procenv.spawn([self._ensure_launchable(exe),
                           f"--remote-debugging-port={port}"])
        except Exception as e:                      # noqa: BLE001
            log.warning("[restart] 拉起失败：%s", e)
            return False
        ok = self._wait_port(port, int(c.get("launch_timeout_sec", 60)))
        log.info("[restart] 客户端已重启，端口 %s %s", port, "已就绪" if ok else "仍未就绪")
        return ok

    @staticmethod
    def _kill_by_name(name: str) -> None:
        """按进程名结束该客户端的**全部**进程（Electron 主/渲染进程同名）。

        `/T` 连子进程一起结束 —— 只杀主进程会留下渲染进程继续占着端口，
        下一次拉起照样 listen 失败。
        """
        try:
            subprocess.run(["taskkill", "/IM", name, "/F", "/T"],
                           capture_output=True, text=True, timeout=30,
                           creationflags=procenv.NO_WINDOW)   # pythonw 下不加会闪黑框
        except (OSError, subprocess.SubprocessError) as e:
            log.debug("[restart] taskkill 失败：%s", e)

    # ------------------------------------------------ 重启前的"忙闲"把关

    def _client_busy(self, port: int, match: str,
                     c: Dict[str, Any]) -> Optional[bool]:
        """按配方的 `busy_probe.busy_js` 探测客户端**是否有任务在执行**。

        返回：
          True  = 忙（有任务在执行）
          False = 空闲
          None  = **不可知**（没配探测 / 取不到 target / 表达式失败）

        ⚠ None 的语义是"不知道"，**调用方必须按"不能重启"处理** ——
        用户 2026-10-14 的要求是"看看有没有在执行的任务，有的话就先不重启"，
        那么"看不清"同样不能重启。宁可漏签，也不能赌一把打断用户。
        """
        probe = c.get("busy_probe")
        if not isinstance(probe, dict) or not str(probe.get("busy_js") or "").strip():
            return None
        tgt = self._wait_target(port, match, 10)
        if tgt is None:
            return None
        page = CDPPage(tgt["webSocketDebuggerUrl"])
        try:
            val = page.evaluate(str(probe["busy_js"]), await_promise=True)
        except Exception as e:                              # noqa: BLE001
            log.debug("忙闲探测失败：%s", e)
            return None
        finally:
            page.close()
        if val is True:
            return True
        if val is False:
            return False
        return None                      # 表达式返回了别的东西 = 不可信，按不可知

    def _wait_client_idle(self, port: int, match: str, c: Dict[str, Any],
                          timeout: int,
                          idle_fallback: Optional[Callable[[], Optional[bool]]] = None
                          ) -> bool:
        """客户端忙时**挂载监测**：轮询到空闲为止，超时则收手。

        为什么不在第一次看到"忙"就直接放弃：用户明确要"等任务执行完了再重启"。
        守候期间用户可能自己把任务跑完 —— 那时我们就能安全重启了。
        """
        if timeout <= 0:
            return False
        deadline = time.time() + timeout
        while True:
            time.sleep(20)                                  # 别把客户端问得太频
            if time.time() >= deadline:
                break
            busy = self._busy_state(port, match, c, idle_fallback)
            if busy is False:
                log.info("客户端已空闲，可以安全重启")
                return True
            if busy is None:
                log.warning("监测期间取不到客户端页面（可能被用户关掉或重启），停止监测")
                return False
        log.warning("监测 %ds 客户端仍在执行任务，本次不重启", timeout)
        return False

    def _safe_to_restart(self, recipe: Recipe, c: Dict[str, Any], port: int,
                         match: str, budget: Optional[RunBudget] = None,
                         cap: Optional[float] = None,
                         idle_fallback: Optional[Callable[[], Optional[bool]]] = None
                         ) -> bool:
        """重启前的最后一道关：客户端没在执行任务才允许重启。

        三条出路：空闲 ⇒ 放行；忙但等到了空闲 ⇒ 放行；
        忙且等不到 / **看不清** ⇒ 不放行（降级提醒，绝不硬重启）。

        `idle_fallback` 是 busy_js **取不到结论时**的旁证来源（端口不通时必发生）；
        两条路都是 None 时，这条关仍然一票否决 —— 用户的原话是："看不见就在执行的任务，
        也不能赌"。详见 `_busy_state` 的说明。

        `budget` / `cap` 用来把"挂载监测的时长"压进本轮预算（不传 = 按配方原值）。
        """
        sid = recipe.id
        probe = c.get("busy_probe")
        if not isinstance(probe, dict) or not str(probe.get("busy_js") or "").strip():
            log.warning("[%s] 未配 busy_probe ⇒ 无法判断客户端是否在执行任务，按用户要求不重启", sid)
            return False

        busy = self._busy_state(port, match, c, idle_fallback)
        if busy is None:
            log.warning("[%s] 忙闲探测不可用（取不到页面或表达式失败），不重启", sid)
            return False
        if not busy:
            return True

        wait = int(_float_cfg(probe, "wait_busy_sec", 0))
        if budget is not None:
            wait = int(budget.clamp(wait, cap))
        log.info("[%s] 客户端有任务在执行，按配方挂载监测（最多 %ds）", sid, wait)
        return self._wait_client_idle(port, match, c, wait, idle_fallback)

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
    def _ensure_launchable(exe: str) -> str:
        r"""「pending-asar」更新布局：新版本解压后主包挂在 ``app.asar.qoder-pending``，
        要等某个时机被「应用」（改名回 ``app.asar``）才能运行 —— 直接拉起会**秒退**
        （退出码 1，连会话日志都不留，2026-10-27 实测）。

        实测（同日）：客户端自带的顶层启动器**不做**这个应用 —— 它版本切换失灵时
        会回退跑旧版自己，于是「本地数据由更高版本创建」的横幅每次重启都出现；
        优雅关闭（WM_CLOSE）也不会触发应用。⇒ 应用这一步只能我们做。

        安全门：只有当**更新就绪标记**存在（``<版本目录名>.*update-ready*.json``，
        更新器校验完载荷才会写）才动手 —— 防止把更新器写到一半的半截包拉起来。
        已应用（app.asar 在）⇒ 原样返回；改动只发生在 exe 自己的版本目录里，
        且就是官方更新器本来要做的那一次改名。
        """
        if not exe or not os.path.isfile(exe):
            return exe
        res = os.path.join(os.path.dirname(exe), "resources")
        if os.path.isfile(os.path.join(res, "app.asar")):
            return exe
        pending = os.path.join(res, "app.asar.qoder-pending")
        if not os.path.isfile(pending):
            return exe
        vdir = os.path.dirname(exe)
        markers = glob.glob(os.path.join(
            os.path.dirname(vdir), os.path.basename(vdir) + ".*update-ready*.json"))
        if not markers:
            log.debug("[launch] %s 是 pending-asar 但无就绪标记，不应用", vdir)
            return exe
        try:
            os.rename(pending, os.path.join(res, "app.asar"))
            log.info("[launch] 应用待生效更新（pending-asar → app.asar）：%s", vdir)
        except OSError as e:
            log.warning("[launch] 应用 pending-asar 失败（照常尝试拉起）：%s", e)
        return exe

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
