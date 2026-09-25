"""加载 config.yaml 与 recipes/*.yaml。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

import yaml

from .models import Recipe


def expand(value: str) -> str:
    """展开 %VAR% 与 ~ 风格路径（Windows 环境变量）。"""
    if not value:
        return value
    return os.path.expandvars(os.path.expanduser(value))


@dataclass
class ChromeConfig:
    remote_debugging_port: int = 9333
    user_data_dir: str = "%LOCALAPPDATA%\\AgentCheckIn\\chrome-profile"
    executable: str = ""
    startup_timeout_sec: int = 25


@dataclass
class SafetyConfig:
    pre_delay_range: List[float] = field(default_factory=lambda: [1.5, 6.0])
    inter_site_gap_range: List[float] = field(default_factory=lambda: [25, 90])
    max_retries: int = 2
    circuit_break_after: int = 3
    skip_when_done_today: bool = True


@dataclass
class ScheduleConfig:
    """每日执行的随机时间窗。

    刻意不固定整点：计划任务只在窗口前唤醒，真正的签到时刻由
    scheduler 在窗口内随机取样（Beta 分布，向中间聚集）。
    """

    window_start: str = "10:05"
    window_end: str = "12:30"
    distribute: str = "random"       # random | earliest | latest
    skip_weekends: bool = False
    timezone: str = "Asia/Shanghai"


@dataclass
class AppConfig:
    root: str
    chrome: ChromeConfig
    safety: SafetyConfig
    state_path: str
    log_dir: str
    log_level: str = "INFO"
    redact_keys: List[str] = field(default_factory=list)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)


def load_config(root: str) -> tuple[AppConfig, List[Recipe]]:
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    ch = raw.get("chrome", {})
    sa = raw.get("safety", {})
    st = raw.get("state", {})
    lg = raw.get("logging", {})
    sc = raw.get("schedule", {}) or {}

    cfg = AppConfig(
        root=root,
        chrome=ChromeConfig(
            remote_debugging_port=int(ch.get("remote_debugging_port", 9333)),
            user_data_dir=expand(ch.get("user_data_dir", ChromeConfig.user_data_dir)),
            executable=expand(ch.get("executable", "")),
            startup_timeout_sec=int(ch.get("startup_timeout_sec", 25)),
        ),
        safety=SafetyConfig(
            pre_delay_range=list(sa.get("pre_delay_range", [1.5, 6.0])),
            inter_site_gap_range=list(sa.get("inter_site_gap_range", [25, 90])),
            max_retries=int(sa.get("max_retries", 2)),
            circuit_break_after=int(sa.get("circuit_break_after", 3)),
            skip_when_done_today=bool(sa.get("skip_when_done_today", True)),
        ),
        state_path=expand(st.get("path", "data/state.json")),
        log_dir=expand(lg.get("dir", "logs")),
        log_level=lg.get("level", "INFO"),
        redact_keys=lg.get("redact_keys", []),
        schedule=ScheduleConfig(
            window_start=str(sc.get("window_start", "10:05")),
            window_end=str(sc.get("window_end", "12:30")),
            distribute=str(sc.get("distribute", "random")),
            skip_weekends=bool(sc.get("skip_weekends", False)),
            timezone=str(sc.get("timezone", "Asia/Shanghai")),
        ),
    )
    # 相对路径锚定到项目根
    if not os.path.isabs(cfg.state_path):
        cfg.state_path = os.path.join(root, cfg.state_path)
    if not os.path.isabs(cfg.log_dir):
        cfg.log_dir = os.path.join(root, cfg.log_dir)

    recipes: List[Recipe] = []
    rdir = os.path.join(root, "recipes")
    if os.path.isdir(rdir):
        for name in sorted(os.listdir(rdir)):
            if name.endswith((".yaml", ".yml")):
                with open(os.path.join(rdir, name), encoding="utf-8") as f:
                    recipes.append(Recipe.from_dict(yaml.safe_load(f)))
    return cfg, recipes


# 便捷：把配置里 dict 型 actions 访问收口
def action_or_none(recipe: Recipe, name: str):
    return recipe.actions.get(name)
