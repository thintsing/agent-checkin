"""本地状态：当日幂等、连续天数、熔断计数。存 JSON，不含任何凭证。"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

log = logging.getLogger("checkin.state")

CST = timezone(timedelta(hours=8))  # 站点按 UTC+8 0:00 刷新

# 补报缺口时最多往前数多少天。只是防止状态库被人为改坏后刷出一屏日期，
# 正常路径下不会碰到（连续漏 14 天以上，人早该来看了）。
_MAX_GAP_DAYS = 14

# "昨日缺口已播报"的戳记键。刻意放在顶层（不进 sites/）：它描述的是**这一轮**
# 说过什么，不是某个站点的签到事实 —— 混进站点数据会被 streak / 幂等读到。
_GAP_NOTICE_KEY = "gap_notice"


def today_key(tz: Optional[str] = None) -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self._data: Dict[str, Any] = {"sites": {}}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {"sites": {}}
        self._data.setdefault("sites", {})

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @property
    def data(self) -> Dict[str, Any]:
        """只读视图，给 CLI / 诊断用，避免外部直接摸 _data。"""
        return self._data

    def _site(self, site_id: str) -> Dict[str, Any]:
        return self._data["sites"].setdefault(site_id, {})

    def done_today(self, site_id: str) -> bool:
        s = self._site(site_id)
        return s.get("last_done_date") == today_key() and s.get("last_done") in ("success", "already")

    def prompted_today(self, site_id: str) -> bool:
        """manual 站点：今天是否已经提醒过。**不代表站点侧已完成。**

        为什么要跟 done_today 分开，而不是合并成一个"今天处理过了"：
        两者的**证据等级**不同，混用会污染统计。

        - `done_today`     —— 服务端已确认"今日已完成"（有接口证据）。驱动 streak。
        - `prompted_today` —— 我们这一侧尽了告知义务（发过提醒）。**不驱动 streak**。

        manual 站点永远拿不到服务端证据，所以它只配参与"今天别再弹一次"这一个判断；
        让它去刷连续天数，等于把"我提醒过你"记成"你签到了"——那是骗自己。
        """
        return self._site(site_id).get("last_prompt_date") == today_key()

    def record_prompt(self, site_id: str) -> None:
        """记下"今天已提醒过"。刻意不碰 streak / failures / last_done。"""
        s = self._site(site_id)
        s["last_prompt_date"] = today_key()
        s["updated_at"] = datetime.now(CST).isoformat(timespec="seconds")
        self._save()

    def failures_today(self, site_id: str) -> int:
        s = self._site(site_id)
        if s.get("failure_date") != today_key():
            return 0
        return int(s.get("failures", 0))

    def record(self, site_id: str, outcome: str) -> None:
        s = self._site(site_id)
        today = today_key()
        if outcome in ("success", "already"):
            if s.get("last_done_date") != today:
                # 连续天数：昨天完成则 +1，否则重置为 1
                yest = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
                s["streak"] = (s.get("streak", 0) + 1) if s.get("last_done_date") == yest else 1
            s["last_done_date"] = today
            s["last_done"] = outcome
            s["failure_date"] = today
            s["failures"] = 0
        elif outcome == "error":
            if s.get("failure_date") != today:
                s["failure_date"] = today
                s["failures"] = 0
            s["failures"] = int(s.get("failures", 0)) + 1
        s["updated_at"] = datetime.now(CST).isoformat(timespec="seconds")
        self._save()

    def streak(self, site_id: str) -> int:
        return int(self._site(site_id).get("streak", 0))

    def last_done_date(self, site_id: str) -> str:
        """上次**服务端确认完成**的日期（"YYYY-MM-DD"），从未成功过则返回空串。"""
        return str(self._site(site_id).get("last_done_date") or "").strip()

    def missed_days(self, site_id: str, skip_weekends: bool = False) -> List[str]:
        """上次完成之后、到**昨天**为止，本该签到却没签到的日期列表。

        为什么是"到昨天为止"而不是"到今天"：今天还没跑完，把今天算进去等于
        在结果出来之前就判它失败。

        **为什么必须有这条**（2026-10-17 用户原话："还要我来手工检查"）：
        在此之前，"昨天整天没签到"这件事**不会通知任何人** —— 计划任务被判 missed、
        程序卡在某个守候分支、机器没开机……任何一种情况下，第二天程序照常跑、
        照常报"今日已完成"，而昨天那个洞**永远不会被提起**。
        用户唯一的发现途径是自己去翻日志 —— 这正是要消灭的东西。
        现在改由程序在每天**第一次真正运行**时回头看一眼，并把缺口说出来。

        `skip_weekends` 为真时周末不算缺口（`skip_weekends` 配了的话，
        那些天本来就不该跑，报出来是噪音）。

        只对**能真正领到**的站点有意义（`auto` / `client`）：manual 站点拿不到
        服务端证据，`last_done_date` 长期不动是**设计如此**，不能当缺口报。
        过滤由调用方（`__main__._collect_gaps`）负责 —— 真实配方里三个站点
        全是 `client`，所以那边的口径是"排除 manual"而不是"只认 auto"。
        """
        last = self.last_done_date(site_id)
        if not last:
            return []
        try:
            cursor = datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=1)
        except ValueError:
            log.warning("last_done_date 格式异常（%r），跳过缺口计算", last)
            return []
        today = datetime.now(CST).date()
        out: List[str] = []
        while cursor < today and len(out) < _MAX_GAP_DAYS:
            if not (skip_weekends and cursor.weekday() >= 5):
                out.append(cursor.strftime("%Y-%m-%d"))
            cursor += timedelta(days=1)
        return out

    def gap_notice_today(self) -> str:
        """今天已经播报过的缺口文案；没播报过则返回空串。"""
        note = self._data.get(_GAP_NOTICE_KEY) or {}
        if str(note.get("date") or "").strip() != today_key():
            return ""
        return str(note.get("text") or "").strip()

    def set_gap_notice(self, text: str) -> None:
        """记下"今天已播报过这段缺口"，让同一天的后续触发不再重复说一遍。

        一天有三个触发（主 / 兜底 / 收尾），缺口若每次都报，同样的话要响三遍 ——
        那正是"告警被淹没人就忽略"的开始。所以**一天只说一次**。
        """
        self._data[_GAP_NOTICE_KEY] = {"date": today_key(), "text": text}
        self._save()
