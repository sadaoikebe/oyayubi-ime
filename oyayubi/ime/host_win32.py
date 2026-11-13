"""Win32 test host: same down/up/QPC/Timer model as a TSF TIP.

Not an IME. Type in this window to exercise SM + lattice decode.
Notepad needs the compiled TIP (src/tip).
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as w
from pathlib import Path

from oyayubi.ime.session import ImeSession
from oyayubi.ime.recording import PROMPTS_PATH, Recorder, load_prompts

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
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
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


# Explicit pointer-sized signatures are required on 64-bit Python. In
# particular default c_int HWND/HDC return values truncate real Win32 handles.
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = HINSTANCE
kernel32.QueryPerformanceFrequency.argtypes = [ctypes.POINTER(w.LARGE_INTEGER)]
kernel32.QueryPerformanceCounter.argtypes = [ctypes.POINTER(w.LARGE_INTEGER)]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = w.ATOM
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 w.HWND, w.HMENU, HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = w.HWND
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.SetTimer.argtypes = [w.HWND, WPARAM, w.UINT, ctypes.c_void_p]
user32.SetTimer.restype = WPARAM
user32.BeginPaint.argtypes = [w.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = HDC
user32.EndPaint.argtypes = [w.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(RECT)]
user32.FillRect.argtypes = [HDC, ctypes.POINTER(RECT), HBRUSH]
user32.DrawTextW.argtypes = [HDC, w.LPCWSTR, ctypes.c_int, ctypes.POINTER(RECT), w.UINT]
user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.LoadCursorW.argtypes = [HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = HCURSOR
user32.MessageBoxW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT]
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = HANDLE

_freq = w.LARGE_INTEGER()
if not kernel32.QueryPerformanceFrequency(ctypes.byref(_freq)) or _freq.value <= 0:
    raise OSError("QueryPerformanceFrequency failed")
_qpc_freq = int(_freq.value)

session = ImeSession()
_last_view = None
recorder: Recorder | None = None
_record_event_count = 0  # display snapshot; raw events still arrive on every key
_record_origin = 0
_callback_failed = False


def qpc_ticks() -> int:
    c = w.LARGE_INTEGER()
    if not kernel32.QueryPerformanceCounter(ctypes.byref(c)):
        raise OSError("QueryPerformanceCounter failed")
    return int(c.value)


def now_ms() -> int:
    return (qpc_ticks() - _record_origin) * 1000 // _qpc_freq


def view_key() -> tuple:
    if recorder is not None:
        return (recorder.index, recorder.focused, recorder.closed, _record_event_count)
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
    global _last_view, _record_event_count
    now = view_key()
    if now == _last_view:
        return
    if recorder is not None:
        # Prompt/focus changes refresh the count immediately as well.
        _record_event_count = len(recorder.events)
        now = view_key()
    _last_view = now
    user32.InvalidateRect(hwnd, None, False)


def paint(hwnd) -> None:
    ps = PAINTSTRUCT()
    hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
    rc = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    brush = gdi32.GetStockObject(WHITE_BRUSH)
    user32.FillRect(hdc, ctypes.byref(rc), brush)
    if recorder is not None:
        prompt = recorder.prompt
        text = (
            "Stage 1 実打鍵記録 — ノート PC の Space 1 本で入力\n"
            "普段の速度で、誤りは直さず最後まで入力してください。\n"
            "Space+文字 = 親指シフト。Space 単独・Enter も記録します。\n"
            "全キーを離して F12 を押して離す = 次の文（やり直しなし）。\n"
            "Backspace / Esc でも記録は消えません。\n\n"
        )
        if recorder.closed:
            text += "記録が終了しました。この窓を閉じてください。"
        elif not recorder.focused:
            text += "フォーカスが外れた文は除外しました。戻ると次の文へ進みます。"
        elif prompt is not None:
            text += (f"お題 {recorder.index + 1}/{len(recorder.prompts)}\n\n"
                     f"{prompt.text}\n\n入力する読み: {prompt.reading}\n\n"
                     f"記録イベント数: {_record_event_count}\n"
                     "漢字の変換結果ではなく、上の読み・句読点・数字を入力。\n"
                     "入力結果は表示しません。打ち間違いがあっても F12 で次へ。")
        user32.DrawTextW(hdc, text, -1, ctypes.byref(rc), DT_WORDBREAK | DT_NOPREFIX)
        user32.EndPaint(hwnd, ctypes.byref(ps))
        return
    body = session.committed
    comp = session.composition
    status = "変換済" if session.converted else "入力中"
    text = (
        "oyayubi-ime 試験窓  （Notepad 用ではない。TSF TIP は src/tip）\n"
        "Space+文字 = 親指シフト（左右不明）。Space 単独 = 変換。Enter = 確定。Esc = 取消。\n"
        "F6 = 注目候補の学習を忘却。F7 = 直前の学習を戻す（確定文字は保持）。\n"
        f"[{status}] tokens={len(session.tokens)} fsm={session.fsm.state}\n\n"
    )
    if not comp:
        text += f"{body}|"
    else:
        cands = ""
        if session.converted and session.candidates:
            bits = []
            for i, c in enumerate(session.candidates):
                mark = ">" if i == session.cand_index else " "
                bits.append(f"{mark}{i + 1}.{c}")
            cands = "\n" + "  ".join(bits)
        text += f"{body}〖{comp}〗{cands}"
    user32.DrawTextW(hdc, text, -1, ctypes.byref(rc), DT_WORDBREAK | DT_NOPREFIX)
    user32.EndPaint(hwnd, ctypes.byref(ps))


def handle_key(hwnd, down: bool, vk: int) -> None:
    if recorder is not None:
        recorder.on_key(vk, down, now_ms())
        redraw_if_changed(hwnd)
        return
    extra = session.on_key(down, vk, now_ms())
    if extra:
        session.committed += extra
    redraw_if_changed(hwnd)


@WNDPROC
def wndproc(hwnd, msg, wparam, lparam):
    # Exceptions must not escape a ctypes callback (which would keep the window
    # alive while silently dropping key events, e.g. after a disk write failure).
    global _callback_failed
    if _callback_failed:
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    try:
        return dispatch(hwnd, msg, wparam, lparam)
    except Exception as exc:
        # The error dialog itself sends focus/paint messages. Stop recording
        # before showing it, otherwise a failed flush could recurse forever.
        _callback_failed = True
        user32.MessageBoxW(hwnd, f"処理を停止しました: {type(exc).__name__}: {exc}",
                          "oyayubi-ime 記録エラー", 0x10)
        user32.PostQuitMessage(1)
        return 0


def dispatch(hwnd, msg, wparam, lparam):
    global _record_event_count
    if msg == WM_DESTROY:
        if recorder is not None:
            recorder.close(now_ms())
        user32.PostQuitMessage(0)
        return 0
    if msg == WM_PAINT:
        paint(hwnd)
        return 0
    if msg == WM_ERASEBKGND:
        return 1
    if msg == WM_TIMER:
        if recorder is not None:
            _record_event_count = len(recorder.events)
            redraw_if_changed(hwnd)
        elif session.fsm.timer_deadline is not None:
            extra = session.on_timeout(now_ms())
            if extra:
                session.committed += extra
            redraw_if_changed(hwnd)
        return 0
    if msg in (WM_SETFOCUS, WM_KILLFOCUS) and recorder is not None:
        recorder.on_focus(msg == WM_SETFOCUS, now_ms())
        redraw_if_changed(hwnd)
        return 0
    if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
        handle_key(hwnd, True, int(wparam) & 0xFF)
        if recorder is not None and msg == WM_SYSKEYDOWN:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0
    if msg in (WM_KEYUP, WM_SYSKEYUP):
        handle_key(hwnd, False, int(wparam) & 0xFF)
        if recorder is not None and msg == WM_SYSKEYUP:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0
    if msg == WM_CHAR:
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> None:
    global recorder, _record_origin
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, help="record Stage 1 raw keys to a NEW JSONL file")
    parser.add_argument("--prompts", type=Path, default=PROMPTS_PATH, help="Stage 1 prompt JSON")
    args = parser.parse_args()
    stream = None
    if args.record:
        prompts = load_prompts(args.prompts)
        # Validate every reading before collecting an irreplaceable real run.
        from tools.replay_stage1 import expected_tokens
        for prompt in prompts:
            expected_tokens(prompt.reading)
        args.record.parent.mkdir(parents=True, exist_ok=True)
        stream = args.record.open("x", encoding="utf-8", newline="\n")
        _record_origin = qpc_ticks()
        recorder = Recorder(stream, prompts, qpc_frequency=_qpc_freq, qpc_origin=_record_origin)
        print(f"recording to {args.record.resolve()} (F12 = next prompt)")
    else:
        print("loading dictionary (first run is slow)...")
        session.load()
        print("ready")
    try:
        run_window()
    finally:
        if stream is not None:
            try:
                recorder.close(now_ms())
            finally:
                stream.close()


def run_window() -> None:
    inst = kernel32.GetModuleHandleW(None)
    cls_name = "OyayubiImeHost"
    wc = WNDCLASSW()
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = wndproc
    wc.hInstance = inst
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
    if not hwnd:
        raise OSError(f"CreateWindowExW failed ({ctypes.GetLastError()})")
    if recorder is not None:
        # A Windows IME/TSF must not consume kana keystrokes in this raw window.
        imm32 = ctypes.windll.imm32
        imm32.ImmAssociateContext.argtypes = [w.HWND, HANDLE]
        imm32.ImmAssociateContext.restype = HANDLE
        imm32.ImmAssociateContext(hwnd, None)
    user32.ShowWindow(hwnd, 1)
    user32.SetTimer(hwnd, TIMER_ID, 500 if recorder is not None else 16, None)
    msg = w.MSG()
    while True:
        result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if result == -1:
            raise OSError("GetMessageW failed")
        if result == 0:
            if msg.wParam:
                raise RuntimeError("window processing stopped after an error")
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
