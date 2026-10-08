"""日志：控制台 + 按日文件，带凭证脱敏过滤器。"""
from __future__ import annotations

import logging
import os
import re
import sys
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
        # 形如 "token": "xxxx" 或 token=xxxx 的值打码。
        #
        # **必须在 keys 非空时才拼这条正则**：老写法在 keys 为空时退化成一个
        # 不含任何捕获组的 `(?!)`，而替换串 `\1***` 里却引用着组 1 —— 于是
        # 每条日志都抛 `re.error: invalid group reference 1`。只有"把 redact_keys
        # 配成空列表"才会踩到，而默认配置里是有值的，所以它一直没被发现（2026-10-08
        # 写测试时顺手炸出来）。
        if self.keys:
            clean = re.sub(
                r"(?i)([\"']?(?:%s)[\"']?\s*[:=]\s*[\"']?)([^\"',\s}]+)"
                % "|".join(re.escape(k) for k in self.keys),
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

    # pythonw.exe（计划任务用的解释器，为的是不弹黑框）下 `sys.stderr` 是 None：
    # StreamHandler 会拿到空流，emit 时抛异常 —— 逐条日志报错，还会连带吞掉
    # 真正有用的信息。没有控制台就只写文件。
    if sys.stderr is not None:
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
