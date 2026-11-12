"""Resident LM pipeline (design 2026-11-12 §7, tests 2-6): fake scorers, no torch.

The central check is equivalence: every event sequence gives the same commits and
the same final state as the synchronous session with the same scorer, however
late the LM result arrives.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading
import time
import unittest

from oyayubi.chord.keys import VK_TO_M
from oyayubi.ime.lm_async import ConnectionPipeline, Job, LmStats, LmWorker
from oyayubi.ime.lm_rescore import LmRescorer, conversion_picks
from oyayubi.ime.server import dispatch
from oyayubi.ime.session import ImeSession
from oyayubi.lattice.mozc_dict import Entry, MozcLex, load_lex
from oyayubi.lattice.table import load_table, reverse_table
from tests.transcript import make_session

TRANSCRIPTS = sorted((Path(__file__).parent / "transcripts").glob("*.json"))
# Up of an unclassified key: a no-op for every session state. Sent last to both
# sides so the subject has one request in which to resolve what is still pending.
TERMINAL = {"op": "up", "vk": 0x7B}
VK_SPACE, VK_RETURN, VK_BACK, VK_ESCAPE, VK_F7 = 0x20, 0x0D, 0x08, 0x1B, 0x76
VK_DOWN, VK_RIGHT = 0x28, 0x27


class PickyScorer:
    """Deterministic, context-dependent NLL that often overrides the 1-best."""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def nll(self, context, candidates):
        self.calls.append((context, list(candidates)))
        return [sum(ord(ch) % 7 for ch in c) * 0.7 + (len(context) % 3) * len(c)
                for c in candidates]


class ManualWorker:
    """LmWorker's interface; the test decides when each wanted job completes."""

    def __init__(self, lex, scorer, delay=lambda index: 0, eager_prefetch=False):
        self.lex = lex
        self.template = LmRescorer(scorer)
        self.delay = delay  # conversion index -> completes before the Nth later request
        self.eager_prefetch = eager_prefetch
        self.stats = LmStats()
        self.ready = True
        self.jobs: list[Job] = []
        self.countdown: dict[int, int] = {}
        self.conversions = 0
        self.draining = False  # complete_all(): later conversions complete at once too

    def available(self):
        return self.ready

    def submit(self, job):
        self.jobs.append(job)
        if job.wanted:
            self._schedule(job)
        elif job.kind == "prime" or self.eager_prefetch:
            self.complete(job)

    def promote(self, job, deadline):
        job.wanted, job.deadline = True, deadline
        if not job.done.is_set():
            self._schedule(job)

    def cancel_owner(self, owner):
        for job in self.jobs:
            if job.owner is owner and not job.done.is_set():
                job.cancelled = True

    def _schedule(self, job):
        n = self.delay(self.conversions)
        self.conversions += 1
        if n == 0 or self.draining:
            self.complete(job)
        else:
            self.countdown[id(job)] = n

    def complete(self, job):
        if job.done.is_set():
            return
        if job.kind == "convert" and not job.cancelled:
            r = job.request
            job.picks = conversion_picks(r.parts, self.lex, r.learning, r.left_surface,
                                         r.lm_context, replace(self.template, last={}),
                                         cancelled=lambda: job.cancelled)
        job.done.set()

    def tick(self):
        """Called before each request."""
        for job in self.jobs:
            if id(job) in self.countdown:
                self.countdown[id(job)] -= 1
                if self.countdown[id(job)] == 0:
                    del self.countdown[id(job)]
                    self.complete(job)

    def complete_all(self):
        self.draining = True
        for job in self.jobs:
            if job.wanted:
                self.complete(job)
        self.countdown.clear()


def final_state(reply: dict) -> dict:
    return {k: v for k, v in reply.items() if k not in ("pending", "timer", "commit")}


