"""The engine only: LM rescoring off the key path (design 2026-11-12, D1-D8).

LmWorker owns the scorer on one daemon thread. ConnectionPipeline sits between a
connection and its ImeSession: a ThumbTap with LM work leaves the session
awaiting; the request waits at most GRACE_MS for the worker, otherwise replies
``pending`` with the display unchanged. Keys that arrive while pending are
queued in order and replayed once the LM picks (or, after DEADLINE_MS, the
Viterbi 1-best) resolve the conversion, so the result equals the synchronous one.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import gc
import json
import sys
import threading
import time

from .lm_rescore import conversion_picks
from .server import apply_op, snapshot
from .session import ConversionRequest, ImeSession

GRACE_MS = 30
DEADLINE_MS = 400
POLL_MS = 10
PREFETCH_DELAY_MS = 30
MAX_QUEUE = 512
STALL_MS = 5000
MAX_ERRORS = 3
EDGES_MS = (1, 2, 5, 10, 20, 30, 50, 100, 150, 250, 400, 1000, float("inf"))


@dataclass(eq=False)
class Job:
    kind: str  # "convert" (prefetch too) | "prime"
    owner: object  # the connection's pipeline
    request: ConversionRequest | None
    context: str = ""  # prime only
    wanted: bool = False  # a conversion is waiting for this result
    not_before: float = 0.0  # time.monotonic(); delays a prefetch
    deadline: float = 0.0  # when wanted: give up and use the Viterbi 1-best
    cancelled: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    picks: list | None = None
    error: bool = False
    stats: dict = field(default_factory=dict)  # LmRescorer.last + run time


class Histogram:
    def __init__(self):
        self.counts = [0] * len(EDGES_MS)

    def add(self, ms: float) -> None:
        self.counts[next(i for i, edge in enumerate(EDGES_MS) if ms <= edge)] += 1

    def report(self) -> dict:
        return {"le_ms": [e if e != float("inf") else "inf" for e in EDGES_MS],
                "counts": list(self.counts)}


class LmStats:
    """Counts since server start, no text (design §8)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self.request_ms = Histogram()
        self.wait_ms = Histogram()
        self.job_ms = Histogram()
        self.conversions = dict.fromkeys(("inline", "pending", "timeout", "bypass", "error"), 0)
        self.adopted = 0
        self.prefetch = {"started": 0, "cancelled": 0}
        self.worker_busy_ms = 0.0

    def observe(self, histogram: str, ms: float) -> None:
        with self.lock:
            getattr(self, histogram).add(ms)

    def conversion(self, kind: str) -> None:
        with self.lock:
            self.conversions[kind] += 1

    def bump(self, name: str, key: str | None = None) -> None:
        with self.lock:
            if key is None:
                setattr(self, name, getattr(self, name) + 1)
            else:
                getattr(self, name)[key] += 1

    def busy(self, ms: float) -> None:
        with self.lock:
            self.worker_busy_ms += ms

    def report(self, lm: str) -> dict:
        with self.lock:
            return {"lm": lm, "request_ms": self.request_ms.report(),
                    "conversions": dict(self.conversions), "wait_ms": self.wait_ms.report(),
                    "job_ms": self.job_ms.report(), "adopted": self.adopted,
                    "prefetch": dict(self.prefetch),
                    "worker_busy_ms": round(self.worker_busy_ms, 3),
                    "uptime_s": round(self.clock() - self.started, 3)}


