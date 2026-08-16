"""Administrator: remove NicolaIME from HKLM and HKCU."""

from __future__ import annotations

import ctypes
import sys
import winreg
from pathlib import Path

CLSID = "{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}"
PROFILE = "{A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}"
DLL = Path(__file__).resolve().parents[2] / "dist" / "NicolaIME3.dll"
LAYOUTS = (
    f"0x0411:{CLSID}{PROFILE}",
    f"0411:{CLSID}{PROFILE}",
)


def elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def del_tree(root, path: str) -> None:
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY)
    except OSError as e:
        print("skip", path, e)
        return
    while True:
        try:
            sub = winreg.EnumKey(key, 0)
        except OSError:
            break
        del_tree(root, path + "\\" + sub)
    winreg.CloseKey(key)
    try:
        winreg.DeleteKeyEx(root, path, winreg.KEY_WOW64_64KEY)
        print("deleted", path)
    except OSError as e:
        print("delete failed", path, e)


def main() -> None:
    if not elevated():
        print("Run in Administrator PowerShell:")
        print(rf"  python {Path(__file__).resolve()}")
        sys.exit(2)

    fn = ctypes.WinDLL("input.dll").InstallLayoutOrTip
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    for layout in LAYOUTS:
        print("uninstall", layout, fn(layout, 1))

    if DLL.exists():
        ole32 = ctypes.OleDLL("ole32")
        ole32.CoInitialize(None)
        try:
            d = ctypes.WinDLL(str(DLL))
            print(f"DllUnregisterServer 0x{d.DllUnregisterServer() & 0xFFFFFFFF:08X}")
        except OSError as e:
            print("DllUnregisterServer skip", e)

    for root, label in (
        (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
        (winreg.HKEY_CURRENT_USER, "HKCU"),
    ):
        for path in (
            rf"SOFTWARE\Microsoft\CTF\TIP\{CLSID}",
            rf"Software\Microsoft\CTF\TIP\{CLSID}",
            rf"Software\Classes\CLSID\{CLSID}",
        ):
            del_tree(root, path)

    print("done. Sign out if NicolaIME is still in the tray.")


if __name__ == "__main__":
    main()
