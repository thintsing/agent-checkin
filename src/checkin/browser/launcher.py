"""拉起带远程调试端口的专用 Chrome，并等待就绪。"""
from __future__ import annotations

import glob
import logging
import os
import subprocess
import time
import urllib.request

log = logging.getLogger("checkin.browser")

# 本地回环一律强制绕过代理。
#
# 踩过的坑（2026-09-24 实测）：会话环境里有 HTTP_PROXY=http://127.0.0.1:52715
# 而**没有** NO_PROXY，于是 urllib 把 127.0.0.1 也丢给代理，DevTools 端点回 502，
# 被误判成"端口未就绪" → 反复拉起新实例 → 25s 超时 → 整个签到失败。
#
# 注意：这跟 Windows 系统代理（注册表 ProxyOverride 里带 127.*）无关 ——
# getproxies() 优先读环境变量，之后 proxy_bypass_environment() 在没设 no_proxy
# 时一律返回"不绕过"，注册表的例外名单根本轮不到生效。所以别以为
# "系统代理配了例外就没事了"，这里必须自己硬绕。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def find_chrome(override: str) -> str:
    if override and os.path.isfile(override):
        return override
    candidates = [
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    hits = glob.glob(r"C:\Program Files*\*\*\chrome.exe")
    return hits[0] if hits else ""


def port_ready(port: int, timeout: float = 1.0) -> bool:
    try:
        with _LOCAL_OPENER.open(f"http://127.0.0.1:{port}/json/version",
                                timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_chrome(cfg, start_url: str) -> bool:
    """确保调试端口就绪；未就绪则启动独立 profile 的 Chrome。返回是否新启。"""
    port = cfg.chrome.remote_debugging_port
    if port_ready(port):
        log.debug("调试端口 %s 已就绪（复用现有 Chrome）", port)
        return False

    exe = find_chrome(cfg.chrome.executable)
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge，请在 config.yaml 的 chrome.executable 指定路径")

    os.makedirs(cfg.chrome.user_data_dir, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={cfg.chrome.user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # 专用浏览器是长驻的，必须压制一切"后台降级"，否则标签页会被冻结，
        # 页面里的 await fetch 永不 resolve，表现为 CDP 读超时（2026-09-27 踩过）。
        # 注意：这两个开关只在**新启动**时生效；复用已在跑的实例要靠 CDPPage.wake()。
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        start_url,
    ]
    log.info("启动签到专用 Chrome：%s", os.path.basename(exe))
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + cfg.chrome.startup_timeout_sec
    while time.time() < deadline:
        if port_ready(port):
            return True
        time.sleep(0.5)
    raise TimeoutError(f"Chrome 调试端口 {port} 在 {cfg.chrome.startup_timeout_sec}s 内未就绪")
