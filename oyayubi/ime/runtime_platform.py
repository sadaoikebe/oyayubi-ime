"""Small OS boundary: process-lifetime lock and detached Python launch."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import subprocess


@contextmanager
def process_lock(path: Path):
    """Yield whether the lock was acquired. Never unlink a live lock inode."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            # msvcrt locks a byte range, including in an initially empty file.
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EDEADLK):  # held by a peer
                    raise
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        # Closing the descriptor also releases its lock after crashes/exceptions.
        os.close(fd)


def spawn_detached(command: list[str]) -> subprocess.Popen:
    options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[2],
                            close_fds=True, **options)
