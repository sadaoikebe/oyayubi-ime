"""Win32 test host: same down/up/QPC/Timer model as a TSF TIP.

Not an IME. Type in this window to exercise SM + lattice decode.
Notepad needs the compiled TIP (src/tip).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oyayubi.ime.session import ImeSession

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

# Python 3.13 wintypes に HCURSOR 等が無い
HANDLE = w.HANDLE
HCURSOR = HANDLE
HICON = getattr(w, "HICON", HANDLE)
HBRUSH = getattr(w, "HBRUSH", HANDLE)
HINSTANCE = getattr(w, "HINSTANCE", HANDLE)
HDC = getattr(w, "HDC", HANDLE)
# 64-bit: WPARAM/LPARAM はポインタ幅。wintypes の 32-bit だと OverflowError になる
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, WPARAM, LPARAM)

user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.InvalidateRect.argtypes = [w.HWND, ctypes.c_void_p, w.BOOL]

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_TIMER = 0x0113
WM_ERASEBKGND = 0x0014
WM_CHAR = 0x0102
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
WS_OVERLAPPEDWINDOW = 0x00CF0000
CW_USEDEFAULT = 0x80000000
IDC_ARROW = 32512
WHITE_BRUSH = 0
DT_WORDBREAK = 0x0010
DT_NOPREFIX = 0x0800
TIMER_ID = 1

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", w.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", w.LPCWSTR),
        ("lpszClassName", w.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", HDC),
        ("fErase", w.BOOL),
        ("rcPaint", w.RECT),
        ("fRestore", w.BOOL),
        ("fIncUpdate", w.BOOL),
        ("rgbReserved", w.BYTE * 32),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_freq = w.LARGE_INTEGER()
kernel32.QueryPerformanceFrequency(ctypes.byref(_freq))
_qpc_freq = max(int(_freq.value), 1)

session = ImeSession()
_last_view = None


def now_ms() -> int:
    c = w.LARGE_INTEGER()
    kernel32.QueryPerformanceCounter(ctypes.byref(c))
    return int(c.value * 1000 // _qpc_freq)


def view_key() -> tuple:
    return (
        session.committed,
        session.composition,
        session.converted,
        session.fsm.state,
        len(session.tokens),
        tuple(session.tokens),
        tuple(session.candidates),
        session.cand_index,
    )


def redraw_if_changed(hwnd) -> None:
    global _last_view
    now = view_key()
    if now == _last_view:
        return
    _last_view = now
    user32.InvalidateRect(hwnd, None, False)


def paint(hwnd) -> None:
    ps = PAINTSTRUCT()
    hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
    rc = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    brush = gdi32.GetStockObject(WHITE_BRUSH)
    user32.FillRect(hdc, ctypes.byref(rc), brush)
    committed = session.committed + ("" if not hasattr(session, "committed") else "")
    body = session.committed
    comp = session.composition
    status = "変換済" if session.converted else "入力中"
    text = (
        "oyayubi-ime 試験窓  （Notepad 用ではない。TSF TIP は src/tip）\n"
        "Space+文字 = 親指シフト（左右不明）。Space 単独 = 変換。Enter = 確定。Esc = 取消。\n"
        f"[{status}] tokens={len(session.tokens)} fsm={session.fsm.state}\n\n"
        f"{body}"
        f"[{comp}]" if comp else f"{body}"
    )
    if not comp:
        text = (
            "oyayubi-ime 試験窓  （Notepad 用ではない。TSF TIP は src/tip）\n"
            "Space+文字 = 親指シフト（左右不明）。Space 単独 = 変換。Enter = 確定。Esc = 取消。\n"
            f"[{status}] tokens={len(session.tokens)} fsm={session.fsm.state}\n\n"
            f"{body}|"
        )
    else:
        cands = ""
        if session.converted and session.candidates:
            bits = []
            for i, c in enumerate(session.candidates):
                mark = ">" if i == session.cand_index else " "
                bits.append(f"{mark}{i + 1}.{c}")
            cands = "\n" + "  ".join(bits)
        text = (
            "oyayubi-ime 試験窓  （Notepad 用ではない。TSF TIP は src/tip）\n"
            "Space+文字 = 親指シフト（左右不明）。Space 単独 = 変換。Enter = 確定。Esc = 取消。\n"
            f"[{status}] tokens={len(session.tokens)} fsm={session.fsm.state}\n\n"
            f"{body}〖{comp}〗"
            f"{cands}"
        )
    user32.DrawTextW(hdc, text, -1, ctypes.byref(rc), DT_WORDBREAK | DT_NOPREFIX)
    user32.EndPaint(hwnd, ctypes.byref(ps))


def handle_key(hwnd, down: bool, vk: int) -> None:
    extra = session.on_key(down, vk, now_ms())
    if extra:
        session.committed += extra
    redraw_if_changed(hwnd)


@WNDPROC
def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    if msg == WM_PAINT:
        paint(hwnd)
        return 0
    if msg == WM_ERASEBKGND:
        return 1
    if msg == WM_TIMER:
        if session.fsm.timer_deadline is not None:
            extra = session.on_timeout(now_ms())
            if extra:
                session.committed += extra
            redraw_if_changed(hwnd)
        return 0
    if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
        handle_key(hwnd, True, int(wparam) & 0xFF)
        return 0
    if msg in (WM_KEYUP, WM_SYSKEYUP):
        handle_key(hwnd, False, int(wparam) & 0xFF)
        return 0
    if msg == WM_CHAR:
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> None:
    print("loading dictionary (first run is slow)...")
    session.load()
    print("ready")
    inst = kernel32.GetModuleHandleW(None)
    cls_name = "OyayubiImeHost"
    wc = WNDCLASSW()
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = wndproc
    wc.hInstance = inst
    user32.LoadCursorW.restype = HCURSOR
    wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
    wc.hbrBackground = gdi32.GetStockObject(WHITE_BRUSH)
    wc.lpszClassName = cls_name
    atom = user32.RegisterClassW(ctypes.byref(wc))
    if not atom:
        raise OSError(f"RegisterClassW failed ({ctypes.GetLastError()})")
    origin = ctypes.c_int(-2147483648)  # CW_USEDEFAULT
    hwnd = user32.CreateWindowExW(
        0,
        cls_name,
        "oyayubi-ime 試験窓",
        WS_OVERLAPPEDWINDOW,
        origin,
        origin,
        900,
        500,
        None,
        None,
        inst,
        None,
    )
    user32.ShowWindow(hwnd, 1)
    user32.SetTimer(hwnd, TIMER_ID, 16, None)
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