def run_reference(new_session, events, scorer):
    sess = new_session()
    sess.lm = LmRescorer(scorer) if scorer is not None else None
    commits = ""
    for event in events + [TERMINAL]:
        reply = dispatch(sess, dict(event))
        commits += reply["commit"]
    return commits, final_state(reply)


def run_subject(new_session, events, scorer, *, delay=lambda i: 0, prefetch="thumb",
                eager_prefetch=False):
    sess = new_session()
    worker = ManualWorker(sess._lex, scorer, delay, eager_prefetch)
    pipe = ConnectionPipeline(sess, worker, grace_ms=0, prefetch=prefetch,
                              clock=lambda: 0.0)  # never reaches the 400 ms deadline
    commits = ""
    for event in events + [TERMINAL]:
        worker.tick()
        if event is TERMINAL:
            worker.complete_all()
        reply = pipe.handle(dict(event))
        commits += reply["commit"]
    assert not reply["pending"]
    return commits, final_state(reply), worker


class Typist:
    """Timed key events for kana text on NICOLA-A (US VKs)."""

    def __init__(self):
        self.reverse = reverse_table(load_table())
        self.vk = {key: vk for vk, key in VK_TO_M.items()}
        self.t = 0
        self.events: list[dict] = []

    def at(self, gap):
        self.t += gap
        return self.t

    def key(self, op, vk, gap=0, **extra):
        self.events.append({"op": op, "vk": vk, "t": self.at(gap), **extra})
        return self

    def tap(self, vk, gap=150, hold=30):
        return self.key("down", vk, gap).key("up", vk, hold)

    def kana(self, text, gap=150):
        for ch in text:
            token = self.reverse[ch]
            vk = self.vk[token["key"]]
            if token["kind"] == "plain":
                self.tap(vk, gap)
            else:  # chord with the thumb: thumb 10 ms after, letter released first
                self.key("down", vk, gap).key("down", VK_SPACE, 10)
                self.key("up", vk, 30).key("up", VK_SPACE, 5)
        return self

    def space(self, gap=150, hold=40):
        return self.tap(VK_SPACE, gap, hold)

    def timeout(self, gap):
        self.events.append({"op": "timeout", "t": self.at(gap)})
        return self


def extra_cases() -> dict[str, list[dict]]:
    """Design §7 test 2, timed cases. Every reading here is one PickyScorer
    converts differently from the Viterbi 1-best (checked by a test)."""
    cases = {}
    t = Typist().kana("きしゃ").space().kana("かいせい", gap=20).space().tap(VK_RETURN)
    cases["convert_then_type_immediately"] = t.events
    for name, vk in (("enter", VK_RETURN), ("backspace", VK_BACK), ("escape", VK_ESCAPE),
                     ("f7", VK_F7), ("period", 0x51), ("minus", 0xBD),
                     ("digit", ord("1"))):
        # Space held under 80 ms, then the key: the chord resolves inside its event.
        t = Typist().kana("しんこう").key("down", VK_SPACE, 150).tap(vk, 50, 20)
        t.key("up", VK_SPACE, 10).kana("かんこう", gap=100).space().tap(VK_RETURN)
        cases[f"space_held_then_{name}"] = t.events
    t = Typist().kana("こうかい").key("down", VK_SPACE, 150).timeout(80)
    t.key("up", VK_SPACE, 40).tap(VK_RETURN)
    cases["space_hold_timeout"] = t.events
    t = Typist().kana("かいせい").key("down", VK_SPACE, 150)
    t.kana("を", gap=120)  # past the deadline, before any timer: flush_timeout
    t.space().tap(VK_RETURN)
    cases["space_hold_flushed_by_key"] = t.events
    t = Typist().kana("きしゃ").space().space(gap=60).tap(VK_DOWN, 30)
    t.tap(VK_RIGHT, 30).tap(VK_RETURN, 30)
    t.kana("かいせい").space().tap(ord("3"), 30).tap(VK_RETURN, 30)
    cases["pending_cycle_arrow_digit"] = t.events
    t = Typist().kana("きしゃ").space().kana("かいせい", gap=30).space(gap=30)
    t.kana("かんこう", gap=30).space(gap=30).tap(VK_RETURN, 30)
    cases["pending_chain"] = t.events
    t = Typist().kana("きしゃ").tap(ord("1")).kana("かいせい").space().tap(VK_RETURN)
    cases["literal_between_runs"] = t.events
    return cases


class EquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_lex = load_lex()
        cls.cases = []
        for path in TRANSCRIPTS:
            case = json.loads(path.read_text(encoding="utf-8"))
            events = [step["event"] for step in case["steps"]]
            cls.cases.append((path.stem, lambda case=case: make_session(case, cls.real_lex),
                              events))
        real = lambda: ImeSession(_table=load_table(), _lex=cls.real_lex, learning_path=None)
        for name, events in extra_cases().items():
            cls.cases.append((name, real, events))

    def check(self, name, new_session, events, **kwargs):
        expected = run_reference(new_session, events, PickyScorer())
        commits, state, worker = run_subject(new_session, events, PickyScorer(), **kwargs)
        self.assertEqual((commits, state), expected, f"{name} {kwargs}")
        return worker

    def test_lm_changes_some_results(self):
        """Guard: the fake LM must actually differ from Viterbi, or nothing is tested."""
        changed = [name for name, new_session, events in self.cases
                   if run_reference(new_session, events, PickyScorer())
                   != run_reference(new_session, events, None)]
        self.assertGreaterEqual(len(changed), 5, changed)

    def test_every_completion_point(self):
        for name, new_session, events in self.cases:
            with self.subTest(case=name):
                conversions = self.check(name, new_session, events).conversions
                n = len(events) + 1
                for late in range(1, n + 1):  # every conversion equally late
                    self.check(name, new_session, events, delay=lambda i, late=late: late)
                for index in range(conversions):  # one conversion late, the rest inline
                    for late in range(1, n + 1):
                        self.check(name, new_session, events,
                                   delay=lambda i, index=index, late=late:
                                   late if i == index else 0)

    def test_prefetch_modes(self):
        for name, new_session, events in self.cases:
            for prefetch in ("off", "thumb", "typing"):
                for eager in (False, True):
                    with self.subTest(case=name, prefetch=prefetch, eager=eager):
                        self.check(name, new_session, events, prefetch=prefetch,
                                   eager_prefetch=eager, delay=lambda i: 2)

    def test_extra_cases_depend_on_the_lm(self):
        for name, new_session, events in self.cases[len(TRANSCRIPTS):]:
            with self.subTest(case=name):
                self.assertNotEqual(run_reference(new_session, events, PickyScorer()),
                                    run_reference(new_session, events, None))

    def test_extra_cases_actually_pend(self):
        """The timed cases must put keys behind a pending conversion."""
        for name, new_session, events in self.cases[len(TRANSCRIPTS):]:
            sess = new_session()
            worker = ManualWorker(sess._lex, PickyScorer(), delay=lambda i: 10 ** 6)
            pipe = ConnectionPipeline(sess, worker, grace_ms=0, clock=lambda: 0.0)
            queued = 0
            for event in events:
                pipe.handle(dict(event))
                queued = max(queued, len(pipe.queue))
            with self.subTest(case=name):
                self.assertGreater(queued, 0)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class DeadlineAndAbandonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lex()

    def session(self):
        return ImeSession(_table=load_table(), _lex=self.lex, learning_path=None)

    def pipeline(self, **kwargs):
        sess = self.session()
        worker = ManualWorker(self.lex, PickyScorer(), delay=lambda i: 10 ** 6)
        clock = Clock()
        return ConnectionPipeline(sess, worker, grace_ms=0, clock=clock, **kwargs), worker, clock

    def test_deadline_falls_back_to_viterbi(self):
        typist = Typist().kana("きしゃ").space()
        head = list(typist.events)
        tail = Typist()
        tail.t = typist.t
        tail.kana("かいせい", gap=20).tap(VK_RETURN)
        pipe, worker, clock = self.pipeline()
        commits = ""
        for event in head:
            commits += pipe.handle(dict(event))["commit"]
        self.assertTrue(pipe.sess._await is not None)
        job = pipe.job
        for event in tail.events[:3]:  # still pending: keys queue up
            reply = pipe.handle(dict(event))
            self.assertTrue(reply["pending"])
            self.assertEqual(reply["timer"], min(reply["timer"], event["t"] + 10))
        clock.now += 0.4
        for event in tail.events[3:] + [TERMINAL]:
            reply = pipe.handle(dict(event))
            commits += reply["commit"]
        self.assertTrue(job.cancelled)
        self.assertEqual(pipe.stats.conversions["timeout"], 1)
        worker.complete_all()  # a late result changes nothing
        self.assertFalse(pipe.handle({"op": "timeout", "t": 0})["pending"])
        expected = run_reference(self.session, head + tail.events, None)
        self.assertEqual((commits, final_state(reply)), expected)
        self.assertNotEqual(expected, run_reference(self.session, head + tail.events,
                                                    PickyScorer()))

    def test_queue_overflow_falls_back(self):
        events = Typist().kana("きしゃ").space().kana("こうせいする", gap=20).tap(VK_RETURN).events
        pipe, worker, clock = self.pipeline(max_queue=3)
        commits = ""
        for event in events + [TERMINAL]:
            reply = pipe.handle(dict(event))
            commits += reply["commit"]
        self.assertEqual(pipe.stats.conversions["timeout"], 1)
        self.assertEqual((commits, final_state(reply)),
                         run_reference(self.session, events, None))

    def test_reset_toggle_and_disconnect_abandon(self):
        for op in ({"op": "reset"}, {"op": "toggle_direct"}, "close"):
            with self.subTest(op=op):
                pipe, worker, clock = self.pipeline()
                for event in Typist().kana("きしゃ").space().kana("こう", gap=20).events:
                    pipe.handle(dict(event))
                job = pipe.job
                self.assertTrue(pipe.queue)
                if op == "close":
                    pipe.close()
                    self.assertTrue(job.cancelled)
                    continue
                reply = pipe.handle(op)
                self.assertTrue(job.cancelled)
                self.assertFalse(pipe.queue)
                self.assertFalse(reply["pending"])
                self.assertEqual((reply["composition"], reply["commit"], reply["n_tokens"]),
                                 ("", "", 0))
                worker.complete_all()  # completing afterwards does nothing
                reply = pipe.handle({"op": "timeout", "t": 10 ** 6})
                self.assertEqual((reply["composition"], reply["commit"], reply["pending"]),
                                 ("", "", False))
                self.assertIsNone(pipe.sess._await)

    def test_bypass_when_worker_unavailable(self):
        pipe, worker, clock = self.pipeline()
        worker.ready = False
        events = Typist().kana("きしゃ").space().events
        for event in events:
            reply = pipe.handle(dict(event))
        self.assertFalse(reply["pending"])
        self.assertTrue(reply["converted"])
        self.assertEqual(pipe.stats.conversions["bypass"], 1)
        self.assertEqual([j for j in worker.jobs if j.kind == "convert"], [])
        self.assertEqual(final_state(reply), run_reference(self.session, events, None)[1])


class PrefetchConditionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lex()

    def run_events(self, prefetch, events):
        sess = ImeSession(_table=load_table(), _lex=self.lex, learning_path=None)
        worker = ManualWorker(self.lex, PickyScorer(), delay=lambda i: 10 ** 6)
        pipe = ConnectionPipeline(sess, worker, grace_ms=0, prefetch=prefetch,
                                  prefetch_delay_ms=30, clock=lambda: 5.0)
        submitted = []
        for event in events:
            before = len(worker.jobs)
            pipe.handle(dict(event))
            submitted.append([(j.kind, j.wanted, j.not_before) for j in worker.jobs[before:]])
        return submitted

    def test_thumb_only_in_s3(self):
        t = Typist().kana("きしゃ")
        typing = len(t.events)
        t.key("down", VK_SPACE, 150)
        submitted = self.run_events("thumb", t.events)
        self.assertEqual(sum(submitted[:typing], []), [])
        [(kind, wanted, not_before)] = submitted[typing]
        self.assertEqual((kind, wanted), ("convert", False))
        self.assertAlmostEqual(not_before, 5.03)

    def test_typing_on_every_token_change(self):
        t = Typist().kana("きしゃ")
        submitted = self.run_events("typing", t.events)
        # Each kana is complete on its key-up: one prefetch per character.
        self.assertEqual([s for s in submitted if s], [[("convert", False, 5.0)]] * 3)

    def test_off(self):
        t = Typist().kana("きしゃ").key("down", VK_SPACE, 150)
        self.assertEqual(sum(self.run_events("off", t.events), []), [])

    def test_prime_once_after_commit(self):
        t = Typist().kana("きしゃ")
        start = len(t.events)
        t.tap(VK_RETURN).kana("こう")
        submitted = self.run_events("off", t.events)
        primes = [i for i, s in enumerate(submitted) for kind, _, _ in s if kind == "prime"]
        self.assertEqual(primes, [start])  # the Enter-down that committed


