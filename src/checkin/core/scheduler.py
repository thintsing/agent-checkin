"""随机时间窗调度：把"每天准点"打散成窗口内的随机时刻（反封堵）。

引擎默认走"立即执行"(now=True)以配合"本机每天手动点一下"；只有显式启用
窗口模式时，才调用本模块在 window_start–window_end 之间采样一个时刻并等待到点。
"""
from __future__ import annotations

import random
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional


def _parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def window_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    ref = now or datetime.now()
    sh, sm = _parse_hhmm(getattr(_cfg, "window_start", "10:05")) if _cfg else (10, 5)
    eh, em = _parse_hhmm(getattr(_cfg, "window_end", "12:30")) if _cfg else (12, 30)
    start = ref.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = ref.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:  # 非法窗口则退化为 1 小时
        end = start + timedelta(hours=1)
    return start, end


_cfg = None  # 由 plan_today/should_skip_today 注入，避免循环依赖


def should_skip_today(cfg) -> Optional[str]:
    """返回跳过原因（None 表示应执行）。"""
    global _cfg
    _cfg = cfg.schedule
    if cfg.schedule.skip_weekends and date.today().weekday() >= 5:
        return "周末不执行（skip_weekends=true）"
    return None


def plan_today(cfg) -> datetime:
    """在窗口内采样今日的执行时刻。

    Beta(2,2) 让时刻向窗口中部聚集、两端稀疏，避免总是贴着边界。
    earliest/latest 为确定性模式，供调试。若采样时刻已过，引擎按负等待立即执行。
    """
    global _cfg
    _cfg = cfg.schedule
    start, end = window_bounds()
    span = (end - start).total_seconds()
    mode = cfg.schedule.distribute
    if mode == "earliest":
        frac = 0.0
    elif mode == "latest":
        frac = 0.95
    else:
        frac = random.betavariate(2, 2)
    return start + timedelta(seconds=span * frac)


def wait_until(target: datetime, chunk_seconds: float = 60.0,
               on_tick: Optional[Callable[[float], None]] = None) -> None:
    """睡到 target 时刻；分片睡眠便于回调与中断。"""
    while True:
        left = (target - datetime.now()).total_seconds()
        if left <= 0:
            return
        if on_tick:
            on_tick(left)
        time.sleep(min(chunk_seconds, left))
