"""单实例互斥锁：防止两个签到进程并发抢跑导致重复请求（对"防封"有意义）。

用独占创建锁文件实现；写入 pid+时间戳，遇陈旧锁（**持锁进程已不在**或超时）自愈接管。

⚠ **为什么必须判"持锁进程还活着"（2026-10-17 实测补的漏洞）**：
进程被杀 / 崩掉 / 手工中断时，`release()` 不会被调用，锁文件就留在那里。
原先只按**时间**判陈旧（30 分钟），于是"进程早就没了、锁还压着 30 分钟" ——
这半小时里任何一次运行（包括兜底与收尾触发）都会被告知"另一实例正在运行"并**整轮跳过**，
而且**不报错、不提醒**，正是本项目最忌讳的静默漏签。
现在先看持锁 PID 在不在：进程没了 ⇒ 锁立即失效，下一轮马上能接上。
（PID 判活在本机要用 `OpenProcess`，见 `core.procenv.pid_alive` 的说明。）
"""
from __future__ import annotations

import json
import os
import time

from . import procenv


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
        # ① 持锁进程已经不在 ⇒ 锁立即失效（进程被杀/崩掉时 release() 不会执行）
        pid = info.get("pid")
        if isinstance(pid, int) and pid > 0 and not procenv.pid_alive(pid):
            return True
        # ② 兜底：判活拿不准时（API 不可用）还留着超时这一条，避免永久压死
        if int(info.get("ts", 0)) + self.stale_after < time.time():
            return True
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
