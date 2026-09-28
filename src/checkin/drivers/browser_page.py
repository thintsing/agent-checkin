"""auto 驱动：复用已登录浏览器会话，在页面上下文里调签到接口。"""
from __future__ import annotations

import logging

from ..browser import cdp, launcher, page_script
from ..core import notify
from ..core.models import Action, CheckinResult, Outcome, Recipe
from . import Driver

log = logging.getLogger("checkin.driver.browser")


def _triple(action: Action):
    return (action.method, action.path, action.body) if action else None


class BrowserPageDriver(Driver):
    mode = "auto"

    def run(self, recipe: Recipe, cfg, dry_run: bool = False, probe: bool = False) -> CheckinResult:
        sess = recipe.session
        status = _triple(recipe.actions.get("status"))
        # dry-run 与 probe 一样是只读语义：绝不能触发签到。
        # 若 dry-run 仍走 trigger 分支，空 trigger 会被拼成 fetch("")（即请求
        # 当前页），回一个假的 http=200，让演练结果彻底失真 —— 故两者共用只读路径。
        readonly = bool(dry_run or probe)
        trigger = None if readonly else _triple(recipe.actions.get("trigger"))
        mode = "probe" if readonly else "trigger"

        launcher.ensure_chrome(cfg, sess.start_url or recipe.origin)
        page = cdp.CDPPage.connect(cfg.chrome.remote_debugging_port, sess.tab_match)
        # 长驻浏览器里的标签页会被 Chromium 冻结，冻结后页面里的 await fetch 永不返回。
        # 评估前先解冻，否则复用上一轮留下的实例时必然超时（2026-09-27 修）。
        page.wake()
        try:
            # 确保落在目标站点页面（fetch 同源、带鉴权）
            if sess.tab_match and not any(m in page.current_url() for m in sess.tab_match):
                page.navigate(sess.start_url or recipe.origin)
                import time
                for _ in range(20):
                    if any(m in page.current_url() for m in sess.tab_match):
                        break
                    time.sleep(0.5)

            js = page_script.build(mode, status, trigger, sess.token_localstorage_key,
                                   recipe.verdict.checked_in_flag)
            diag = page.evaluate(js)
            log.debug("[%s] 页面诊断: %s", recipe.id, _brief(diag))

            if readonly:
                sc = (diag or {}).get("statusCall") or {}
                if sc.get("netError"):
                    return CheckinResult(recipe.id, Outcome.ERROR,
                                         f"网络错误: {sc['netError']}", detail=diag)
                # 服务端的"今日已签"比业务 code 更准：code=0 只说请求成功。
                if recipe.verdict.already_checked_in(sc.get("flags")):
                    mapped = Outcome.ALREADY
                else:
                    mapped = recipe.verdict.map(sc.get("http"), sc.get("code"))
                keys = [c.get("k") for c in (diag or {}).get("tokenCandidates", [])]
                if sc.get("tokenKey"):
                    where = f" token键={sc['tokenKey']}"
                elif keys:
                    where = f" 候选token键={keys}"
                else:
                    where = "（未探测到 token 键）"
                label = "探针" if probe else "演练"
                if mapped == Outcome.NEED_LOGIN:
                    _remind(recipe)
                # 只读模式下如实返回映射结果（可能 need_login / already / success），
                # 但注明未触发签到，避免把"当前状态"误读成"刚签成功"。
                return CheckinResult(
                    recipe.id, mapped,
                    message=(f"[{label}] 当前状态={_zh(mapped)}"
                             f"（http={sc.get('http')} code={sc.get('code')}"
                             f"{_flags_note(sc.get('flags'))}）{where}"
                             f"　只读，未触发签到"),
                    detail=diag or {})

            if (diag or {}).get("skippedBecauseCheckedIn"):
                # 状态接口已声明今日已签 —— 收手，不再发那一次写请求。
                return CheckinResult(
                    recipe.id, Outcome.ALREADY,
                    message="今日已签（状态接口已声明，未重复请求签到接口）",
                    detail=diag or {})

            tc = (diag or {}).get("triggerCall") or {}
            if tc.get("netError"):
                return CheckinResult(recipe.id, Outcome.ERROR, message=f"网络错误: {tc['netError']}", detail=diag)

            # 优先按客户端自己的业务词表（data.status）判定；拿不到才退回 code 规则。
            outcome = (recipe.verdict.map_flags(tc.get("flags"))
                       or recipe.verdict.map(tc.get("http"), tc.get("code")))
            if outcome == Outcome.NEED_LOGIN:
                _remind(recipe)
            msg = (f"{_zh(outcome)} (code={tc.get('code')} http={tc.get('http')}"
                   f"{_flags_note(tc.get('flags'))})")
            return CheckinResult(recipe.id, outcome, message=msg, detail=diag)
        finally:
            page.close()


def _zh(o: Outcome) -> str:
    return {
        Outcome.SUCCESS: "已领取", Outcome.ALREADY: "今日已签",
        Outcome.NEED_LOGIN: "需登录", Outcome.NO_ACTION: "未动作",
        Outcome.ERROR: "错误",
    }.get(o, o.value)


def _flags_note(flags) -> str:
    """把服务端返回的关键状态标量摘几个进消息里，便于一眼看清真实状态。"""
    if not isinstance(flags, dict) or not flags:
        return ""
    want = ("active", "today_checked_in", "streak_days", "daily_credit", "today_credit")
    picked = [f"{k}={flags[k]}" for k in want if k in flags]
    if not picked:
        picked = [f"{k}={v}" for k, v in list(flags.items())[:3]]
    return " " + " ".join(picked)


def _brief(diag) -> dict:
    if not isinstance(diag, dict):
        return {"raw": str(diag)[:200]}
    out = {"href": diag.get("href")}
    for k in ("statusCall", "triggerCall"):
        c = diag.get(k)
        if isinstance(c, dict):
            out[k] = {kk: c.get(kk) for kk in
                      ("http", "code", "message", "flags", "tokenKey", "tokenRedacted", "netError")}
    out["tokenCandidates"] = [c.get("k") for c in diag.get("tokenCandidates", [])]
    if diag.get("skippedBecauseCheckedIn"):
        out["skippedBecauseCheckedIn"] = True
    return out


def _remind(recipe: Recipe) -> None:
    """把"需要人工介入"变成用户**看得见**的一条系统通知。

    为什么必须做（2026-09-28 实测，代价是漏签一整天）：
    `need_login` 原先**只写日志**，界面上没有任何动静 —— 当天 10:51 就判定了未登录，
    用户直到 12:29 自己来问才发现，中间白白漏了半天。登录态是会自然失效的
    （实测 `session` cookie 有效期 168 小时 = 7 天，到期只能人工重登），
    所以这条路径**注定会被走到**，必须主动出声，而不是等人来查。

    文案走配方（`reminder` 段），代码里不出现站点名 —— 与 client_claim 同约定。
    """
    rem = recipe.reminder or {}
    notify._toast(rem.get("toast_title") or f"{recipe.name} 签到需人工处理",
                  rem.get("toast_body") or "自动签到未能完成，详情见日志。")