def small_lex():
    lex = MozcLex({"う": [Entry("宇", "う", 1, 1, 100), Entry("卯", "う", 1, 1, 200)]},
                  {"う"}, [0] * 4, 2)
    return lex


class Recording:
    """Scorer stand-in for LmWorker tests; prime is optional."""

    def __init__(self, preferred="卯", gate: threading.Event | None = None,
                 fail: bool = False, sleep: float = 0):
        self.preferred = preferred
        self.gate = gate
        self.fail = fail
        self.sleep = sleep
        self.calls: list[str] = []

    def nll(self, context, candidates):
        self.calls.append("nll:" + context)
        if self.sleep:
            time.sleep(self.sleep)
        if self.fail and context:
            raise RuntimeError("model failure")
        return [0.0 if c == self.preferred else 10.0 for c in candidates]

    def prime(self, context):
        self.calls.append("prime:" + context)
        if self.gate is not None:
            self.gate.wait(5)


def ready_worker(scorer, **kwargs):
    worker = LmWorker(lambda: LmRescorer(scorer), small_lex(), **kwargs)
    deadline = time.monotonic() + 5
    while worker.state == "loading" and time.monotonic() < deadline:
        time.sleep(0.005)
    scorer.calls.clear()
    return worker


def u_request(context="上"):
    sess = ImeSession(_table=load_table(), _lex=small_lex(), learning_path=None)
    sess.on_key(True, 0x41, 0)
    sess.on_key(False, 0x41, 30)
    sess._lm_context = context
    return sess.conversion_request()


