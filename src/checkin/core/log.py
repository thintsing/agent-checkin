"""日志：控制台 + 按日文件，带凭证脱敏过滤器。"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from .state import today_key

_REDACT_RE = re.compile(r"(?i)(bearer\s+)[a-z0-9._\-]+")


class RedactFilter(logging.Filter):
    def __init__(self, keys):
        super().__init__()
        self.keys = [k.lower() for k in (keys or [])]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = _REDACT_RE.sub(r"\1***", msg)
        # 形如 "token": "xxxx" 或 token=xxxx 的值打码
        clean = re.sub(r"(?i)([\"']?(?:%s)[\"']?\s*[:=]\s*[\"']?)([^\"',\s}]+)" % "|".join(
            [re.escape(k) for k in self.keys]) if self.keys else r"(?!)",
            r"\1***", clean)
        if clean != msg:
            record.msg = clean
            record.args = ()
        return True


def setup(root_log_dir: str, level: str, redact_keys) -> logging.Logger:
    os.makedirs(root_log_dir, exist_ok=True)
    logger = logging.getLogger("checkin")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-6s %(message)s", "%H:%M:%S")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(os.path.join(root_log_dir, f"checkin-{today_key()}.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-6s %(name)s %(message)s"))
    logger.addHandler(fh)

    rf = RedactFilter(redact_keys)
    for h in logger.handlers:
        h.addFilter(rf)

    logging.getLogger("websocket").setLevel(logging.WARNING)
    return logger
