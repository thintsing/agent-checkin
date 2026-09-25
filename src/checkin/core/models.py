"""核心数据结构与判定语义。"""
from __future__ import annotations

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
class CheckinResult:
    site_id: str
    outcome: Outcome
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.SUCCESS, Outcome.ALREADY)
