"""驱动协议：各 mode 的签到执行器共同接口。"""
from __future__ import annotations

from typing import Optional

from ..core.models import CheckinResult, Recipe


class Driver:
    mode: str = ""

    def run(self, recipe: Recipe, cfg, dry_run: bool = False,
            probe: bool = False) -> CheckinResult:  # pragma: no cover - 接口
        """执行一个站点的一次签到。

        ⚠ 本签名是**跨 Agent 冻结的接缝**（见 AGENTS.md 第三节），不能加参数。
        于是"本轮还能等多久"（`core.models.RunBudget`）由引擎挂在 `cfg.budget` 上带下来
        —— 它是**调度层**才知道的信息，由引擎统一决定，驱动不必自己猜。
        """
        raise NotImplementedError


def driver_for(mode: str) -> Optional[Driver]:
    from .browser_page import BrowserPageDriver
    from .client_claim import ClientDriver
    from .reminder import ReminderDriver

    table = {
        "auto": BrowserPageDriver,     # 走 HTTP 接口
        "manual": ReminderDriver,      # 只提醒
        "client": ClientDriver,        # 驱动桌面客户端自己完成签到（Qoder 点 UI / WorkBuddy 走 bridge）
    }
    cls = table.get(mode)
    return cls() if cls else None
