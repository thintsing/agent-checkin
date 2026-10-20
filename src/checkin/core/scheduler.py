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


def _fallback_window() -> tuple[str, str]:
    """窗口兜底值 —— 唯一真值是 `ScheduleConfig` 的 dataclass 默认值。

    放在函数里 import：`config` 只在测试/调用时才会被加载，避免模块级循环导入。
    """
    from .config import ScheduleConfig
    return ScheduleConfig.window_start, ScheduleConfig.window_end


def window_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    ref = now or datetime.now()
    # 兜底**引用** `ScheduleConfig` 的默认值，不在这里再写一遍字面量 ——
    # 窗口值曾散在四处（本处 / ScheduleConfig / config.load_config / config.yaml），
    # 只改一处就会静默失同步：它们只在"配置键缺失"时才生效，正常跑永远看不出差异
    # （DESIGN_NOTES 记过一次，是最隐蔽的那类坑）。守卫见
    # tests.test_core.TestScheduleWindowConsistency。
    fallback_start, fallback_end = _fallback_window()
    sh, sm = _parse_hhmm(getattr(_cfg, "window_start", fallback_start)) if _cfg \
        else _parse_hhmm(fallback_start)
    eh, em = _parse_hhmm(getattr(_cfg, "window_end", fallback_end)) if _cfg \
        else _parse_hhmm(fallback_end)
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