class LmWorker:
    """One daemon thread; the only user of the scorer."""

    def __init__(self, loader, lex, *, stats: LmStats | None = None, stall_ms: float = STALL_MS,
                 clock=time.monotonic, on_disabled=None):
        """on_disabled(reason): told once when the LM is not used any more
        ("unavailable": it could not be loaded; "errors": it failed MAX_ERRORS
        times in a row). Called on the worker thread."""
        self.loader = loader
        self.on_disabled = on_disabled
        self.lex = lex
        self.stats = stats if stats is not None else LmStats()
        self.stall_ms = stall_ms
        self.clock = clock
        self.state = "loading"
        self.template = None
        self._cond = threading.Condition()
        self._wanted: deque[Job] = deque()  # first come, first served
        self._slots: dict[tuple[int, str], Job] = {}  # (owner, kind) -> queued prefetch/prime
        self._running: Job | None = None
        self._running_since = 0.0
        self._errors = 0
        self.thread = threading.Thread(target=self._run, name="lm-worker", daemon=True)
        self.thread.start()

    # -- connection side ----------------------------------------------------
    def available(self) -> bool:
        with self._cond:
            stalled = (self._running is not None
                       and (self.clock() - self._running_since) * 1000 > self.stall_ms)
            return self.state == "ready" and not stalled

    def submit(self, job: Job) -> None:
        with self._cond:
            if job.wanted:
                self._wanted.append(job)
            else:
                slot = (id(job.owner), job.kind)
                old = self._slots.pop(slot, None)
                if old is not None:
                    self._drop(old)
                running = self._running
                if (running is not None and running.owner is job.owner
                        and running.kind == job.kind and not running.wanted):
                    running.cancelled = True  # stops at the next kana-run boundary
                self._slots[slot] = job
            self._cond.notify()

    def promote(self, job: Job, deadline: float) -> None:
        """A conversion now waits for this prefetch (queued, running or done)."""
        with self._cond:
            job.wanted = True
            job.deadline = deadline
            slot = (id(job.owner), job.kind)
            if self._slots.get(slot) is job:
                del self._slots[slot]
                self._wanted.appendleft(job)
                self._cond.notify()

    def cancel_owner(self, owner) -> None:
        with self._cond:
            for job in [j for j in self._wanted if j.owner is owner]:
                self._wanted.remove(job)
                self._drop(job)
            for slot in [s for s, j in self._slots.items() if j.owner is owner]:
                self._drop(self._slots.pop(slot))
            if self._running is not None and self._running.owner is owner:
                self._running.cancelled = True

    def _drop(self, job: Job) -> None:
        """A queued job that will never run (caller holds the lock)."""
        job.cancelled = True
        if job.kind == "convert" and not job.wanted:
            self.stats.bump("prefetch", "cancelled")
        job.done.set()

    # -- worker thread ------------------------------------------------------
    def _run(self) -> None:
        try:
            template = self.loader()
            template.scorer.nll("", ["あ", "い"])  # warm up before "ready"
        except Exception as exc:
            print(f"LM rescoring unavailable ({type(exc).__name__}); continuing without it",
                  file=sys.stderr, flush=True)
            with self._cond:
                self.state = "disabled"
            if self.on_disabled is not None:
                self.on_disabled("unavailable")
            return
        gc.freeze()  # the model's objects must not be traversed by full GC
        with self._cond:
            self.template = template
            self.state = "ready"
        while True:
            self._execute(self._next())

    def _next(self) -> Job:
        with self._cond:
            while True:
                if self._wanted:
                    job = self._wanted.popleft()
                else:
                    now = self.clock()
                    ready = [s for s, j in self._slots.items() if j.not_before <= now]
                    if ready:
                        job = self._slots.pop(min(ready, key=lambda s: self._slots[s].not_before))
                    else:
                        waits = [j.not_before - now for j in self._slots.values()]
                        self._cond.wait(min(waits) if waits else None)
                        continue
                if job.cancelled:
                    job.done.set()
                    continue
                self._running, self._running_since = job, self.clock()
                return job

    def _execute(self, job: Job) -> None:
        start = self.clock()
        if job.kind == "convert" and not job.wanted:
            self.stats.bump("prefetch", "started")
        try:
            if job.kind == "convert":
                rescorer = replace(self.template, last={})
                request = job.request
                job.picks = conversion_picks(
                    request.parts, self.lex, request.learning, request.left_surface,
                    request.lm_context, rescorer, cancelled=lambda: job.cancelled)
                job.stats = dict(rescorer.last)
            else:
                prime = getattr(self.template.scorer, "prime", None)
                if prime is not None:
                    prime(job.context)
            self._errors = 0
        except Exception as exc:
            job.error = True
            self._errors += 1
            print(f"LM job failed ({type(exc).__name__}); {self._errors} in a row",
                  file=sys.stderr, flush=True)
        elapsed = (self.clock() - start) * 1000
        job.stats["job_ms"] = elapsed
        self.stats.busy(elapsed)
        disabled = False
        with self._cond:
            self._running = None
            if self._errors >= MAX_ERRORS and self.state != "disabled":
                self.state = "disabled"
                disabled = True
            if job.wanted:
                self.stats.observe("job_ms", elapsed)
            elif job.kind == "convert" and job.cancelled:
                self.stats.bump("prefetch", "cancelled")
            job.done.set()
        if disabled:
            print(f"LM rescoring disabled after {MAX_ERRORS} failures in a row",
                  file=sys.stderr, flush=True)
            if self.on_disabled is not None:
                self.on_disabled("errors")


PENDING = object()


