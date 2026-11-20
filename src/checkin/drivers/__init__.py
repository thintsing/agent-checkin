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
    from .uia_client import UiaClientDriver

    table = {
        "auto": BrowserPageDriver,     # 走 HTTP 接口
        "manual": ReminderDriver,      # 只提醒
        "client": ClientDriver,        # 驱动桌面客户端自己完成签到（Qoder 点 UI / WorkBuddy 走 bridge）
        # 2026-11-20 新增：客户端把调试端口堵死时的最后一条路 ——
        # 走 Windows 辅助功能通道（UIA）操作原生控件。判据同样要求"点击后复核"。
        "uia": UiaClientDriver,
    }
    cls = table.get(mode)
    return cls() if cls else None
