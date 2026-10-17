"""核心数据结构与判定语义。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Outcome(str, Enum):
    """一次签到尝试的结果语义。"""

    SUCCESS = "success"        # 成功领取
    ALREADY = "already"        # 今日已签（视为成功，不重试）
    NEED_LOGIN = "need_login"  # 会话失效：转人工，绝不重试硬刚
    CIRCUIT = "circuit"        # 触发熔断：当日不再碰该站点
    NO_ACTION = "no_action"    # 未知 code / 活动结束：不动作，只记日志
    SKIPPED = "skipped"        # 当日已完成，按幂等策略跳过
    ERROR = "error"            # 网络/未知错误


# 属于"真正失败、可重试/计入熔断"的语义
FAILURE_OUTCOMES = {Outcome.ERROR}
# 需要人工介入、不重试、直接停该站点的语义
STOP_OUTCOMES = {Outcome.NEED_LOGIN}
# 当日视为已处理、幂等可跳过的语义
DONE_OUTCOMES = {Outcome.SUCCESS, Outcome.ALREADY}


@dataclass
class Action:
    method: str = "GET"
    path: str = ""
    body: Optional[Dict[str, Any]] = None


@dataclass
class Session:
    kind: str = "browser_page"
    start_url: str = ""
    tab_match: List[str] = field(default_factory=list)
    token_localstorage_key: str = ""


@dataclass
class Verdict:
    transport: str = "body_code"
    rules: List[Dict[str, Any]] = field(default_factory=list)
    http_401_result: str = "need_login"
    unknown_result: str = "no_action"
    # 响应体 data 里表示"今天已领过"的布尔字段名（WorkBuddy 是 today_checked_in）。
    # 为什么要单独一个字段：业务 code=0 只说明"这次请求成功"，不区分
    # "我刚领到" 与 "我今天早领过了"。少了它就无法在已签时收手。
    checked_in_flag: str = ""
    # 响应体 data 里表示"业务结论"的字段名，以及它的取值到 Outcome 的映射。
    # 依据是客户端自己的判别词表 CheckinClaimStatus（claimed / already_claimed /
    # not_eligible / event_ended / unknown_biz_error）。只看 code 会把
    # "活动已结束"（code 仍是 0）误判成"已领取"—— 这是最危险的假成功。
    result_flag: str = ""
    result_map: Dict[str, str] = field(default_factory=dict)

    def map(self, http_status: Optional[int], code: Any) -> Outcome:
        if code is not None:
            for rule in self.rules:
                if rule.get("code") == code:
                    return Outcome(rule.get("result", "no_action"))
        if http_status in (401, 403):
            return Outcome(self.http_401_result)
        return Outcome(self.unknown_result)

    def already_checked_in(self, flags: Optional[Dict[str, Any]]) -> bool:
        """按 checked_in_flag 判断服务端是否已声明"今日已签"。

        刻意不放进 map()：map 是跨 Agent 冻结的接缝（见 AGENTS.md 第三节），
        而"读哪个字段算已签"是站点知识，属于配方层。
        """
        if not self.checked_in_flag or not isinstance(flags, dict):
            return False
        return flags.get(self.checked_in_flag) is True

    def map_flags(self, flags: Optional[Dict[str, Any]]) -> Optional[Outcome]:
        """按 result_flag 的业务词表判定；字段缺失/取值不认识时返回 None（交给 map 兜底）。"""
        if not self.result_flag or not isinstance(flags, dict):
            return None
        raw = flags.get(self.result_flag)
        if raw is None:
            return None
        mapped = self.result_map.get(str(raw))
        return Outcome(mapped) if mapped else None


@dataclass
class Recipe:
    id: str
    name: str
    enabled: bool = True
    mode: str = "auto"                       # auto | manual | client | disabled
    origin: str = ""
    session: Session = field(default_factory=Session)
    actions: Dict[str, Action] = field(default_factory=dict)
    verdict: Verdict = field(default_factory=Verdict)
    reminder: Dict[str, Any] = field(default_factory=dict)
    # mode=client 专用：怎么把这台机器上的桌面客户端拉起来、点哪里。
    # 与 reminder 分开是因为语义不同 —— reminder 是"给人看的话"，
    # client 是"给程序执行的步骤"。混在一起以后必然有人把 toast 文案改坏自动化。
    client: Dict[str, Any] = field(default_factory=dict)
    safety_overrides: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Recipe":
        sess = d.get("session") or {}
        acts = {}
        for key in ("status", "trigger"):
            if d.get("actions", {}).get(key):
                a = d["actions"][key]
                acts[key] = Action(a.get("method", "GET"), a.get("path", ""), a.get("body"))
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            enabled=bool(d.get("enabled", True)),
            mode=d.get("mode", "auto"),
            origin=d.get("origin", ""),
            session=Session(
                kind=sess.get("kind", "browser_page"),
                start_url=sess.get("start_url", ""),
                tab_match=sess.get("tab_match", []),
                token_localstorage_key=sess.get("token_localstorage_key", "") or "",
            ),
            actions=acts,
            verdict=Verdict(**(d.get("verdict") or {})),
            reminder=d.get("reminder") or {},
            client=d.get("client") or {},
            safety_overrides=d.get("safety_overrides") or {},
        )


@dataclass
class RunBudget:
    """**整轮共享**的时间预算 —— 防止一个站点把后面的站点全部饿死。

    为什么必须有（2026-10-17 事故，代价是三站全军覆没）：
    站点是**串行**跑的，而 client 驱动里有一个长达 `wait_port_sec`（当时 5400 秒）
    的"守候端口"分支。当天 qoder 卡在这个分支上，于是排在它后面的 trae / workbuddy
    **根本没被执行** —— 一轮跑完，日志里只有一行 qoder 的警告，另外两站连影子都没有。
    更隐蔽的是：**那次连通知都没发出去**（进程还在跑，没走到汇总那一步），
    用户只能自己去看日志才发现。

    ⇒ 结论是两条，缺一不可：
      ① **一个站点的等待必须有上限**（`pass_index=1` 时压缩到 `cap`）；
      ② 等待也得让出 CPU —— 卡住的站点**退出本轮**，把后面的站点先跑完，
         有余力再回来补（`pass_index=2`）。

    `deadline_monotonic`：本轮到期的 `time.monotonic()` 读秒；**0 = 不限**（手动跑的场景）。
    `pass_index`：1 = 快扫一遍（不许长守候）；2 = 回头补领（允许守候到预算耗尽）。
    """

    deadline_monotonic: float = 0.0
    pass_index: int = 1

    def left(self) -> Optional[float]:
        """还剩多少秒；`None` = 不限（没设预算）。耗尽时返回 0.0，不是负数。"""
        if not self.deadline_monotonic:
            return None
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def clamp(self, want: float, cap_in_pass1: Optional[float] = None) -> float:
        """把"想要等待的秒数"压到本轮真正能给的范围里。

        两道夹取依次生效，顺序不能反：
          ① **角色夹**：第一遍（快扫）时压到 `cap_in_pass1` —— 这一步与预算无关，
             它的目的是让"卡住"的站点**立刻让位**，而不是吃掉整轮预算。
          ② **预算夹**：无论如何不能超过整轮剩余时间。
        """
        if self.pass_index <= 1 and cap_in_pass1 is not None:
            want = min(want, float(cap_in_pass1))
        left = self.left()
        if left is not None:
            want = min(want, left)
        return max(0.0, want)


@dataclass
class CheckinResult:
    site_id: str
    outcome: Outcome
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.SUCCESS, Outcome.ALREADY)
