"""Administrator registration. CorvusSKK-style: COM + ProfileMgr + InstallLayoutOrTip.

Do not hand-write CTF\\TIP language profiles. That plus the old
ITfInputProcessorProfiles::Register pair made Win11 attach the wrong engine.
"""

from __future__ import annotations

import ctypes
import sys
import winreg
from pathlib import Path

CLSID = "{A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}"
PROFILE = "{A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}"
DLL = Path(__file__).resolve().parents[2] / "dist" / "NicolaIME3.dll"
# CorvusSKK: 0x0411:{clsid}{profile}
LAYOUT = f"0x0411:{CLSID}{PROFILE}"


def elevated() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def set_sz(root, path, name, value):
    key = winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY)
    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    winreg.CloseKey(key)


def main() -> None:
    if not elevated():
        print("管理者の PowerShell で実行してください:")
        print(rf"  python {Path(__file__).resolve()}")
        sys.exit(2)
    if not DLL.exists():
        raise SystemExit(f"missing {DLL}")
    dll = str(DLL)

    clsid = rf"Software\Classes\CLSID\{CLSID}"
    set_sz(winreg.HKEY_LOCAL_MACHINE, clsid, None, "NicolaIME")
    set_sz(winreg.HKEY_LOCAL_MACHINE, clsid + r"\InprocServer32", None, dll)
    set_sz(winreg.HKEY_LOCAL_MACHINE, clsid + r"\InprocServer32", "ThreadingModel", "Apartment")

    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitialize(None)
    d = ctypes.WinDLL(dll)
    hr = d.DllRegisterServer() & 0xFFFFFFFF
    print(f"DllRegisterServer 0x{hr:08X}  dll={dll}")

    inp = ctypes.WinDLL("input.dll")
    fn = inp.InstallLayoutOrTip
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    print(f"InstallLayoutOrTip({LAYOUT}) -> {fn(LAYOUT, 0)}")
    print("Settings -> Japanese -> keyboard -> NicolaIME")
    print("If the tray shows NicolaIME but letters look like Pinyin/Latin,")
    print("the engine is not ready or the wrong TIP is active. Check %TEMP%\\oyayubi_tip.log")


if __name__ == "__main__":
    main()
