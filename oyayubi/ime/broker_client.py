"""The management tools' line to the broker (REDESIGN §9.4): its state, and "retry".

The broker listens on \\\\.\\pipe\\oyayubi-ime-<user SID>-<session> (on POSIX, where
only the tests run it, ~/.oyayubi-ime/broker.sock). These functions connect,
say hello as a TIP would, and either read the state or ask for a retry (which
also restarts a running engine, so that new settings are read). Nothing here
starts anything.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading

HELLO = {"op": "hello", "proto": 1, "tag": "manage", "bits": 64}


def pipe_address() -> str:
    if os.name != "nt":
        return str(Path.home() / ".oyayubi-ime" / "broker.sock")
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_pointer), ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
        sid = text.value
        kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)
    session = wintypes.DWORD()
    kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    return rf"\\.\pipe\oyayubi-ime-{sid}-{session.value}"


class _Connection:
    def __init__(self, address: str, timeout: float):
        self.timeout = timeout
        if os.name == "nt":
            self.file = open(address, "r+b", buffering=0)  # FileNotFoundError: no broker
            self.sock = None
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(address)

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode("utf-8")
        if self.sock is not None:
            self.sock.sendall(data)
        else:
            self.file.write(data)

    def read_line(self) -> dict | None:
        """One line within the timeout; None when none came."""
        result: list = []

        def read():
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = self.sock.recv(4096) if self.sock is not None else self.file.read(4096)
                if not chunk:
                    return
                buffer += chunk
            result.append(json.loads(buffer.split(b"\n", 1)[0]))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(self.timeout)
        return result[0] if result else None

    def close(self) -> None:
        (self.sock if self.sock is not None else self.file).close()


def _connect(address: str | None, timeout: float) -> _Connection | None:
    try:
        return _Connection(address or pipe_address(), timeout)
    except OSError:
        return None  # no broker: not running for this user


def status(address: str | None = None, timeout: float = 1.0) -> dict | None:
    """{"state": ready|starting|down|..., "warnings": [...]}; None when no broker answers."""
    connection = _connect(address, timeout)
    if connection is None:
        return None
    try:
        connection.send(HELLO)
        reply = connection.read_line()
    except OSError:
        return None
    finally:
        connection.close()
    if reply is None:
        return None
    return {"state": reply.get("state"), "warnings": reply.get("warnings", [])}


def retry(address: str | None = None, timeout: float = 1.0) -> bool:
    """Ask the broker to start the engine again; False when no broker answers."""
    connection = _connect(address, timeout)
    if connection is None:
        return False
    try:
        connection.send(HELLO)
        if connection.read_line() is None:
            return False
        connection.send({"op": "retry"})
        return True
    except OSError:
        return False
    finally:
        connection.close()


def last_stop(logs: Path) -> dict | None:
    """The last stop the broker told the user about (logs/notices.log), with its time."""
    path = Path(logs) / "notices.log"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            notice = json.loads(line)
        except ValueError:
            continue
        if "stopped" in notice:
            return notice
    return None