class LmWorkerTest(unittest.TestCase):
    def test_wanted_runs_before_prefetch(self):
        gate = threading.Event()
        scorer = Recording(gate=gate)
        worker = ready_worker(scorer)
        owner, other = object(), object()
        blocker = Job("prime", owner, None, context="blocker")
        worker.submit(blocker)
        while not scorer.calls:
            time.sleep(0.001)
        spec = Job("convert", other, u_request("先読み"))
        wanted = Job("convert", owner, u_request("変換"), wanted=True)
        worker.submit(spec)
        worker.submit(wanted)
        gate.set()
        self.assertTrue(spec.done.wait(2) and wanted.done.wait(2))
        self.assertEqual(scorer.calls, ["prime:blocker", "nll:変換", "nll:先読み"])
        self.assertEqual([d.surface for d in wanted.picks], ["卯"])

    def test_new_prefetch_replaces_old_and_not_before(self):
        scorer = Recording()
        worker = ready_worker(scorer)
        owner = object()
        old = Job("convert", owner, u_request("古"), not_before=time.monotonic() + 0.2)
        worker.submit(old)
        new = Job("convert", owner, u_request("新"), not_before=time.monotonic() + 0.05)
        worker.submit(new)
        self.assertTrue(old.done.wait(1) and old.cancelled)
        self.assertTrue(new.done.wait(2))
        time.sleep(0.25)
        self.assertEqual(scorer.calls, ["nll:新"])  # replaced before not_before: never ran
        self.assertEqual(worker.stats.prefetch, {"started": 1, "cancelled": 1})

    def test_promote_adopts_finished_prefetch(self):
        scorer = Recording()
        worker = ready_worker(scorer)
        spec = Job("convert", object(), u_request())
        worker.submit(spec)
        self.assertTrue(spec.done.wait(2))
        calls = list(scorer.calls)
        worker.promote(spec, time.monotonic() + 0.4)
        self.assertTrue(spec.wanted and spec.done.is_set())
        time.sleep(0.05)
        self.assertEqual(scorer.calls, calls)

    def test_pipeline_adopts_thumb_prefetch_without_new_lm_calls(self):
        scorer = Recording()
        worker = ready_worker(scorer)
        sess = ImeSession(_table=load_table(), _lex=small_lex(), learning_path=None)
        pipe = ConnectionPipeline(sess, worker, prefetch_delay_ms=0)
        for event in ({"op": "down", "vk": 0x41, "t": 0}, {"op": "up", "vk": 0x41, "t": 30},
                      {"op": "down", "vk": VK_SPACE, "t": 200}):
            pipe.handle(event)
        self.assertTrue(pipe.spec.done.wait(2))
        calls = list(scorer.calls)
        reply = pipe.handle({"op": "up", "vk": VK_SPACE, "t": 240})
        self.assertEqual((reply["pending"], reply["converted"], reply["composition"]),
                         (False, True, "卯"))
        self.assertEqual(scorer.calls, calls)
        self.assertEqual((worker.stats.adopted, worker.stats.conversions["inline"]), (1, 1))

    def test_three_errors_disable(self):
        scorer = Recording(fail=True)
        worker = ready_worker(scorer)
        owner = object()
        for i in range(3):
            self.assertTrue(worker.available())
            job = Job("convert", owner, u_request(f"文{i}"), wanted=True)
            worker.submit(job)
            self.assertTrue(job.done.wait(2))
            self.assertTrue(job.error)
        self.assertEqual(worker.state, "disabled")
        self.assertFalse(worker.available())

    def test_stalled_job_makes_worker_unavailable(self):
        gate = threading.Event()
        scorer = Recording(gate=gate)
        worker = ready_worker(scorer, stall_ms=50)
        job = Job("prime", object(), None, context="x")
        worker.submit(job)
        time.sleep(0.1)
        self.assertFalse(worker.available())
        gate.set()
        self.assertTrue(job.done.wait(2))
        self.assertTrue(worker.available())

    def test_cancel_owner_stops_between_runs(self):
        gate = threading.Event()
        scorer = Recording(gate=gate)
        worker = ready_worker(scorer)
        owner = object()
        worker.submit(Job("prime", owner, None, context="x"))
        while not scorer.calls:
            time.sleep(0.001)
        queued = Job("convert", owner, u_request(), wanted=True)
        worker.submit(queued)
        worker.cancel_owner(owner)
        gate.set()
        self.assertTrue(queued.done.wait(2))
        self.assertTrue(queued.cancelled)
        self.assertIsNone(queued.picks)
        time.sleep(0.05)
        self.assertEqual(scorer.calls, ["prime:x"])


if __name__ == "__main__":
    unittest.main()
