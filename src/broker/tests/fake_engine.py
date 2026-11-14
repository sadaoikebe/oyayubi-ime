"""A stand-in for yuragi.engine in the broker tests (REDESIGN §5.7).

    python fake_engine.py STATE_DIR

STATE_DIR/plan.json lists what each start does, in order (the last entry repeats):
  "normal"                 ready, then one reply per request
  "crash"                  exit 1 at once, before ready
  {"crash_after": N}       ready; exit 3 on receiving the Nth request, unanswered
  "silent"                 ready; read requests, never answer
  "no_ready"               never say ready (sleeps)
  {"exit": 21}             "fatal 21 test" on stderr, exit 21 before ready
  {"warnings": ["config"]} ready with these warnings, then like "normal"
Each start appends "pid birth" to STATE_DIR/starts and writes every line it receives
to STATE_DIR/received-<start index>.jsonl.

    python fake_engine.py --admin STATE_DIR

stands for the admin command: appends "pid birth" to STATE_DIR/admins and leaves.
"""
import json
import os
from pathlib import Path
import sys
import time


def birth(pid: int):
    """When the running process with this pid started; None once it has exited
    (a zombie counts as exited). With the pid it names one process: Windows
    soon gives a freed pid to another."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            return None
        try:
            if kernel32.WaitForSingleObject(handle, 0) != 0x102:  # not WAIT_TIMEOUT: exited
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            kernel32.GetProcessTimes(handle, *map(ctypes.byref, times))
            return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()  # from the state on
    except FileNotFoundError:
        return None
    return None if fields[0] == "Z" else int(fields[19])  # starttime


def record(path: Path) -> None:
    with path.open("a") as stream:
        stream.write(f"{os.getpid()} {birth(os.getpid())}\n")


def main() -> int:
    if sys.argv[1] == "--admin":
        record(Path(sys.argv[2]) / "admins")
        return 0
    state = Path(sys.argv[1])
    plan = json.loads((state / "plan.json").read_text(encoding="utf-8"))
    starts = state / "starts"
    index = len(starts.read_text().splitlines()) if starts.exists() else 0
    record(starts)
    mode = plan[min(index, len(plan) - 1)]
    kind = mode if isinstance(mode, str) else next(iter(mode))
    print(f"fake engine start {index} mode {json.dumps(mode)}", file=sys.stderr, flush=True)
    if kind == "crash":
        return 1
    if kind == "exit":
        print(f"fatal {mode['exit']} test", file=sys.stderr, flush=True)
        return mode["exit"]
    if kind == "no_ready":
        time.sleep(3600)
        return 0
    warnings = mode["warnings"] if kind == "warnings" else []
    out = sys.stdout
    out.write(json.dumps({"ready": True, "proto": 1, "warnings": warnings}) + "\n")
    out.flush()
    received = (state / f"received-{index}.jsonl").open("a", encoding="utf-8")
    direct = {}
    requests = 0
    for line in sys.stdin:
        received.write(line)
        received.flush()
        msg = json.loads(line)
        if msg.get("op") in ("open", "close"):
            continue
        requests += 1
        if kind == "crash_after" and requests >= mode["crash_after"]:
            return 3
        if kind == "silent":
            continue
        sid = msg["sid"]
        if msg.get("op") == "set_direct":
            direct[sid] = msg["direct"]
        elif msg.get("op") == "toggle_direct":
            direct[sid] = not direct.get(sid, False)
        out.write(json.dumps({"sid": sid, "seq": msg["seq"], "direct": direct.get(sid, False),
                              "composition": f"{msg.get('op')}:{msg.get('vk')}",
                              "commit": "", "converted": False, "pending": False}) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
