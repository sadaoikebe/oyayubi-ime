from __future__ import annotations

from collections import defaultdict
import unittest

from yuragi.ime.bunsetsu import COST_WINDOW, CandidateStream, candidates, content
from yuragi.lattice.mozc_dict import Entry, MozcLex
from yuragi.lattice.token_viterbi import spans_from


def lexicon(entries, matrix):
    by = defaultdict(list)
    for reading, surface, lid, rid, cost in entries:
        by[reading].append(Entry(surface, reading, lid, rid, cost))
    prefixes = {r[:i] for r in by for i in range(1, len(r) + 1)}
    return MozcLex(dict(by), prefixes, [x for row in matrix for x in row], len(matrix))


class BunsetsuTest(unittest.TestCase):
    def test_exact_paths_against_exhaustive_oracle(self):
        # Same-RID alternatives, two token faces, duplicate spellings, different
        # tokenizations, expensive *real* entries, and negative connections.
        lex = lexicon([
            ('か', '蚊', 1, 2, 50), ('か', '科', 1, 2, 80),
            ('か', '科', 2, 3, 10), ('が', '画', 2, 1, -300),
            ('く', '区', 3, 2, 12000), ('ぐ', '具', 1, 3, 200),
            ('かく', '書く', 2, 3, 90), ('がく', '学', 3, 1, 500),
            ('がぐ', '外', 3, 1, 8000),
        ], [[0, 300, -50, 30], [500, 0, 70, -500],
            [-100, 600, -40, 90], [200, -300, 500, 40]])
        tokens = [{'kind': 'amb', 'faces': {'left': 'か', 'right': 'が'}},
                  {'kind': 'amb', 'faces': {'left': 'く', 'right': 'ぐ'}}]
        for left in range(4):
            for right in range(4):
                paths = []

                def walk(pos, rid, cost, surface, reading, unknown):
                    if pos == len(tokens):
                        paths.append((cost + lex.transition(rid, right), surface, reading, unknown))
                        return
                    for w in spans_from(tokens, pos, lex):
                        e = w.entry
                        walk(w.end, e.rid, cost + lex.transition(rid, e.lid) + e.cost,
                             surface + e.surface, reading + e.reading, unknown or w.unknown)

                walk(0, left, 0, '', '', False)
                known = [p for p in paths if not p[3]]
                best = min(p[0] for p in known)
                expected = {}
                for cost, surface, _, _ in known:
                    if cost <= best + COST_WINDOW:
                        expected[surface] = min(expected.get(surface, float('inf')), cost)
                actual = list(candidates(tokens, lex, left, right))
                dictionary = [c for c in actual if c.kind == 'dictionary']
                self.assertEqual({c.surface: c.score for c in dictionary}, expected)
                self.assertEqual([c.score for c in dictionary], sorted(c.score for c in dictionary))
                readings = {}
                for cost, _, reading, _ in paths:
                    readings[reading] = min(readings.get(reading, float('inf')), cost)
                kana = [c for c in actual if c.kind == 'kana']
                self.assertEqual({c.reading: c.score for c in kana}, readings)
                self.assertEqual([c.score for c in kana], sorted(c.score for c in kana))
                self.assertEqual(len(kana), 2 * len(readings))
                self.assertEqual([c.kind for c in actual],
                                 ['dictionary'] * len(dictionary) + ['kana'] * len(kana))

    def test_no_count_cap_and_lazy_pages(self):
        lex = lexicon([('か', str(i), 1, 1, i) for i in range(100)], [[0, 0], [0, 0]])
        stream = CandidateStream(candidates([{'kind': 'plain', 'kana': 'か'}], lex, 0, 0))
        self.assertTrue(stream.ensure(9))
        self.assertEqual(len(stream.cache), 10)
        self.assertFalse(stream.exhausted)
        self.assertEqual(stream.last(), 101)
        self.assertEqual([c.surface for c in stream.cache[:100]], [str(i) for i in range(100)])
        self.assertEqual([c.surface for c in stream.cache[100:]], ['か', 'カ'])

    def test_window_inclusive_and_kana_without_dictionary_prefix(self):
        lex = lexicon([('か', '基', 1, 1, 0), ('か', '境', 1, 1, COST_WINDOW),
                       ('か', '外', 1, 1, COST_WINDOW + 1)], [[0, 0], [0, 0]])
        tokens = [{'kind': 'amb', 'faces': {'left': 'か', 'right': 'が'}}]
        cs = list(candidates(tokens, lex, 0, 0))
        self.assertEqual([c.surface for c in cs], ['基', '境', 'か', 'カ', 'が', 'ガ'])
        # A dictionary cost >= UNK_COST is still a real dictionary arc.
        lex = lexicon([('か', '高', 1, 1, 15000)], [[0, 0], [0, 0]])
        self.assertEqual(next(candidates([{'kind': 'plain', 'kana': 'か'}], lex, 0, 0)).surface, '高')

    def test_content_pos_matches_e7(self):
        from yuragi.ime.bunsetsu import _pos_names
        for lid, name in _pos_names().items():
            p = name.split(',')
            expected = (p[1] not in ('接尾', '非自立') if p[0] in ('名詞', '動詞', '形容詞')
                        else p[0] in ('副詞', '連体詞', '接続詞', '感動詞', '接頭詞'))
            self.assertEqual(content(lid), expected)
