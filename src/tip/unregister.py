"""Per-user unregister. Prefer unregister_admin.py if the IME was installed as admin."""

from __future__ import annotations

import ctypes
import winreg
from pathlib import Path

CLSID = "{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}"
PROFILE = "{A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}"
DLL = Path(__file__).resolve().parents[2] / "dist" / "NicolaIME3.dll"


def del_tree(root, path: str) -> None:
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    while True:
        try:
            sub = winreg.EnumKey(key, 0)
        except OSError:
            break
        del_tree(root, path + "\\" + sub)
    winreg.CloseKey(key)
    try:
        winreg.DeleteKey(root, path)
        print("deleted HKCU", path)
    except OSError:
        pass


def main() -> None:
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitialize(None)
    if DLL.exists():
        d = ctypes.WinDLL(str(DLL))
        print(f"DllUnregisterServer 0x{d.DllUnregisterServer() & 0xFFFFFFFF:08X}")

    fn = ctypes.WinDLL("input.dll").InstallLayoutOrTip
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    for layout in (f"0x0411:{CLSID}{PROFILE}", f"0411:{CLSID}{PROFILE}"):
        print("uninstall", layout, fn(layout, 1))

    for path in (
        rf"Software\Microsoft\CTF\TIP\{CLSID}",
        rf"Software\Classes\CLSID\{CLSID}",
    ):
        del_tree(winreg.HKEY_CURRENT_USER, path)
    print("done")


if __name__ == "__main__":
    main()
