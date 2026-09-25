"""拟人化节奏与退避计算。"""
from __future__ import annotations

import random
from typing import List


def sleep_range(rng: List[float]) -> float:
    """返回一个区间内的随机秒数（供调用方 sleep）。"""
    lo, hi = float(rng[0]), float(rng[1])
    return random.uniform(lo, hi)


def backoff_sec(attempt: int, base: float = 30.0, cap: float = 300.0) -> float:
    """指数退避 + 抖动。attempt 从 1 开始。"""
    raw = min(cap, base * (3 ** (attempt - 1)))
    return raw + random.uniform(0, raw * 0.25)
