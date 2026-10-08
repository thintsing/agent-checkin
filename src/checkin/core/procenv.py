"""从"被 Agent 污染的环境"里派生子进程时，必须先把环境洗干净。

**为什么需要这个模块（2026-09-25 实测定位的根因）**

WorkBuddy 以 Electron 运行时启动，会把下面两个变量注入到所有子进程：

    ELECTRON_RUN_AS_NODE=1
    NODE_OPTIONS=--require=".../WorkBuddy/resources/app.asar.unpacked/cli/vendor/shim/node-language-shim.cjs"

任何 **Electron 应用**（Qoder CN、VS Code 系…）继承这两个变量后都会**退化成 Node 模式**：
不初始化 Chromium、不建窗口、不绑 `--remote-debugging-port`，把主脚本跑完就退出。

症状（极具误导性，很容易误判成"启动方式不对"）：
  * 进程数恒为 1，剩下的是一个 `xxx-app-host.cjs`（那正是被当作 Node 脚本执行的它）
  * 应用日志目录**不产生新 session**
  * `--enable-logging --log-file=` **不生成文件**
  * stderr 报 `bad option: --remote-debugging-port=9335` —— 这是 **Node 的参数解析器**报的，
    不是应用拒绝该参数

对照实测（同一台机器、同一份 exe）：

    A) 原始环境（继承 ELECTRON_RUN_AS_NODE=1） → 40s 端口未就绪，只剩 helper 进程
    B) 清理后环境                              → 2s 端口就绪，主进程正常带参数启动

⇒ 结论：**不是"必须落在交互式桌面"，而是环境变量污染**。
   `explorer.exe` / 计划任务之所以"能成功"，只是因为它们顺带提供了干净环境。
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional, Sequence

# 精确匹配：这些变量一旦被 Electron 应用继承，就会把它变成 Node 跑腿
_BAD_EXACT = frozenset({
    "ELECTRON_RUN_AS_NODE",        # 决定性变量
    "NODE_OPTIONS",                # 会把 WorkBuddy 的 shim 注入进去
    "NODE_ENV",
    "CHROME_CRASHPAD_PIPE_NAME",   # 指向父进程 crashpad 管道，跨进程无效
    "BASH_ENV",
    "AGENT_BROWSER_ARGS",
    "BROWSER_USE_CHROMIUM_SANDBOX",
})

# 前缀匹配：Agent 自身的一整套变量，对被测应用没有意义，删掉更干净
_BAD_PREFIX = ("CODEBUDDY_", "CLAUDE_", "WORKBUDDY_")

# 派生**控制台子系统**的子进程（powershell.exe 之流）时必须带上它。
#
# 计划任务用 `pythonw.exe`（无控制台）运行，目的就是不弹黑框；而 Windows 的规则是：
# 一个没有控制台的父进程再去启动控制台程序、又没声明 CREATE_NO_WINDOW 时，
# 系统会**为它新建一个可见的控制台窗口**。于是"消灭黑框"的努力会被一次
# `subprocess.run(["powershell", ...])` 原样还回来（2026-10-08 排查）。
# 非 Windows 上该常量不存在，取 0 即可（无此概念）。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def clean_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """返回一份"去掉 Agent 污染"的环境变量副本。不改动传入的 base。"""
    env = dict(os.environ if base is None else base)
    for k in list(env):
        if k in _BAD_EXACT or k.startswith(_BAD_PREFIX):
            env.pop(k, None)
    return env


def removed_names(base: Optional[Dict[str, str]] = None) -> List[str]:
    """调试用：列出会被清掉的变量名。"""
    b = os.environ if base is None else base
    return sorted(k for k in b if k in _BAD_EXACT or k.startswith(_BAD_PREFIX))


def spawn(cmd: Sequence[str], cwd: Optional[str] = None,
          env: Optional[Dict[str, str]] = None,
          extra: Optional[Dict[str, str]] = None) -> subprocess.Popen:
    """以干净环境派生一个分离的 GUI 进程。

    - 默认 `env=None` 也走清洗（这正是本模块存在的意义，不要绕过）
    - `close_fds=True`：不要把 Agent 的句柄泄漏给被测应用
    - 输出一律丢弃：GUI 应用的 stdout/stderr 对我们没用，且写满管道会阻塞子进程
    """
    e = clean_env(env)
    if extra:
        e.update(extra)
    return subprocess.Popen(
        list(cmd), cwd=cwd, env=e, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
