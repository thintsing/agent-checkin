"""单实例互斥锁：防止两个签到进程并发抢跑导致重复请求（对"防封"有意义）。

用独占创建锁文件实现；写入 pid+时间戳，遇陈旧锁（进程已不在或超时）自愈接管。
"""
from __future__ import annotations

import json
import os
import time


class RunLock:
    def __init__(self, path: str, stale_after_sec: int = 30 * 60):
        self.path = path
        self.stale_after = stale_after_sec
        self._held = False

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._is_stale():
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                return False
            else:
                with os.fdopen(fd, "w") as f:
                    json.dump({"pid": os.getpid(), "ts": int(time.time())}, f)
                self._held = True
                return True
        return False

    def _is_stale(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            return True
        if int(info.get("ts", 0)) + self.stale_after < time.time():
            return True
        # 进程已不存在则视为陈旧（Windows 无 os.kill(pid,0) 语义，保守只按时间判断）
        return False

    def release(self) -> None:
        if self._held:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self._held = False

    def __enter__(self):
        if not self.acquire():
            raise BlockingIOError(f"锁被占用: {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()
        return False
