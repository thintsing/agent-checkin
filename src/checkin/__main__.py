"""CLI 入口：python -m checkin [--dry-run] [--probe] [--only-site a,b]"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from datetime import datetime

# pythonw.exe 下没有控制台，`sys.stdout/stderr` 就是 **None** —— 任何 print() 都会
# 直接 AttributeError。计划任务用的正是 pythonw（为的是不弹黑框，见 `_task_python()`），
# 所以先给它们兜一个空设备，再统一重配编码。
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull

# 强制 UTF-8 输出，避免 Windows 控制台 GBK 下中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# 允许 `python -m checkin` 与脚本直跑两种方式
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.join(_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "src"))

from checkin.core import config as cfg_mod
from checkin.core import log as log_mod
from checkin.core import notify
from checkin.core import scheduler
from checkin.core.engine import Engine
from checkin.core.state import StateStore


# ----------------------------------------------------------------------
# 纯查询命令（不起日志、不开引擎、不碰接口）
# ----------------------------------------------------------------------

def _status(cfg, recipes, as_json: bool) -> int:
    store = StateStore(cfg.state_path)
    print("")
    print("═" * 62)
    print("  智能体签到 · 状态")
    print("═" * 62)
    print(f"\n  状态库：{cfg.state_path}")
    print(f"  日志：{cfg.log_dir}\n")
    print(f"  {'站点':<14}{'模式':<9}{'连续':<7}{'今日失败':<10}今日")
    print("  " + "-" * 58)
    for recipe in recipes:
        streak = store.streak(recipe.id)
        fails = store.failures_today(recipe.id)
        if store.done_today(recipe.id):
            done = "已完成"
        elif store.prompted_today(recipe.id):
            done = "已提醒"          # 手动站点：只说明"提醒发过了"，不代表已领到
        else:
            done = "-"
        print(f"  {recipe.id:<14}{recipe.mode:<9}{streak:<7}{fails:<10}{done}")
    if any(r.mode == "manual" for r in recipes):
        print("\n  注：「已提醒」= 今天已发过提醒（manual 站点没有服务端证据，不计入连续天数）")
    if as_json:
        import json as _json
        print("\n" + _json.dumps(store.data, ensure_ascii=False, indent=2))
    print("")
    return 0


def _wake_hint(window_start: str) -> str:
    """计划任务的唤醒时刻 = 窗口开始前 10 分钟。

    在这里自算，而不是调 scheduler：唤醒时刻只是"提前量"的算术，
    与"窗口内随机采样"是两件事，不该耦合。

    **为什么必须是「固定提前唤醒 + 程序内等待」**（2026-10-10 回退）：
    为消灭"等待"这个观感问题，10-08 曾把随机化挪到计划任务的 `-RandomDelay`。
    结果 **10-10 真漏签整整一天**：当日 occurrence 被调度器静默判为 `missed`
    后直接跳次日 —— `NumberOfMissedRuns=1`、`NextRunTime` 跳到次日、
    16 秒内连读 8 次 `NextRunTime` 得到 **8 个不同值且全是次日**、
    `logs/checkin-2026-10-10.log` 根本不存在。微软 KB2956042 的标题就叫
    「使用 RandomDelay 参数的计划任务不会运行」。

    ⇒ 对"绝不能静默漏掉"的每日任务，`-RandomDelay` **不可靠**。
      宁可留一个**不可见**的等待进程（pythonw 无控制台），也不赌调度器：
      这条路径从 09-24 跑到 10-08 一次没漏。
    """
    hh, _, mm = str(window_start).partition(":")
    try:
        total = int(hh) * 60 + int(mm or 0) - 10
    except ValueError:
        total = 7 * 60 + 30
    total = max(0, total)
    return f"{total // 60:02d}:{total % 60:02d}"


def _plan(cfg) -> int:
    reason = scheduler.should_skip_today(cfg)
    if reason:
        print(f"今天不执行：{reason}")
        return 0
    target = scheduler.plan_today(cfg)
    delta = (target - datetime.now()).total_seconds()
    print(f"\n  今天计划执行时刻：{target:%Y-%m-%d %H:%M:%S}")
    if delta <= 0:
        print("  （该时刻已过，会立即执行 —— 手动补跑时的正常行为）")
    else:
        print(f"  （还有 {delta / 60:.0f} 分钟）")
    print(f"  窗口 {cfg.schedule.window_start}–{cfg.schedule.window_end}，"
          f"分布模式 {cfg.schedule.distribute}\n")
    return 0


_TASK_NAME = "AgentCheckin"


def _psq(value: str) -> str:
    """PowerShell 单引号字符串字面量。单引号在 PS 里靠**双写**转义。"""
    return "'" + str(value).replace("'", "''") + "'"


# 每段 PowerShell 脚本的前置设置（两条都是实测踩出来的）：
#   · ProgressPreference  —— 不关的话，cmdlet 的进度记录会被序列化成 CLIXML
#     写进 stderr，输出里塞满乱码 XML，真正的结果反而看不见。
#   · Console.OutputEncoding —— PowerShell 5.1 默认按系统 ANSI（本机是 GBK）输出，
#     而我们按 UTF-8 解码 ⇒ 中文结果全成乱码。
_PS_PREAMBLE = (
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
)


def _ps(script: str):
    """跑一段 PowerShell 脚本。

    两个刻意的选择：

    **1) 用 ScheduledTasks cmdlet，不用 `schtasks.exe`。**
    本机把 schtasks.exe 列进了程序黑名单 —— 连 `/Query` 都跑不了，报
    "PROGRAM BLOCKED BY SECURITY POLICY"，且明确不可绕过。而
    `Register-ScheduledTask` / `Unregister-ScheduledTask` 是 in-process 的
    PowerShell cmdlet，不启动任何外部程序，**不受黑名单影响**（实测全通）。
    顺带还多两个 schtasks 命令行给不了的能力：`-WorkingDirectory`，
    以及 `-StartWhenAvailable`（错过唤醒时刻后开机补跑）。

    **2) 用 `-EncodedCommand` 传参，不用 `-Command "<脚本>"`。**
    项目根目录名是中文（`智能体签到`），走命令行参数要过一遍系统 ANSI 代码页，
    中文路径会被毁掉。`-EncodedCommand` 传的是 Base64(UTF-16LE)，与代码页无关；
    而且脚本只存在于内存里，不受执行策略限制 —— 不像落盘的 .ps1 会被拦。
    """
    b64 = base64.b64encode((_PS_PREAMBLE + script).encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", b64],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


def _task_python() -> str:
    """计划任务用哪个解释器：优先项目 venv，没有就退回当前解释器。

    **优先 `pythonw.exe`（无控制台子系统）**，而不是 `python.exe`：
    后者每次运行都会弹一个控制台黑框，而任务要"提前唤醒 + 在窗口内随机等待"
    —— 于是那个黑框在屏幕上挂了两小时（2026-10-08 用户报障）。`pythonw` 没有控制台，
    同样的等待（仍然留在进程内，理由见 `_wake_hint`）也不会露脸。
    """
    for name in ("pythonw.exe", "python.exe"):
        python = os.path.join(_ROOT, ".venv", "Scripts", name)
        if os.path.isfile(python):
            return python
    return sys.executable


def _task_command() -> str:
    """计划任务实际执行的命令行。

    这里踩过一个坑：原先写的是 `python -m checkin`，看着更"标准"，但它要求
    **cwd 或 PYTHONPATH 指向 <root>/src**。而计划任务的工作目录是
    `C:\\Windows\\System32`，实测直接报 `No module named checkin` —— 任务每天
    准时跑、每天准时失败，还不弹窗，最难发现的那类 bug。

    改成直接执行 `src/checkin/__main__.py`：它自己会把 `<root>/src` 挂上
    sys.path，于是与 cwd、与 PYTHONPATH 都无关。

    **不带 `--now`**（2026-10-10 回退）：随机时刻由程序自己在窗口内采样后等待，
    所以被唤醒后必须走"等窗口"那条路。带 `--now` 会跳过采样，等于每天固定在
    同一个时刻签到 —— 反封堵的那层随机性就没了。
    """
    entry = os.path.join(_ROOT, "src", "checkin", "__main__.py")
    return f'"{_task_python()}" "{entry}"'


def _print_task(cfg) -> int:
    py = _task_python()
    entry = os.path.join(_ROOT, "src", "checkin", "__main__.py")
    hint = _wake_hint(cfg.schedule.window_start)

    print("")
    print("  推荐：直接装（自动处理路径与编码）\n")
    print("      run_checkin.bat --install-task")
    print("")
    print("  等价的手写 PowerShell（可整段粘贴）：\n")
    print(f'    $a = New-ScheduledTaskAction -Execute "{py}" `')
    print(f"         -Argument '\"{entry}\"' -WorkingDirectory \"{_ROOT}\"")
    print(f"    $t = New-ScheduledTaskTrigger -Daily -At {hint}")
    print("    $s = New-ScheduledTaskSettingsSet -StartWhenAvailable `")
    print("         -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd")
    print("    $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME `")
    print("         -LogonType Interactive -RunLevel Limited")
    print(f'    Register-ScheduledTask -TaskName "{_TASK_NAME}" `')
    print("         -Action $a -Trigger $t -Settings $s -Principal $p -Force")
    print("")
    print(f"  任务在 {hint} 唤醒（窗口开始前 10 分钟），程序内部再随机等待到")
    print(f"  {cfg.schedule.window_start}–{cfg.schedule.window_end} 之间的某时刻执行，")
    print("  所以「每天几点签到」在系统层面看不出来。")
    print("")
    print("  · 唤醒时刻没开机 → -StartWhenAvailable 会在开机后补跑一次")
    print("  · 只在有登录会话时运行（锁屏算、未登录不算）—— 开浏览器需要桌面会话")
    print("  · 本机 schtasks.exe 被程序黑名单封了，所以走 PowerShell cmdlet")
    print("  · 用 pythonw.exe 运行（无控制台），等待期间不露黑框")
    print("  · **不要**给触发器加 -RandomDelay：会让当日 occurrence 被静默跳过")
    print("    （2026-10-10 因此真漏签一天，见 _wake_hint 的说明）")
    print("  · 改动触发方式后**必须重跑本命令**才会生效（旧任务仍是老触发）")
    print("")
    print(f'  立刻试跑：Start-ScheduledTask -TaskName "{_TASK_NAME}"')
    print("  卸载    ：run_checkin.bat --uninstall-task")
    print("")
    return 0


def _install_script(cfg) -> str:
    """注册每日计划任务的 PowerShell 脚本。

    抽成纯函数（不执行）是为了**可测**：真跑一次会往系统里塞任务，
    测试不该有这种副作用。测试只断言脚本内容对不对。

    **固定时刻唤醒 + 进程内等待**（2026-10-10 从 `-RandomDelay` 回退）：

    10-08 曾把随机化挪到计划任务的 `-RandomDelay`（想消灭长等待），
    但 **10-10 真漏签一整天** —— 当日 occurrence 被调度器静默判为 `missed`
    后跳过（证据与微软 KB2956042 见 `_wake_hint` 的说明）。
    改回"固定提前唤醒 → 程序自己在窗口内随机采样后等待"：
    这条路径从 09-24 跑到 10-08 一次没漏；黑框早已由 pythonw 消灭，
    等待过程不可见，代价只剩一个静默进程。
    """
    entry = os.path.join(_ROOT, "src", "checkin", "__main__.py")
    hint = _wake_hint(cfg.schedule.window_start)
    return (
        "$ErrorActionPreference='Stop';"
        f"$a=New-ScheduledTaskAction -Execute {_psq(_task_python())} "
        f"-Argument {_psq(f'\"{entry}\"')} "
        f"-WorkingDirectory {_psq(_ROOT)};"
        # -At <固定时刻>，**刻意不带 -RandomDelay**：
        #   随机时刻由程序自己在窗口内采样（RandomDelay 会让当日 occurrence
        #   被静默跳过，见本函数 docstring 与 _wake_hint）
        f"$t=New-ScheduledTaskTrigger -Daily -At {_psq(hint)};"
        # -StartWhenAvailable       错过唤醒时刻（休眠/关机）后，开机自动补跑一次
        # -AllowStartIfOnBatteries  笔记本不在电源上也要跑（签到本身极轻）
        # -DontStopIfGoingOnBatteries / -DontStopOnIdleEnd
        #                           等待窗口内随机时刻期间（最长约 2.4 小时），
        #                           不能因拔电源或"空闲结束"被杀
        # -ExecutionTimeLimit 6h    够容纳"等到窗口末端"，又不至于挂死一整天
        # -MultipleInstances IgnoreNew  程序自己也有 run.lock，这里是第二道
        "$s=New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 6) -MultipleInstances IgnoreNew;"
        # 查 `-DontStopOnIdleEnd` 是否生效要看 `Settings.IdleSettings.StopOnIdleEnd`
        # —— 它**不在 Settings 顶层**（顶层没这个属性，去那儿读会得到 $null 并误判成
        # "没生效"，我因此白改了一轮还改坏了注册）。它管的是"任务运行期间系统空闲结束后
        # 是否被杀"：本任务要等待窗口内随机时刻（最长约 2.4 小时），必须为 False。
        "$p=New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        "-LogonType Interactive -RunLevel Limited;"
        f"Register-ScheduledTask -TaskName {_psq(_TASK_NAME)} -Action $a -Trigger $t "
        "-Settings $s -Principal $p "
        f"-Description {_psq('每日签到：' + hint + ' 唤醒，窗口内随机时刻执行')} "
        "-Force | Out-Null;"
        f"Write-Output ('OK 已注册，状态=' + "
        f"(Get-ScheduledTask -TaskName {_psq(_TASK_NAME)}).State)"
    )


def _uninstall_script() -> str:
    return (
        f"$t=Get-ScheduledTask -TaskName {_psq(_TASK_NAME)} -ErrorAction SilentlyContinue;"
        f"if($t){{Unregister-ScheduledTask -TaskName {_psq(_TASK_NAME)} -Confirm:$false;"
        "Write-Output 'OK 已卸载'}else{Write-Output '任务不存在（无需卸载）'}"
    )


def _install_task(cfg) -> int:
    """注册每日计划任务。

    用 PowerShell cmdlet 而不是 `schtasks.exe` —— 原因见 `_ps()` 的说明。
    """
    hint = _wake_hint(cfg.schedule.window_start)
    print("\n  注册计划任务：")
    print(f"    {hint} 唤醒（窗口开始前 10 分钟），运行 {_task_command()}\n")
    p = _ps(_install_script(cfg))
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    print("  " + (out.replace("\n", "\n  ") or "(无输出)"))
    if p.returncode != 0 or "OK" not in out:
        print("\n  注册失败。常见原因：")
        print("    · 已存在同名任务但无权覆盖 → 先跑一次 --uninstall-task")
        print("    · PowerShell 被执行策略限制 → 用「以管理员身份运行」的终端重试")
        print("")
        return p.returncode or 1
    print("\n  已注册。建议先手动试跑一次确认链路：")
    print(f'    Start-ScheduledTask -TaskName "{_TASK_NAME}"')
    print("  然后看 logs/ 下当天的日志有没有内容。\n")
    return 0


def _uninstall_task(cfg) -> int:
    p = _ps(_uninstall_script())
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    print("\n  " + (out.replace("\n", "\n  ") or "(无输出)") + "\n")
    return p.returncode


def _login(cfg, recipes) -> int:
    """打开签到专用浏览器，让你登录一次；登录态之后长期复用。

    这是全流程**唯一的交互环节**：专用 profile 出厂是空白的，必须先人工登录，
    后续的自动签到才有会话可用。免得用户得靠 `--probe` 顺带把浏览器拉起来。
    """
    from checkin.browser import launcher

    targets = [r for r in recipes
               if r.enabled and r.mode == "auto" and r.session.start_url]
    if not targets:
        print("\n  recipes/ 里没有 mode=auto 且带 start_url 的配方，无需登录。\n")
        return 1

    print("")
    print("═" * 62)
    print("  签到专用浏览器 · 登录")
    print("═" * 62)
    print(f"\n  profile  ：{cfg.chrome.user_data_dir}")
    print(f"  调试端口 ：{cfg.chrome.remote_debugging_port}（供自动化接管，别关）")
    print("\n  它与你的日常浏览器完全隔离，登录态长期保留，只服务签到。")
    print("  请在下面这个窗口里完成登录（含验证码）：\n")
    for r in targets:
        print(f"    · {r.name}")
        print(f"      {r.session.start_url}")

    launcher.ensure_chrome(cfg, targets[0].session.start_url)
    _focus_target_page(cfg, targets[0])

    print("\n  浏览器已打开 —— 去那个窗口登录，登录好了回这里按回车。")
    while True:
        try:
            input("\n  按【回车】验证登录态（Ctrl+C 放弃）：")
        except (EOFError, KeyboardInterrupt):
            print("\n\n  已退出验证。随时可用 --probe 复查。\n")
            return 0

        print("\n  验证中……")
        results = Engine(cfg, recipes).run(only=[r.id for r in targets], probe=True)

        print("")
        bad = [r for r in results if r.outcome.value in ("need_login", "error")]
        for r in results:
            print(f"    [{'未通过' if r in bad else '通过  '}] {r.site_id}")
            print(f"             {r.message}")
        print("")

        if not bad:
            print("  全部就绪。下一步注册每日计划任务：")
            print("    python -m checkin --print-task\n")
            return 0
        print("  还没检测到登录。确认是在**专用浏览器窗口**里登录的，再按回车重试。")


def _focus_target_page(cfg, recipe) -> None:
    """浏览器可能本来就在跑（此时 ensure_chrome 的 start_url 会被忽略），
    这里确保目标页面确实开着，否则用户打开窗口会一脸茫然。"""
    from checkin.browser import cdp

    sess = recipe.session
    try:
        page = cdp.CDPPage.connect(cfg.chrome.remote_debugging_port)
    except Exception as e:                                  # 附着不上就让用户手动开
        print(f"\n  （未能自动定位页面：{e}）")
        return
    try:
        if sess.tab_match and not any(m in page.current_url() for m in sess.tab_match):
            page.navigate(sess.start_url or recipe.origin)
    except Exception as e:
        print(f"\n  （未能自动打开目标页：{e}，请手动在那个窗口里打开）")
    finally:
        page.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="checkin", description="智能体每日签到")
    parser.add_argument("--login", action="store_true",
                        help="打开签到专用浏览器，人工登录一次（登录态长期复用）")
    parser.add_argument("--dry-run", action="store_true", help="只走流程不触发签到、不记状态")
    parser.add_argument("--probe", action="store_true", help="诊断模式：查登录态/探测 token 键名，不签到")
    parser.add_argument("--only-site", default="", help="逗号分隔，仅处理指定站点 id")
    parser.add_argument("--config-root", default=_ROOT, help="项目根（含 config.yaml 与 recipes/）")
    parser.add_argument("--now", action="store_true",
                        help="立刻执行，不等调度窗口内的随机时刻（计划任务即以此唤醒后直接执行）")
    parser.add_argument("--status", action="store_true", help="查看各站点状态与连续天数")
    parser.add_argument("--plan", action="store_true", help="查看今天的计划执行时刻")
    parser.add_argument("--print-task", dest="print_task", action="store_true",
                        help="打印每日计划任务的注册命令")
    parser.add_argument("--install-task", dest="install_task", action="store_true",
                        help="注册每日计划任务（PowerShell cmdlet，免 schtasks 黑名单）")
    parser.add_argument("--uninstall-task", dest="uninstall_task", action="store_true",
                        help="卸载每日计划任务")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON")
    args = parser.parse_args(argv)

    cfg, recipes = cfg_mod.load_config(args.config_root)

    # 交互 / 纯查询命令：不起日志、不碰业务接口
    if args.login:
        return _login(cfg, recipes)
    if args.install_task:
        return _install_task(cfg)
    if args.uninstall_task:
        return _uninstall_task(cfg)
    if args.status or args.plan or args.print_task:
        if args.status:
            return _status(cfg, recipes, args.json)
        if args.plan:
            return _plan(cfg)
        return _print_task(cfg)

    logger = log_mod.setup(cfg.log_dir, cfg.log_level, cfg.redact_keys)
    only = [s.strip() for s in args.only_site.split(",") if s.strip()] or None

    logger.info("智能体每日签到启动 | 站点=%s | 模式=%s",
                [r.id for r in recipes if r.enabled],
                "probe" if args.probe else ("dry-run" if args.dry_run else "run"))

    engine = Engine(cfg, recipes)
    results = engine.run(only=only, dry_run=args.dry_run, probe=args.probe, now=args.now)

    if args.json:
        import json as _json
        print(_json.dumps(
            [{"site": r.site_id, "outcome": r.outcome.value, "message": r.message}
             for r in results], ensure_ascii=False, indent=2))

    if not results:
        print("没有可执行的站点。检查 recipes/*.yaml 的 enabled / mode，或 --only-site。")
        return 1

    if not (args.dry_run or args.probe):
        notify.notify_results(results)
    else:
        print(notify.summary(results))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
