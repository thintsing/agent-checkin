"""凌晨把"没带调试端口"的客户端重启成带端口的形态。

为什么需要这个工具（2026-10-25 定的取舍）
----------------------------------------
调试端口**只能在进程启动时绑定**，运行中加不上去。而 WorkBuddy 会**自己重启**
（更新完成、崩溃恢复、用户关机重开），那些路径**不吃启动入口的参数**
⇒ 第二天签到必然 `no_action`（10-23 只等到 20:24 用户手动签、10-25 连试 4 次全废）。

"不重启就签到"的六条路已经全部堵死（实证，别重走）：
  · CDP 直连 —— 没端口就没有入口，这是 Chromium 的设计；
  · Windows UIA —— 客户端没启用 a11y，控件树只有 9 个无名节点；
  · 本地 HTTP —— 主进程的 18488 只是 LocalProbeServer，仅 /workbuddy/probe 探活；
  · MCP —— 本机 68 个工具里没有任何签到能力；
  · CLI —— `codebuddy --help` 只有 config/mcp/daemon/ps 这类运维命令；
  · 会话凭据直调 —— 拿 MCP 的 Bearer 打 checkin-status，401（用途不同）。
⇒ 唯一可行的是**换一个时间点重启**：挪到凌晨 3 点，用户不在的时候做。

分寸
----
* 端口**已经通** ⇒ 立刻退出，绝不无谓打断；
* 客户端**不在跑** ⇒ 直接用带端口命令行拉起（顺手把入口修好的状态用上）；
* **忙就跳过**（CPU 采样超阈值）—— 凌晨也可能挂着长任务，宁可少签一天也不砍人的活；
* 全程写 `logs/nightly-restart.log`，出问题能倒查。

用法
----
    python tools/nightly_restart_client.py --app workbuddy --dry-run
    python tools/nightly_restart_client.py --app workbuddy          (给计划任务用)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from checkin.core.config import load_config                      # noqa: E402
from checkin.drivers.client_claim import ClientDriver             # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "nightly-restart.log")


def log(msg: str) -> None:
    line = "%s %s" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    # `pythonw.exe` 下 sys.stdout 是 None，直接 print 会 AttributeError 把整轮打死
    # （本项目的老坑：计划任务里 pythonw 的 stdio 全为 None）。
    try:
        if sys.stdout is not None:
            print(line)
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="把无调试端口的客户端在凌晨重启成带端口形态")
    ap.add_argument("--app", default="workbuddy", help="配方 id（默认 workbuddy）")
    ap.add_argument("--dry-run", action="store_true", help="只打印决策，不动进程")
    ap.add_argument("--ignore-busy", action="store_true", help="无视忙闲（调试用，勿配进计划任务）")
    ap.add_argument("--idle-sec", type=float, default=3.0,
                    help="5 秒观测窗内合计 CPU 秒数上限，低于它才算空闲（默认 3.0）")
    args = ap.parse_args()

    _, recipes = load_config(PROJECT_ROOT)
    recipe = next((r for r in recipes if r.id == args.app), None)
    if recipe is None:
        log("[%s] 找不到配方，退出" % args.app)
        return 2
    c = recipe.client or {}
    port = int(c.get("debug_port") or 0)
    if not port:
        log("[%s] 配方没有 debug_port，退出" % args.app)
        return 2

    drv = ClientDriver()
    if drv._port_alive(port):                      # noqa: SLF001 - 与主流程同一判据
        log("[%s] 端口 %d 已就绪，无需重启" % (args.app, port))
        return 0

    running = drv._app_running(c)                  # noqa: SLF001
    if running:
        # 自己采样而不用 `_cpu_busy`：后者只回 bool，日志里留不下数值，
        # 而"夜里到底忙不忙"恰恰是这条路径会不会**名存实亡**的关键证据。
        if args.ignore_busy:
            log("[%s] --ignore-busy：跳过忙闲判断" % args.app)
        else:
            import time as _t
            name = str(c.get("process_match") or "") or os.path.basename(
                drv._resolve_exe(c) or "")             # noqa: SLF001
            before = drv._cpu_total(name)              # noqa: SLF001
            _t.sleep(5.0)
            after = drv._cpu_total(name)               # noqa: SLF001
            if before is None or after is None:
                log("[%s] CPU 采样失败（取不到进程累计时间），按「看不清不动手」跳过" % args.app)
                return 0
            delta = round(after - before, 2)
            if delta >= args.idle_sec:
                log("[%s] 客户端正忙：5s 内 CPU %.2f 秒 ≥ 阈值 %.1f，跳过今晚的重启"
                    % (args.app, delta, args.idle_sec))
                return 0
            log("[%s] 空闲判定通过：5s 内 CPU %.2f 秒 < 阈值 %.1f"
                % (args.app, delta, args.idle_sec))
        log("[%s] 客户端在跑但无调试端口，准备重启：%s" % (args.app, running[:100]))
    else:
        log("[%s] 客户端没在跑，直接带端口拉起" % args.app)

    if args.dry_run:
        log("[%s] --dry-run：本应重启并等待端口 %d，实际未执行" % (args.app, port))
        return 0

    ok = drv._restart_client(c, port)              # noqa: SLF001 - 复用主流程的干净重启
    if ok and drv._port_alive(port):               # noqa: SLF001
        log("[%s] 重启完成，端口 %d 就绪" % (args.app, port))
        return 0
    log("[%s] 重启后端口仍未就绪（ok=%s），留给白天那轮自愈" % (args.app, ok))
    return 1


if __name__ == "__main__":
    sys.exit(main())
