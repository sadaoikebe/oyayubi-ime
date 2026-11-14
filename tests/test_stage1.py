"""Stage 1 has no Win32, dictionary, or learning dependencies on Linux."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from oyayubi.chord.fsm import AmbShift, Plain, ThumbTap
from oyayubi.ime.recording import Prompt, Recorder, VK_NEXT_PROMPT, load_prompts
from tools.replay_stage1 import (
    Native, Trace, evaluate_log, evaluate_trace, expected_tokens, read_log, replay,
)

FIXTURE = Path(__file__).parent / "fixtures/stage1_synthetic.jsonl"


def trace(reading, events):
    return Trace({"id": "test", "text": reading, "reading": reading},
                 [{"ms": ms, "vk": vk, "action": action}
                  for ms, vk, action in events], "complete",
                 max((e[0] for e in events), default=0))


class RecordingTest(unittest.TestCase):
    def recorder(self, count=2):
        stream = io.StringIO()
        recorder = Recorder(stream, [Prompt(str(i), "と。", "と。") for i in range(count)],
                            qpc_frequency=10000000, qpc_origin=123456789)
        return recorder, stream

    def test_append_only_edits_and_raw_repeats(self):
        rec, stream = self.recorder()
        events = [(74, True, 1), (74, True, 2), (74, False, 3),
                  (8, True, 4), (8, False, 5), (27, True, 6), (27, False, 7),
                  (13, True, 8), (13, False, 9)]
        before = stream.getvalue()
        for vk, down, ms in events:
            rec.on_key(vk, down, ms)
        self.assertTrue(stream.getvalue().startswith(before))
        self.assertEqual([(e['vk'], e['action'], e['ms']) for e in rec.events],
                         [(vk, 'down' if down else 'up', ms) for vk, down, ms in events])
        self.assertEqual(rec.index, 0)
        self.assertEqual(rec.prompt.reading, "と。")
        self.assertEqual(len([r for r in stream.getvalue().splitlines()
                              if json.loads(r)['type'] == 'prompt_start']), 1)

    def test_f9_requires_all_keys_released_and_repeat_does_not_skip(self):
        rec, stream = self.recorder()
        rec.on_key(32, True, 1)
        rec.on_key(VK_NEXT_PROMPT, True, 2)
        rec.on_key(VK_NEXT_PROMPT, True, 3)
        rec.on_key(VK_NEXT_PROMPT, False, 4)
        self.assertEqual(rec.index, 0)
        rec.on_key(32, False, 5)
        self.assertEqual(rec.index, 0)  # no surprise delayed advance
        rec.on_key(VK_NEXT_PROMPT, True, 6)
        rec.on_key(VK_NEXT_PROMPT, False, 7)
        rec.on_key(VK_NEXT_PROMPT, False, 8)  # unmatched release
        self.assertEqual(rec.index, 1)
        rec.on_key(VK_NEXT_PROMPT, True, 9)
        rec.on_key(VK_NEXT_PROMPT, False, 10)
        self.assertTrue(rec.closed)
        rows = [json.loads(s) for s in stream.getvalue().splitlines()]
        self.assertEqual([r['status'] for r in rows if r['type'] == 'prompt_end'],
                         ['complete', 'complete'])
        self.assertEqual(rows[-1]['reason'], 'complete')

    def test_focus_loss_never_restarts_prompt_and_close_marks_partial(self):
        rec, stream = self.recorder(3)
        rec.on_key(74, True, 1)
        rec.on_focus(False, 2)
        rec.on_key(75, True, 3)  # not in a focused trial
        rec.on_focus(True, 4)
        self.assertEqual(rec.prompt.id, '1')
        self.assertFalse(rec.down)
        rec.close(5)
        rec.close(6)
        rows = [json.loads(s) for s in stream.getvalue().splitlines()]
        ends = [r for r in rows if r['type'] == 'prompt_end']
        self.assertEqual([r['status'] for r in ends], ['interrupted', 'incomplete'])
        self.assertEqual(ends[0]['held_vks'], [74])
        self.assertEqual(sum(r['type'] == 'session_end' for r in rows), 1)

    def test_timestamp_and_vk_validation(self):
        rec, _ = self.recorder()
        rec.on_key(74, True, 10)
        for vk, down, ms in [(74, False, 9), (256, True, 11),
                             (True, True, 11), (74, 1, 11), (74, False, 10.5)]:
            with self.subTest(vk=vk, down=down, ms=ms), self.assertRaises(ValueError):
                rec.on_key(vk, down, ms)

    def test_prompts_are_40_encodable_daily_sentences(self):
        prompts = load_prompts()
        self.assertEqual(len(prompts), 40)
        for p in prompts:
            with self.subTest(id=p.id):
                self.assertTrue(expected_tokens(p.reading))
                self.assertEqual(p.reading.count('。') + p.reading.count('？'), 1)
        self.assertTrue(any(any(c.isdigit() for c in p.reading) for p in prompts))
        self.assertTrue(any('、' in p.reading for p in prompts))


class ReplayTest(unittest.TestCase):
    def test_expected_tokens_include_punctuation_digits_and_spelled_particles(self):
        self.assertEqual(expected_tokens('はをへ、おの？2。'),
                         [Plain('h'), AmbShift('a'), Plain('b'), Plain('p'),
                          AmbShift('j'), AmbShift('k'), AmbShift('/'), Native('2'), Plain('q')])
        with self.assertRaises(ValueError):
            expected_tokens('漢字')

    def test_frozen_sample_accuracy_and_all_failure_categories(self):
        result = evaluate_log(FIXTURE)
        self.assertEqual(result['completed_prompts'], 8)
        self.assertTrue(result['session_closed'])
        s = result['summary']
        # The overlap prompt (の held in the press of お, released inside it)
        # is a chord since the S1-S7 FSM (album design 2 §3.2): no error.
        self.assertEqual((s['expected_count'], s['actual_count'], s['matches']), (16, 16, 12))
        self.assertEqual((s['substitutions'], s['deletions'], s['insertions']), (3, 1, 1))
        self.assertEqual(s['edit_distance'], 5)
        self.assertEqual(s['token_accuracy'], 0.6875)
        self.assertEqual(s['failures'], {'thumb_overlap_shift_loss': 0, 'unintended_chord': 1,
                                       'missed_chord': 1, 'other': 3})
        clean, overlap = result['prompts'][:2]
        self.assertEqual(clean['token_accuracy'], 1)
        self.assertEqual(overlap['errors'], [])
        self.assertEqual(overlap['raw_tokens'][1], {
            'kind': 'AmbShift', 'key': 'k', 'ms': 2170, 'source_event': 3,
            'press': 1, 'turn': 2, 'held': True})
        self.assertEqual(s['controls']['backspace_downs'], 1)
        self.assertEqual(s['controls']['thumb_taps'], 2)
        self.assertEqual(s['controls']['next_prompt_downs'], 8)
        self.assertEqual(result['prompts'][5]['token_accuracy'], 1)  # BS didn't erase と

    def test_timeout_at_80_is_fired_before_same_time_key(self):
        for ms, expected in [(79, [AmbShift('j')]),
                             (80, [ThumbTap(), Plain('j')]),
                             (81, [ThumbTap(), Plain('j')])]:
            t = trace('お', [(0, 32, 'down'), (ms, 74, 'down'),
                            (ms+30, 74, 'up'), (ms+40, 32, 'up')])
            with self.subTest(ms=ms):
                got, _ = replay(t)
                self.assertEqual([o.token for o in got], expected)
        # A lone pending key is drained at prompt end without losing a token.
        got, _ = replay(trace('と', [(0, 74, 'down')]))
        self.assertEqual([o.token for o in got], [Plain('j')])

    def test_overlap_threshold_at_20_and_repeat_suppression(self):
        for duration, expected in [(19, [Plain('h'), ThumbTap()]), (20, [AmbShift('h')])]:
            t = trace('は', [(0, 72, 'down'), (60, 32, 'down'),
                            (60+duration, 72, 'up'), (110, 32, 'up')])
            with self.subTest(duration=duration):
                got, _ = replay(t)
                self.assertEqual([o.token for o in got], expected)
        t = trace('と', [(0, 74, 'down'), (20, 74, 'down'), (30, 74, 'up')])
        got, controls = replay(t)
        self.assertEqual([o.token for o in got], [Plain('j')])
        self.assertEqual(controls['repeat_downs'], 1)

    def test_overlap_inference_requires_consumed_held_space(self):
        # Space was held past its timeout, but never consumed by an earlier
        # chord: this is a missed chord, not evidence of fused thumb presses.
        t = trace('お', [(0, 32, 'down'), (100, 74, 'down'),
                        (130, 74, 'up'), (150, 32, 'up')])
        result = evaluate_trace(t)
        self.assertEqual(result['failures']['missed_chord'], 1)
        self.assertEqual(result['failures']['thumb_overlap_shift_loss'], 0)
        # A second character can settle the first chord while Space is held;
        # released inside the press it is a chord too (album design 2 §3.2).
        t = trace('おの', [(0, 32, 'down'), (10, 74, 'down'), (40, 75, 'down'),
                          (50, 74, 'up'), (60, 75, 'up'), (70, 32, 'up')])
        result = evaluate_trace(t)
        self.assertEqual(result['edit_distance'], 0)
        # Held only 15 ms with Space (< V), it goes back to pending and is plain.
        t = trace('おの', [(0, 32, 'down'), (10, 74, 'down'), (100, 74, 'up'),
                          (110, 75, 'down'), (125, 32, 'up'), (160, 75, 'up')])
        result = evaluate_trace(t)
        self.assertEqual(result['failures']['thumb_overlap_shift_loss'], 1)

    def test_insertion_is_penalized_and_counts_do_not_go_negative(self):
        t = trace('と', [(0, 74, 'down'), (30, 74, 'up'), (100, 75, 'down'),
                        (130, 75, 'up'), (200, 72, 'down'), (230, 72, 'up')])
        result = evaluate_trace(t)
        self.assertEqual(result['matches'], 1)
        self.assertEqual(result['insertions'], 2)
        self.assertEqual(result['token_accuracy'], 0)

    def test_raw_native_digit_finishes_pending_before_literal(self):
        result = evaluate_trace(trace('と2', [(0, 74, 'down'), (20, 50, 'down'),
                                             (30, 74, 'up'), (40, 50, 'up')]))
        self.assertEqual(result['token_accuracy'], 1)
        self.assertEqual([t['kind'] for t in result['raw_tokens']], ['Plain', 'literal'])

    def test_incomplete_and_interrupted_trials_are_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'log.jsonl'
            stream = io.StringIO()
            rec = Recorder(stream, [Prompt(str(i), 'と', 'と') for i in range(3)],
                           qpc_frequency=1000, qpc_origin=0)
            rec.on_key(74, True, 1)
            rec.on_key(74, False, 2)
            rec.on_key(VK_NEXT_PROMPT, True, 3)
            rec.on_key(VK_NEXT_PROMPT, False, 4)
            rec.on_focus(False, 5)
            rec.on_focus(True, 6)
            rec.on_key(74, True, 7)
            # Simulated process crash: no close, third prompt remains open.
            path.write_text(stream.getvalue(), encoding='utf-8')
            result = evaluate_log(path)
            self.assertEqual(result['completed_prompts'], 1)
            self.assertFalse(result['session_closed'])
            self.assertEqual(result['summary']['token_accuracy'], 1)
            self.assertEqual(result['excluded_prompts'],
                             [{'id': '1', 'status': 'interrupted'},
                              {'id': '2', 'status': 'incomplete'}])

    def test_malformed_log_rejected_with_line_number(self):
        rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
        mutations = [(2, 'ms', -1), (2, 'action', 'press'), (2, 'vk', 256),
                     (2, 'id', 'wrong'), (0, 'schema', 'unknown'),
                     (26, 'event_count', 999)]
        # Find prompt_end rather than depending on its fixture position.
        mutations[-1] = (next(i for i, row in enumerate(rows) if row['type'] == 'prompt_end'),
                         'event_count', 999)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.jsonl'
            for index, key, value in mutations:
                changed = [dict(row) for row in rows]
                changed[index][key] = value
                path.write_text(''.join(json.dumps(r)+'\n' for r in changed))
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, f':{index+1}:'):
                    read_log(path)

    def test_cli_json_report_on_linux(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            proc = subprocess.run([sys.executable, '-m', 'tools.replay_stage1', str(FIXTURE),
                                   '--json', '--report', str(report)],
                                  capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(proc.stdout), json.loads(report.read_text()))
            self.assertEqual(json.loads(proc.stdout)['summary']['token_accuracy'], 0.6875)


if __name__ == '__main__':
    unittest.main()
