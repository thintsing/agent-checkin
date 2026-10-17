"""通知：控制台摘要 + Windows 桌面 toast（经 PowerShell，无第三方依赖）。

**这个模块的职责是"说人话"，不是"报数据"。** 它只回答一个问题：
"今天有没有需要我处理的？" —— 所以下面的分类逻辑比看起来重要。

处理这块时踩过的坑（2026-09-24 修）：原先完成率的分母是 `len(results)`（全部站点），
分子只数 success/already。于是 `manual` 站点（永远返回 `no_action`）**永远不计入分子、
却永远计入分母** ⇒ 每天都弹「每日签到完成 1/2 个站点已处理」，看着像永远失败一半。
更糟的是**熔断/报错时也照弹这个标题** —— 用户看到"完成"就不会去查，等于没有告警。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from . import procenv
from .models import CheckinResult, Outcome

log = logging.getLogger("checkin.notify")

_ICON = {
    Outcome.SUCCESS: "[OK]  ",
    Outcome.ALREADY: "[已签]",
    Outcome.NEED_LOGIN: "[登录]",
    Outcome.CIRCUIT: "[熔断]",
    Outcome.NO_ACTION: "[待办]",
    Outcome.SKIPPED: "[略过]",
    Outcome.ERROR: "[错误]",
}

# ── 分类 ────────────────────────────────────────────────────────────
# `_PROBLEM` / `_DONE` + "其余" 必须**穷尽且互斥**地覆盖 Outcome。
# 保险丝：tests/test_core.py::TestNotify::test_every_outcome_is_classified ——
# 将来加了新 Outcome 却忘了归类，它会被静默算进"其余"：既不报错也不告警。
#
# ⚠ 运行时**必须真的读这两个常量**（`notify_results` 里用的就是它们）。
#   这里踩过一次：常量定义了、测试也断言了，但 `notify_results` 内部另外写了一份
#   内联字面量元组 —— 结果是测试守着一个**没人用的死常量**，把常量改坏它也不红。
#   是"故意改坏、看它变不变红"这一步当场抓出来的（见 HANDOFF 2026-09-24 23:0x）。
_PROBLEM = (Outcome.NEED_LOGIN, Outcome.ERROR, Outcome.CIRCUIT)
# 上面这组里"需要登录"的那部分要单独给操作指引，故单列。**必须是 _PROBLEM 的子集**。
_LOGIN_NEEDED = (Outcome.NEED_LOGIN,)
_DONE = (Outcome.SUCCESS, Outcome.ALREADY)
# 其余 = NO_ACTION（manual 已提醒 / 未知结果降级）+ SKIPPED（幂等、窗口未到、周末…）
# 二者都属于"不需要你动手"，因此既不算完成、也不算问题。


def _toast(title: str, body: str) -> None:
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType=WindowsRuntime] | Out-Null;"
        "$t=[Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($t);"
        "$n=$x.GetElementsByTagName('text');"
        f"$n.Item(0).AppendChild($x.CreateTextNode({title!r})) | Out-Null;"
        f"$n.Item(1).AppendChild($x.CreateTextNode({body!r})) | Out-Null;"
        "$tn=[Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('AgentCheckIn').Show($tn);"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            timeout=15, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # 计划任务用 pythonw（无控制台）跑；不加这个 flag 的话，powershell.exe
            # 会**新建一个可见的控制台窗口**闪一下 —— 正是我们要消灭的黑框。
            creationflags=procenv.NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("toast 失败（忽略）：%s", e)


def summary(results: List[CheckinResult]) -> str:
    lines = ["", "  今日签到结果", "  " + "-" * 30]
    for r in results:
        lines.append(f"  {_ICON.get(r.outcome,'[??] ')} {r.site_id:<10} {r.message}")
    return "\n".join(lines)


def _brief(msg: str, limit: int = 30) -> str:
    """toast 正文放不下长句，截断到一句人话。"""
    text = str(msg or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def gap_line(gaps: Optional[Dict[str, List[str]]]) -> str:
    """把"本该签到却没签到的日子"压缩成一句人话。

    为什么单独有这一条（2026-10-17）：在此之前，昨天漏签是**完全静默**的 ——
    计划任务被判 missed、程序卡在守候分支、机器没开机……任何一种情况下，
    第二天程序照常跑、照常报"今日已完成"，昨天那个洞永远不会被提起。
    缺口由 `core/state.py::StateStore.missed_days` 在每天第一次运行时往前回溯得到。
    """
    if not gaps:
        return ""
    parts = []
    for sid, days in gaps.items():
        if len(days) == 1:
            parts.append(f"{sid} 昨日漏签")
        else:
            parts.append(f"{sid} 漏签 {len(days)} 天（{days[0]} 起）")
    return "；".join(parts)


def _gap_title(gaps: Optional[Dict[str, List[str]]]) -> str:
    """缺口通知的标题。漏一天说"昨天"，漏多天直接说天数 ——
    标题是唯一一眼能扫到的东西，含糊不得。"""
    total = max((len(d) for d in (gaps or {}).values()), default=0)
    return "昨天漏签了" if total <= 1 else f"漏签 {total} 天"


def _done_line(results: List[CheckinResult]) -> str:
    """【只报"今天真做了什么"，不报"没做什么"。】

    刻意不出现 "N/M 个站点" 这种比例：站点集合里混着 auto 和 manual，
    一个统一的分母对任何一方都是错的。改成按性质分列，读的人不会误解。
    """
    done = [r.site_id for r in results if r.outcome in _DONE]
    waiting = [r.site_id for r in results if r.outcome is Outcome.NO_ACTION]
    parts = []
    if done:
        parts.append("已签到 " + "、".join(done))
    if waiting:
        parts.append("待你手动 " + "、".join(waiting))
    if not parts:
        return "今日无需动作"      # 全是 skipped：今日已做过 / 窗口未到 / 周末
    return " · ".join(parts)


def notify_results(results: List[CheckinResult],
                   root: Optional[str] = None,
                   gaps: Optional[Dict[str, List[str]]] = None) -> None:
    """控制台摘要 + 桌面 toast（+ 可选：推送到 QQ）。

    `root` 非空且 `data/notify.json` 存在时，额外把同一份结论推到 QQ。
    `root` 缺省 None ⇒ 只做本地通知（老调用方与单元测试不受影响）。

    `gaps` = {站点: [漏签日期…]}，由 `StateStore.missed_days` 回溯得到。
    缺省 None ⇒ 不做缺口播报（老调用方行为不变）。
    """
    print(summary(results))
    gap = gap_line(gaps)
    if gap:
        print("  [缺口] " + gap)
    if not results:
        return

    problems = [r for r in results if r.outcome in _PROBLEM]
    text = _qq_message(results, problems, gaps)
    if problems:
        # 分类一律从 _PROBLEM 派生 —— 运行时绝不另写一份字面量元组（见上面的 ⚠）
        need_login = [r for r in problems if r.outcome in _LOGIN_NEEDED]
        broken = [r for r in problems if r.outcome not in _LOGIN_NEEDED]

        # 出问题时**必须换标题**。"每日签到完成" 配一个全是失败的主体，是最坏的通知：
        # 它让用户以为没事，从而跳过检查 —— 一个会误导的告警比没有告警更糟。
        parts = []
        if need_login:
            parts.append("需登录 " + "、".join(r.site_id for r in need_login))
        if broken:
            parts.append("出错 " + "、".join(r.site_id for r in broken))
        hint = ("跑 run_checkin.bat --login 处理登录"
                if need_login else "详情见 logs/ 里今天的日志")
        _toast("签到我处理不了",
               " · ".join(parts) + (f"；{gap}" if gap else "") + f"（{hint}）")
        _push_qq(root, text)
        return

    # 今天没事、但昨天有洞：也要换标题。"每日签到完成"会让用户以为一切正常，
    # 从而不知道连续天数已经断了 —— 那正是"静默漏签"的另一种形态。
    # 正文里刻意**不写**"今日已补上"：这条分支也可能在站点被 `not_before` 闸门
    # 拦成 skipped 时走到，那时"补上了"是假的。今天到底怎么样，交给 `_done_line`
    # 原样说，别替它下结论。
    if gap:
        _toast(_gap_title(gaps), _done_line(results) + " · " + gap)
        _push_qq(root, text)
        return

    _toast("每日签到完成", _done_line(results))
    _push_qq(root, text)


# ── QQ 机器人推送（2026-10-14 新增）────────────────────────────────
# 为什么是"签到程序自己推"，而不是走 WorkBuddy 的通知体系：
#   签到的执行者是 **Windows 计划任务**（`pythonw -m checkin`），WorkBuddy 全程不参与；
#   而 WorkBuddy 的「定时任务结果通知」只支持 企微 bot / 小程序，且要求任务由它自己跑。
#   WorkBuddy 里那个「QQ 机器人」是**助理远程通道**（方向是 你→WorkBuddy，用来远程下指令），
#   不是结果通知出口。⇒ 要让签到结果到 QQ，只能直接调 QQ 开放平台的 Bot API。
#
# 为什么必须是**群聊**而不是私聊：
#   QQ 官方对**私聊主动消息**限制为**每月 4 条**，每天推必然超限；
#   群聊无此限制（每分钟 20 条）。所以需要一个只有你和机器人的群。
#
# 凭据放 `data/notify.json`（`.gitignore` 已覆盖 `data/`）—— **绝不进开源仓库**。
# 文件不存在 = 未开启 ⇒ 静默跳过，对没配的人零影响。
#
# ⚠ 通知**永远不能**影响签到本身：这里任何一步失败都只记日志。
_QQ_CFG_REL = os.path.join("data", "notify.json")
_QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
_QQ_API_BASE = "https://api.sgroup.qq.com"


def _load_qq_cfg(root: Optional[str]) -> Optional[Dict[str, Any]]:
    """读 `data/notify.json` 的 `qq_bot` 段；缺文件/缺字段一律返回 None（= 不推送）。"""
    if not root:
        return None
    path = os.path.join(root, _QQ_CFG_REL)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("QQ 推送配置读取失败（%s）：%s", path, e)
        return None
    qq = (data or {}).get("qq_bot") or {}
    missing = [k for k in ("app_id", "app_secret", "group_openid")
               if not str(qq.get(k) or "").strip()]
    if missing:
        log.warning("QQ 推送配置不完整，缺 %s，已跳过", "、".join(missing))
        return None
    return qq


def _http_post_json(url: str, payload: Dict[str, Any],
                    headers: Optional[Dict[str, str]] = None,
                    timeout: int = 15) -> Dict[str, Any]:
    """POST 一段 JSON，返回解析后的响应体。

    **显式绕开系统代理**：本机（及 WorkBuddy 沙箱）会注入 `HTTP(S)_PROXY`，
    走它反而连不上 QQ 的域名 —— 与 `drivers/client_claim.py` 里读本地 CDP 的做法一致。
    """
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _qq_access_token(app_id: str, app_secret: str) -> Optional[str]:
    """用 AppID/AppSecret 换 access_token（有效期 7200s，每天跑一次够用，不做缓存）。"""
    try:
        data = _http_post_json(_QQ_TOKEN_URL,
                               {"appId": app_id, "clientSecret": app_secret})
    except urllib.error.HTTPError as e:
        # 凭据错时平台回 4xx，body 里通常写明原因（如 invalid appid or secret）
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:                                       # noqa: BLE001
            pass
        log.warning("QQ Bot 取 token 失败：HTTP %s %s", e.code, detail)
        return None
    except Exception as e:                                      # noqa: BLE001
        log.warning("QQ Bot 取 token 失败：%s", e)
        return None
    token = str((data or {}).get("access_token") or "").strip()
    if not token:
        log.warning("QQ Bot 取 token 返回里没有 access_token：%s",
                    json.dumps(data, ensure_ascii=False)[:200])
        return None
    return token


def _qq_message(results: List[CheckinResult],
                problems: List[CheckinResult],
                gaps: Optional[Dict[str, List[str]]] = None) -> str:
    """把结论压缩成一条 QQ 消息（**纯文本、不带 emoji**）。

    为什么不带 emoji：消息最终可能经过 GBK 环境（Windows 控制台/日志），
    emoji 会引发 `UnicodeEncodeError` —— 这是接入 QQ 推送时踩过的坑。
    """
    from datetime import datetime
    head = datetime.now().strftime("%m-%d")
    gap = gap_line(gaps)
    tail = f" ｜另：{gap}" if gap else ""
    if not problems:
        return f"【每日签到 {head}】{_done_line(results)}{tail}"
    parts = []
    login = [r.site_id for r in problems if r.outcome in _LOGIN_NEEDED]
    broken = [r.site_id for r in problems if r.outcome not in _LOGIN_NEEDED]
    if login:
        parts.append("需登录 " + "、".join(login))
    if broken:
        parts.append("出错 " + "、".join(broken))
    return f"【签到需处理 {head}】{' · '.join(parts)}{tail}"


def _push_qq(root: Optional[str], text: str) -> None:
    """把 `text` 推到配置好的 QQ 群。任何失败都只记日志，绝不影响签到结果。"""
    qq = _load_qq_cfg(root)
    if not qq:
        return
    app_id = str(qq["app_id"]).strip()
    token = _qq_access_token(app_id, str(qq["app_secret"]).strip())
    if not token:
        return
    url = f"{_QQ_API_BASE}/v2/groups/{str(qq['group_openid']).strip()}/messages"
    try:
        resp = _http_post_json(
            url,
            # msg_type=0 纯文本；主动消息**不带 msg_id**（不是回复某条消息）
            {"content": text, "msg_type": 0},
            headers={"Authorization": f"QQBot {token}", "X-Union-Appid": app_id},
        )
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:                                       # noqa: BLE001
            pass
        log.warning("QQ 推送失败：HTTP %s %s", e.code, detail)
        return
    except Exception as e:                                      # noqa: BLE001
        log.warning("QQ 推送失败：%s", e)
        return
    log.info("QQ 推送成功：%s", str((resp or {}).get("id") or "")[:40])

