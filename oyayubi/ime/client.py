"""Reference TIP lifecycle: connect first, otherwise spawn and wait off the key thread.

python -m oyayubi.ime.client [--runtime-dir PATH] checks/starts the resident server.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import time

from .resident import DEFAULT_RUNTIME_DIR, VERSION, read_message, write_message
from .runtime_platform import spawn_detached


class ResidentClient:
    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.stream = connection.makefile("rwb")

    def request(self, msg: dict) -> dict:
        """One outstanding request at a time; failures close the ambiguous stream."""
        try:
            write_message(self.stream, msg)
            return read_message(self.stream)
        except (OSError, EOFError, ValueError):
            self.close()
            raise

    def close(self) -> None:
        self.stream.close()
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect(directory: Path = DEFAULT_RUNTIME_DIR, *, timeout=0.25) -> ResidentClient:
    endpoint = json.loads((directory / "endpoint.json").read_text(encoding="utf-8"))
    if endpoint["version"] != VERSION:
        raise ValueError("unsupported endpoint version")
    connection = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=timeout)
    client = ResidentClient(connection)
    try:
        reply = client.request({"op": "hello", "version": VERSION, "token": endpoint["token"]})
        if reply != {"ok": True, "msg": "ready", "version": VERSION}:
            raise ValueError("invalid ready reply")
        return client
    except BaseException:
        client.close()
        raise


def connect_or_start(directory: Path = DEFAULT_RUNTIME_DIR, *, timeout=15.0,
                     request_timeout=0.15, server_args=(), spawn=spawn_detached) -> ResidentClient:
    """Bounded startup/recovery. The caller passes through keys while this runs.

    Does not retry key requests or restore composition. Reconnection is a new session.
    """
    deadline = time.monotonic() + timeout

    def try_connect():
        client = connect(directory, timeout=min(0.25, max(0.001, deadline - time.monotonic())))
        client.connection.settimeout(request_timeout)
        return client

    try:
        return try_connect()
    except (OSError, EOFError, ValueError, KeyError):
        pass
    child = spawn([sys.executable, "-m", "oyayubi.ime.server", "--resident",
                   "--runtime-dir", str(directory.resolve()), *server_args])
    try:
        while time.monotonic() < deadline:
            try:
                return try_connect()
            except (OSError, EOFError, ValueError, KeyError):
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        raise TimeoutError("resident server not ready")
    finally:
        # Reap an already-exited duplicate starter. Never terminate the resident.
        child.poll()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    args = parser.parse_args()
    with connect_or_start(args.runtime_dir):
        print("resident server ready")


if __name__ == "__main__":
    main()
