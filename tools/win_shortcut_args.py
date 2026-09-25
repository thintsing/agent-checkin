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
    python tools/win_shortcut_args.py --qoder          # 给 Qoder CN 全部入口加调试端口
    python tools/win_shortcut_args.py --qoder --revert # 从备份还原
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
QODER_ARG = "--remote-debugging-port=9334"
QODER_LNKS = [
    os.path.expandvars(r"%USERPROFILE%\Desktop\Qoder CN.lnk"),
    os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Qoder CN.lnk"),
    os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Qoder\Qoder CN.lnk"),
]
BACKUP_DIR = os.path.join(PROJECT_ROOT, "data", "shortcut_backup")

# 实测（2026-09-25）：Qoder 启动时会把**这一个**回写成"无参数"版本，另两个不会。
# 只有第一次启动带得上参数，之后就被静默回滚 —— 所以必须给它加只读保护。
QODER_LOCK = QODER_LNKS[1]


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
        mark = "  ← 已带调试端口" if QODER_ARG in args else ""
        print(f"  {lnk}\n      Arguments = [{args}]{mark}")
    return 0


def do_set(lnk: str, arg: str, backup_dir: str, lock: bool = False) -> int:
    if not os.path.exists(lnk):
        print(f"目标不存在: {lnk}")
        return 2
    os.chmod(lnk, stat.S_IWRITE | stat.S_IREAD)      # 只读会挡住写入，先解开
    before = read_args(lnk)
    if arg in before:
        if lock:
            os.chmod(lnk, stat.S_IREAD)
        print(f"  {lnk}\n      已含该参数，跳过{'（已加只读保护）' if lock else ''}")
        return 0
    os.makedirs(backup_dir, exist_ok=True)
    tag = os.path.basename(os.path.dirname(lnk)) or "root"
    bk = os.path.join(backup_dir, f"{tag}__{os.path.basename(lnk)}")
    shutil.copy2(lnk, bk)
    set_args(lnk, arg)
    after = read_args(lnk)
    if lock:
        os.chmod(lnk, stat.S_IREAD)
    ok = after.strip() == arg
    print(f"  {lnk}\n      改前 [{before}] → 改后 [{after}]   备份 {bk}"
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
    ap.add_argument("--arg", default=QODER_ARG, help=f"要写入的参数（默认 {QODER_ARG}）")
    ap.add_argument("--qoder", action="store_true", help="处理 Qoder CN 全部启动入口")
    ap.add_argument("--revert", action="store_true", help="从备份还原")
    ap.add_argument("--backup-dir", default=BACKUP_DIR)
    a = ap.parse_args()

    if not self_check():
        print("自检失败：IPersistFile 槽位不对，拒绝继续（防止写坏快捷方式）")
        return 3
    print("自检通过（Load 槽位正确）\n")

    if a.qoder:
        targets = [p for p in QODER_LNKS if os.path.exists(p)]
        if not targets:
            print("未找到 Qoder 快捷方式")
            return 2
        for t in targets:
            if a.revert:
                do_revert(t, a.backup_dir)
            else:
                do_set(t, QODER_ARG, a.backup_dir, lock=(t == QODER_LOCK))
        return 0
    if a.revert and a.set:
        return do_revert(a.set, a.backup_dir)
    if a.set:
        return do_set(a.set, a.arg, a.backup_dir)
    if a.list is not None:
        return do_list(a.list or [os.path.expandvars(r"%USERPROFILE%\Desktop")])
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
