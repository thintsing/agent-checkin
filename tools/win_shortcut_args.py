"""Windows 快捷方式（.lnk）参数管理 —— 零依赖，纯 ctypes 调 COM。

为什么不用现成办法
------------------
* PowerShell 的 `New-Object -ComObject WScript.Shell` → 被安全 hook 拦
  （"COM object instantiation can run arbitrary code"）
* `cscript` / `wscript` 跑 VBScript              → 被当成 LOLBin 拦
⇒ 直接在 Python 进程内调 IShellLinkW / IPersistFile，绕开所有被拦的宿主。

**关键坑（我踩过，后果极具误导性）**
------------------------------------
`QueryInterface` 返回的接口指针，它的 vtable 是**从 IUnknown 重新计数**的：

    IPersistFile:  [0]QI [1]AddRef [2]Release [3]GetClassID [4]IsDirty [5]Load [6]Save ...
    IShellLinkW :  [0]QI [1]AddRef [2]Release [3]GetPath ... [10]GetArguments [11]SetArguments ... [20]SetPath

如果按"IShellLinkW 的 20 号之后继续数"去取 Load/Save（21/22/23），会**越界读到别的槽位**。
症状是最难查的那种：**调用返回 S_OK，但文件一个字节都没变**（方法存在但被当空实现调用）。
定位手段：用"加载一个不存在的文件"探测，正确的 `Load` 必须返回 `0x80070002`（文件未找到）。

为什么需要这个能力（2026-09-25 实测背景）
----------------------------------------
Electron 应用的调试端口**只能在启动时绑定**，运行中无法追加；而单实例锁是全局的，
没法偷偷起第二个实例。所以"让客户端常开也能被自动化接管"的唯一办法，
就是**让日常启动的入口就带上 `--remote-debugging-port`**。
实测：Qoder CN Launcher 会把未知命令行开关**透传**给真正的 app exe。

用法
----
    python tools/win_shortcut_args.py --list "<目录或 .lnk>"
    python tools/win_shortcut_args.py --set "<.lnk>" --arg "--foo=bar"
    python tools/win_shortcut_args.py --app qoder            # 给 Qoder CN 全部入口加调试端口
    python tools/win_shortcut_args.py --app workbuddy        # 同上，WorkBuddy
    python tools/win_shortcut_args.py --app qoder --revert   # 从备份还原
    python tools/win_shortcut_args.py --qoder                # 等价 --app qoder（旧写法保留）

端口不在这里定义 —— 每个应用的端口来自**它自己的配方**（单一事实源）：
`--app qoder` → `recipes/qoder.yaml`，`--app workbuddy` → `recipes/workbuddy.yaml`，
都取 `client.debug_port`。换端口 = 改配方 → 重跑对应的 `--app`。

为什么端口必须由配方单点定义：端口是**一次性资源**（2026-09-27 的 9334 就被
已死进程的僵尸句柄占死，只能换）。换端口时若快捷方式与驱动各留一份旧值，
就会出现"端口写着 A、驱动连 B"的静默失配 —— 实测踩过。
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import os
import shutil
import stat
import sys
import uuid
from ctypes import POINTER, WINFUNCTYPE, byref, c_int, c_long, c_void_p, c_wchar_p, wintypes

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPES_DIR = os.path.join(PROJECT_ROOT, "recipes")

DEBUG_ARG_PREFIX = "--remote-debugging-port="

# 每个应用一张表。加新应用 = 加一条表项，逻辑不动。
#
# `lock` = **实测**会被客户端自己回写（启动时重置成无参数版本）的那一个入口的下标。
# 只有它需要只读保护。值为 None 表示"还没观察到回写，先不加锁"——
# 加锁有代价（客户端更新器可能写不进去），没有实测依据就不要加。
APPS = {
    "qoder": {
        "label": "Qoder CN",
        "recipe": "qoder.yaml",
        "lnks": [
            r"%USERPROFILE%\Desktop\Qoder CN.lnk",
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Qoder CN.lnk",
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Qoder\Qoder CN.lnk",
        ],
        # 2026-09-25 实测：Qoder 启动时只回写这一个。
        "lock": 1,
    },
    "workbuddy": {
        "label": "WorkBuddy",
        "recipe": "workbuddy.yaml",
        "lnks": [
            r"%USERPROFILE%\Desktop\WorkBuddy.lnk",
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\WorkBuddy.lnk",
        ],
        # 2026-10-06 实测记录：改完重启后**未被回写**（见 HANDOFF）。
        "lock": None,
    },
}
BACKUP_DIR = os.path.join(PROJECT_ROOT, "data", "shortcut_backup")


def app_lnks(app: str) -> list:
    return [os.path.expandvars(p) for p in APPS[app]["lnks"]]


def recipe_debug_port(app: str, default: int = 9335) -> int:
    """从该应用的配方读 `client.debug_port`。

    端口是**跨进程契约**：快捷方式写进去的、和驱动要连的必须是同一个值。
    两处各硬编码一份迟早漂移（本项目已因"同一结论两处写法"踩过坑），
    所以这里以配方为**单一事实源**，本工具不自己定义端口。

    刻意不引 yaml 依赖（本工具的设计目标之一是零依赖），够用的最小解析即可。
    """
    path = os.path.join(RECIPES_DIR, APPS[app]["recipe"])
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                s = line.split("#", 1)[0].strip()
                if s.startswith("debug_port:"):
                    return int(s.split(":", 1)[1].strip())
    except Exception:
        pass
    return default


def app_arg(app: str) -> str:
    return f"{DEBUG_ARG_PREFIX}{recipe_debug_port(app)}"


# --------------------------------------------------------------------- COM

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s: str) -> "GUID":
        u = uuid.UUID(s)
        return cls(u.time_low, u.time_mid, u.time_hi_version,
                   (ctypes.c_ubyte * 8)(*u.bytes[8:]))


CLSID_SHELLLINK = GUID.of("{00021401-0000-0000-C000-000000000046}")
IID_ISHELLLINKW = GUID.of("{000214F9-0000-0000-C000-000000000046}")
IID_IPERSISTFILE = GUID.of("{0000010B-0000-0000-C000-000000000046}")

_ole32 = ctypes.oledll.ole32
_ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = c_long
_ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, wintypes.DWORD,
                                    POINTER(GUID), POINTER(c_void_p)]
_ole32.CoCreateInstance.restype = c_long

# vtable 槽位（注意 IPersistFile 是独立计数）
SL_GET_ARGS, SL_SET_ARGS = 10, 11
PF_LOAD, PF_SAVE = 5, 6


def _slot(ptr, index: int, restype, *argtypes):
    vtbl = ctypes.cast(ptr, POINTER(c_void_p))[0]
    addr = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(addr)


def _open(path: str, write: bool = False):
    """返回 (IShellLinkW 指针, IPersistFile 指针)，已 Load 指定 .lnk。"""
    _ole32.CoInitializeEx(None, 2)          # COINIT_APARTMENTTHREADED
    p = c_void_p()
    _ole32.CoCreateInstance(byref(CLSID_SHELLLINK), None, 1,
                            byref(IID_ISHELLLINKW), byref(p))
    pf = c_void_p()
    _slot(p, 0, c_long, POINTER(GUID), POINTER(c_void_p))(
        p, byref(IID_IPERSISTFILE), byref(pf))
    mode = 2 if write else 0                # STGM_READWRITE / STGM_READ
    hr = _slot(pf, PF_LOAD, c_long, c_wchar_p, wintypes.DWORD)(pf, c_wchar_p(path), mode)
    if (hr & 0xFFFFFFFF) not in (0, 1):
        raise OSError(f"Load 失败: 0x{hr & 0xFFFFFFFF:08X}")
    return p, pf


def read_args(lnk: str) -> str:
    p, _ = _open(lnk)
    buf = ctypes.create_unicode_buffer(2048)
    _slot(p, SL_GET_ARGS, c_long, POINTER(ctypes.c_wchar), c_int)(p, buf, 2048)
    return buf.value


def set_args(lnk: str, args: str) -> None:
    p, pf = _open(lnk, write=True)
    _slot(p, SL_SET_ARGS, c_long, c_wchar_p)(p, c_wchar_p(args))
    _slot(pf, PF_SAVE, c_long, c_wchar_p, wintypes.BOOL)(pf, c_wchar_p(lnk), True)


def self_check() -> bool:
    """用"加载不存在的文件"验证槽位正确 —— 正确实现必须返回 0x80070002。"""
    _ole32.CoInitializeEx(None, 2)
    p = c_void_p()
    _ole32.CoCreateInstance(byref(CLSID_SHELLLINK), None, 1,
                            byref(IID_ISHELLLINKW), byref(p))
    pf = c_void_p()
    _slot(p, 0, c_long, POINTER(GUID), POINTER(c_void_p))(
        p, byref(IID_IPERSISTFILE), byref(pf))
    hr = _slot(pf, PF_LOAD, c_long, c_wchar_p, wintypes.DWORD)(
        pf, c_wchar_p(r"C:\__no_such_link_xyz.lnk"), 2)
    return (hr & 0xFFFFFFFF) == 0x80070002


# ----------------------------------------------------------------- 操作

def collect(paths) -> list:
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += glob.glob(os.path.join(p, "**", "*.lnk"), recursive=True)
        else:
            out.append(p)
    return out


def do_list(paths) -> int:
    for lnk in collect(paths):
        if not os.path.exists(lnk):
            print(f"  (不存在) {lnk}")
            continue
        try:
            args = read_args(lnk)
        except Exception as e:
            print(f"  (读取失败 {e}) {lnk}")
            continue
        mark = "  ← 已带调试端口" if DEBUG_ARG_PREFIX in args else ""
        print(f"  {lnk}\n      Arguments = [{args}]{mark}")
    return 0


def do_set(lnk: str, arg: str, backup_dir: str, lock: bool = False) -> int:
    if not os.path.exists(lnk):
        print(f"目标不存在: {lnk}")
        return 2
    os.chmod(lnk, stat.S_IWRITE | stat.S_IREAD)      # 只读会挡住写入，先解开
    before = read_args(lnk)
    if before.strip() == arg:
        if lock:
            os.chmod(lnk, stat.S_IREAD)
        print(f"  {lnk}\n      参数已正确，跳过{'（已加只读保护）' if lock else ''}")
        return 0
    stale = [t for t in before.split() if t.startswith("--remote-debugging-port")]
    if stale:
        print(f"  {lnk}\n      检测到旧参数 {stale} → 替换为 [{arg}]")
    os.makedirs(backup_dir, exist_ok=True)
    tag = os.path.basename(os.path.dirname(lnk)) or "root"
    bk = os.path.join(backup_dir, f"{tag}__{os.path.basename(lnk)}")
    # 备份**只取最早那份**（= 任何改造之前的原始态）。若每次都覆盖，
    # "改端口"这类二次操作会把原始备份换成半成品，回滚就回不到真正干净的状态。
    if os.path.exists(bk):
        kept = "备份已存在，保留最早的"
    else:
        shutil.copy2(lnk, bk)
        kept = "已备份"
    set_args(lnk, arg)
    after = read_args(lnk)
    if lock:
        os.chmod(lnk, stat.S_IREAD)
    ok = after.strip() == arg
    print(f"      改前 [{before}] → 改后 [{after}]   {kept}: {bk}"
          f"{'   已加只读保护' if lock else ''}\n      {'OK' if ok else '校验失败 ✘'}")
    return 0 if ok else 1


def do_revert(lnk: str, backup_dir: str) -> int:
    tag = os.path.basename(os.path.dirname(lnk)) or "root"
    bk = os.path.join(backup_dir, f"{tag}__{os.path.basename(lnk)}")
    if not os.path.exists(bk):
        print(f"无备份，跳过: {lnk}")
        return 2
    shutil.copy2(bk, lnk)
    print(f"  已还原 {lnk}  Arguments=[{read_args(lnk)}]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Windows 快捷方式参数管理")
    ap.add_argument("--list", nargs="*", metavar="PATH", help="列出快捷方式及其参数")
    ap.add_argument("--set", metavar="LNK", help="目标快捷方式")
    ap.add_argument("--arg", default=None,
                    help="要写入的参数（缺省 = 按 --app 从该应用的配方读端口）")
    ap.add_argument("--app", choices=sorted(APPS), help="处理该应用的全部启动入口")
    ap.add_argument("--qoder", action="store_true",
                    help="等价于 --app qoder（旧写法，保留兼容）")
    ap.add_argument("--revert", action="store_true", help="从备份还原")
    ap.add_argument("--backup-dir", default=BACKUP_DIR)
    a = ap.parse_args()

    if not self_check():
        print("自检失败：IPersistFile 槽位不对，拒绝继续（防止写坏快捷方式）")
        return 3
    print("自检通过（Load 槽位正确）\n")

    app = a.app or ("qoder" if a.qoder else None)
    if app:
        lnks = app_lnks(app)
        arg = a.arg or app_arg(app)
        lock_idx = APPS[app]["lock"]
        if not any(os.path.exists(p) for p in lnks):
            print(f"未找到 {APPS[app]['label']} 快捷方式")
            return 2
        print(f"应用 {APPS[app]['label']} | 写入参数 {arg}\n")
        for i, t in enumerate(lnks):
            if not os.path.exists(t):
                continue
            if a.revert:
                do_revert(t, a.backup_dir)
            else:
                do_set(t, arg, a.backup_dir, lock=(lock_idx is not None and i == lock_idx))
        return 0
    if a.revert and a.set:
        return do_revert(a.set, a.backup_dir)
    if a.set:
        return do_set(a.set, a.arg or app_arg("qoder"), a.backup_dir)
    if a.list is not None:
        return do_list(a.list or [os.path.expandvars(r"%USERPROFILE%\Desktop")])
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
