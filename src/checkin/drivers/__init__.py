"""驱动协议：各 mode 的签到执行器共同接口。"""
from __future__ import annotations

from typing import Optional

from ..core.models import CheckinResult, Recipe


class Driver:
    mode: str = ""

    def run(self, recipe: Recipe, cfg, dry_run: bool = False,
            probe: bool = False) -> CheckinResult:  # pragma: no cover - 接口
        raise NotImplementedError


def driver_for(mode: str) -> Optional[Driver]:
    from .browser_page import BrowserPageDriver
    from .client_claim import ClientDriver
    from .reminder import ReminderDriver

    table = {
        "auto": BrowserPageDriver,     # 走 HTTP 接口
        "manual": ReminderDriver,      # 只提醒
        "client": ClientDriver,        # 驱动桌面客户端自己点（Qoder）
    }
    cls = table.get(mode)
    return cls() if cls else None
