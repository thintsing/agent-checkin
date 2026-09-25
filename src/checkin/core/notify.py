"""通知：控制台摘要 + Windows 桌面 toast（经 PowerShell，无第三方依赖）。

**这个模块的职责是"说人话"，不是"报数据"。** 它只回答一个问题：
"今天有没有需要我处理的？" —— 所以下面的分类逻辑比看起来重要。

处理这块时踩过的坑（2026-09-24 修）：原先完成率的分母是 `len(results)`（全部站点），
分子只数 success/already。于是 `manual` 站点（永远返回 `no_action`）**永远不计入分子、
却永远计入分母** ⇒ 每天都弹「每日签到完成 1/2 个站点已处理」，看着像永远失败一半。
更糟的是**熔断/报错时也照弹这个标题** —— 用户看到"完成"就不会去查，等于没有告警。
"""
from __future__ import annotations

import logging
import subprocess
from typing import List

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


def notify_results(results: List[CheckinResult]) -> None:
    print(summary(results))
    if not results:
        return

    problems = [r for r in results if r.outcome in _PROBLEM]
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
        _toast("签到我处理不了", " · ".join(parts) + f"（{hint}）")
        return

    _toast("每日签到完成", _done_line(results))
