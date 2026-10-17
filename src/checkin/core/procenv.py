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
  * stderr 报 `bad option: --remote-debugging-port=9337` —— 这是 **Node 的参数解析器**报的，
    不是应用拒绝该参数

对照实测（同一台机器、同一份 exe）：

    A) 原始环境（继承 ELECTRON_RUN_AS_NODE=1） → 40s 端口未就绪，只剩 helper 进程
    B) 清理后环境                              → 2s 端口就绪，主进程正常带参数启动

⇒ 结论：**不是"必须落在交互式桌面"，而是环境变量污染**。
   `explorer.exe` / 计划任务之所以"能成功"，只是因为它们顺带提供了干净环境。
"""
from __future__ import annotations

import ctypes
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


# GetExitCodeProcess 对**还没退出**的进程返回这个值（Windows 就这么定义）
_STILL_ACTIVE = 259


def pid_alive(pid: int) -> bool:
    """这个 PID 还在吗 —— 用 Windows `OpenProcess` + `GetExitCodeProcess`，不起子进程。

    **为什么不用 `os.kill(pid, 0)`**：实测在本机（含 Agent 沙箱）它对**所有** PID
    都抛 OSError（进程隔离），会把活进程全判成死的。2026-10-15 就因此把三个活着的
    调试端口误判成僵尸。

    ⚠ **拿到句柄 ≠ 进程活着**（2026-10-17 实测，这条守卫当时竟然是绿的）：
    `OpenProcess` 对**已经退出、但句柄还被别人握着**的"僵尸"进程照样返回句柄 ——
    只要父进程没关掉 `Popen` 的句柄，进程对象就不会销毁，于是句柄一直拿得到。
    实测 6 个确认已退出的 PID，`OpenProcess` **六个全部成功**。
    ⇒ 只看句柄的话 `pid_alive` 恒为 True，而它是 `RunLock._is_stale()` 判活的
    唯一依据：**锁永远不会被判陈旧** —— 机制从写下那一刻起就是死的，
    却因为"代码在、测试绿"而看不出来（本项目第二次栽在"名存实亡"上，见 MEMORY.md）。
    ⇒ 必须再调 `GetExitCodeProcess`：只有退出码仍是 `STILL_ACTIVE`(259) 才算活着。

    拿不准（API 不可用 / 非 Windows / 取不到退出码）时返回 True ——
    **宁可漏判死亡，也别把活的说成死的**（后者会并发重复签到，代价大得多）。
    """
    try:
        kernel32 = ctypes.windll.kernel32
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        handle = open_process(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True                     # 取不到退出码 ⇒ 拿不准，当活着
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                                   # noqa: BLE001
        return True


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
