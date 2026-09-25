"""manual 驱动：只做提醒，不碰任何业务接口/桌面控件（Qoder 用此模式）。"""
from __future__ import annotations

import logging
import os
import subprocess

from ..core.models import CheckinResult, Outcome, Recipe
from ..core import notify
from . import Driver

log = logging.getLogger("checkin.driver.reminder")


class ReminderDriver(Driver):
    mode = "manual"

    def run(self, recipe: Recipe, cfg, dry_run: bool = False, probe: bool = False) -> CheckinResult:
        rem = recipe.reminder or {}
        target = (rem.get("open_target") or "").strip()
        opened = False

        if target and not (dry_run or probe):
            opened = self._try_open(target)

        title = rem.get("toast_title") or f"{recipe.name} 提醒"
        body = rem.get("toast_body") or "请手动完成今日签到。"
        if not (dry_run or probe):
            notify._toast(title, body)

        note = rem.get("note", "")
        msg = ("已唤起客户端，请在浮窗点「签到」" if opened else "已发送签到提醒")
        if note:
            log.info("[%s] %s", recipe.id, note)
        return CheckinResult(recipe.id, Outcome.NO_ACTION, message=msg + "（人工确认）",
                             detail={"opened": opened})

    @staticmethod
    def _try_open(target: str) -> bool:
        try:
            if os.path.isfile(target):
                subprocess.Popen([target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            os.startfile(target)  # 支持 URL scheme / 已注册协议 / 应用别名
            return True
        except OSError as e:
            log.debug("唤起目标失败: %s", e)
            return False