class ConnectionPipeline:
    """Per connection. Called with the server's dispatch lock held."""

    def __init__(self, sess: ImeSession, worker, *, stats: LmStats | None = None,
                 grace_ms: float = GRACE_MS, deadline_ms: float = DEADLINE_MS,
                 poll_ms: int = POLL_MS, prefetch: str = "thumb",  # thumb | typing | off
                 prefetch_delay_ms: float = PREFETCH_DELAY_MS, max_queue: int = MAX_QUEUE,
                 clock=time.monotonic, log_text: bool = False, lm_timing: bool = False):
        self.sess = sess
        self.worker = worker
        self.stats = stats if stats is not None else worker.stats
        self.grace_ms = grace_ms
        self.deadline_ms = deadline_ms
        self.poll_ms = poll_ms
        self.prefetch = prefetch
        self.prefetch_delay_ms = prefetch_delay_ms
        self.max_queue = max_queue
        self.clock = clock
        self.log_text = log_text
        self.lm_timing = lm_timing
        self.queue: deque[dict] = deque()
        self.last_t = 0
        self.job: Job | None = None  # the awaited conversion's job
        self.awaited_at = 0.0
        self.spec: Job | None = None  # the last prefetch
        self.spec_key: tuple | None = None
        self.primed = ""  # left context last sent to prime

    def handle(self, msg: dict) -> dict:
        op = msg.get("op")
        if "t" in msg:
            self.last_t = int(msg["t"])
        if self.sess._await is not None and op in ("reset", "set_direct", "toggle_direct"):
            self._abandon()
        if (self.sess._await is not None and op == "timeout"
                and self.queue and self.queue[-1].get("op") == "timeout"):
            self.queue[-1] = msg  # consecutive timeouts: the last one is equivalent
        else:
            self.queue.append(msg)
        commit, grace, inline = "", False, False
        while True:
            if self.sess._await is not None:
                outcome = self._outcome(wait=grace)
                grace = False
                if outcome is PENDING:
                    break
                self._resolved(outcome, inline)
                commit += self.sess.finish_conversion(outcome)
                self.job = None
                continue
            if not self.queue:
                break
            m = self.queue.popleft()
            available = self.worker.available()
            self.sess.lm_deferred = available
            was_converted = self.sess.converted
            commit += apply_op(self.sess, m)
            if (not available and not was_converted and self.sess.converted
                    and not self.sess.predicting):
                self.stats.conversion("bypass")
            if self.sess._await is not None:
                self._submit_convert()
                grace = inline = True
        self._prefetch()
        reply = snapshot(self.sess, op)
        reply["commit"] = commit
        reply["pending"] = self.sess._await is not None
        if reply["pending"]:
            poll = self.last_t + self.poll_ms
            reply["timer"] = poll if reply["timer"] is None else min(reply["timer"], poll)
        if self.log_text:
            print(json.dumps({"composition": reply["composition"], "commit": reply["commit"]},
                             ensure_ascii=False), file=sys.stderr, flush=True)
        return reply

    def close(self) -> None:
        """Connection ended: nothing of this connection may run any more."""
        self.worker.cancel_owner(self)

    def _abandon(self) -> None:
        self.worker.cancel_owner(self)
        self.queue.clear()
        self.sess._await = None
        self.sess._continuation = None
        self.job = None

    def _submit_convert(self) -> None:
        request = self.sess._await
        deadline = self.clock() + self.deadline_ms / 1000
        spec = self.spec
        if (spec is not None and not spec.cancelled and not spec.error
                and spec.request.key == request.key):
            self.worker.promote(spec, deadline)
            self.job = spec
            self.stats.bump("adopted")
        else:
            self.worker.cancel_owner(self)
            self.job = Job("convert", self, request, wanted=True, deadline=deadline)
            self.worker.submit(self.job)
        self.awaited_at = self.clock()

    def _outcome(self, *, wait: bool):
        job = self.job
        if wait:
            job.done.wait(self.grace_ms / 1000)
        if job.done.is_set():
            return None if job.error or job.picks is None else job.picks
        if self.clock() >= job.deadline or len(self.queue) > self.max_queue:
            job.cancelled = True
            return None
        return PENDING

    def _resolved(self, picks, inline: bool) -> None:
        job = self.job
        if picks is not None:
            kind = "inline" if inline else "pending"
        elif job.done.is_set() and job.error:
            kind = "error"
        else:
            kind = "timeout"
        self.stats.conversion(kind)
        self.stats.observe("wait_ms", (self.clock() - self.awaited_at) * 1000)
        if self.lm_timing and job.done.is_set() and job.stats:
            print(json.dumps({"lm": {k: round(v, 3) if isinstance(v, float) else v
                                     for k, v in job.stats.items()}}),
                  file=sys.stderr, flush=True)

    def _prefetch(self) -> None:
        sess = self.sess
        if sess._await is not None or not self.worker.available():
            return
        if sess._lm_context != self.primed:
            self.primed = sess._lm_context
            self.worker.submit(Job("prime", self, None, context=sess._lm_context))
        if self.prefetch == "off" or sess.converted or not sess.tokens:
            return
        if self.prefetch == "thumb" and sess.fsm.state != "S3":
            return  # S3: only a thumb key is down, awaiting the chord decision
        request = sess.conversion_request()
        if request is None or request.key == self.spec_key:
            return
        delay = self.prefetch_delay_ms if self.prefetch == "thumb" else 0
        self.spec = Job("convert", self, request, not_before=self.clock() + delay / 1000)
        self.spec_key = request.key
        self.worker.submit(self.spec)
