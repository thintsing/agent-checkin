"""本地状态：当日幂等、连续天数、熔断计数。存 JSON，不含任何凭证。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

CST = timezone(timedelta(hours=8))  # 站点按 UTC+8 0:00 刷新


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
