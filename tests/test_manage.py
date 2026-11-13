"""The settings file (S1), the manage CLI (D1/D2/D3/S2/S3) and their live effect."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from oyayubi.chord.fsm import Plain
from oyayubi.ime import config, manage
from oyayubi.ime.bunsetsu import Candidate
from oyayubi.ime.learning import LearningStore, pause_marker
from oyayubi.ime.personal import PersonalLexicon, add_word
from oyayubi.ime.server import argument_parser
from oyayubi.ime.session import ImeSession
from oyayubi.lattice.mozc_dict import Entry, MozcLex

POS = 2000


def small_lex():
    return MozcLex({"かた": [Entry("肩", "かた", 1851, 1851, 3000)]},
                   {"か", "かた"}, [0] * (POS * POS), POS)


class TempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config = self.dir / "config.json"

    def write_config(self, **values):
        self.config.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")

    def run_manage(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = manage.main(["--runtime-dir", str(self.dir), *argv])
        return code, out.getvalue()


class ServerTest(TempDir):
    def test_restart_when_not_running(self):
        code, out = self.run_manage("server", "restart")
        self.assertEqual(code, 0)
        self.assertIn("not running", out)


class ConfigTest(TempDir):
    def test_absent_file_is_the_built_in_defaults(self):
        args = config.parse_args(argument_parser(), ["--runtime-dir", str(self.dir)])
        plain = argument_parser().parse_args([])
        for name in config.file_settings(argument_parser()):
            self.assertEqual(getattr(args, name), getattr(plain, name), name)
        self.assertEqual(args.config, self.config)

    def test_file_over_default_and_command_line_over_file(self):
        self.write_config(lm_deadline_ms=250, learning_file=str(self.dir / "l.jsonl"),
                          lm_prefetch="typing", no_learning=True)
        args = config.parse_args(argument_parser(), ["--runtime-dir", str(self.dir)])
        self.assertEqual((args.lm_deadline_ms, args.lm_prefetch, args.no_learning),
                         (250.0, "typing", True))
        self.assertEqual(args.learning_file, self.dir / "l.jsonl")  # converted to a Path
        args = config.parse_args(argument_parser(), [
            "--runtime-dir", str(self.dir), "--lm-deadline-ms", "100"])
        self.assertEqual(args.lm_deadline_ms, 100.0)

    def test_unknown_or_invalid_values_are_skipped(self):
        self.write_config(lm_deadlin_ms=1, lm_prefetch="always", resident=True)
        err = io.StringIO()
        with redirect_stderr(err):
            args = config.parse_args(argument_parser(), ["--runtime-dir", str(self.dir)])
        self.assertEqual((args.lm_prefetch, args.resident), ("thumb", False))
        self.assertIn("lm_deadlin_ms", err.getvalue())
        self.assertIn("always", err.getvalue())
        self.assertIn("resident", err.getvalue())

    def test_explicit_config_path(self):
        other = self.dir / "other.json"
        other.write_text('{"lm_k": 4}', encoding="utf-8")
        args = config.parse_args(argument_parser(), ["--config", str(other)])
        self.assertEqual((args.lm_k, args.config), (4, other))


class ManageTest(TempDir):
    def setUp(self):
        super().setUp()
        self.learning = self.dir / "learning.jsonl"
        self.personal = self.dir / "personal.jsonl"
        self.write_config(learning_file=str(self.learning), personal_file=str(self.personal))

    def test_words(self):
        self.assertEqual(self.run_manage("word", "add", "おやゆび", "親指")[0], 0)
        self.assertEqual(self.run_manage("word", "add", "かた", "型", "--pos", "固有名詞",
                                         "--cost", "1000")[0], 0)
        code, out = self.run_manage("word", "list")
        self.assertEqual(out.splitlines(), ["おやゆび\t親指\t名詞\t2600", "かた\t型\t固有名詞\t1000"])
        self.assertEqual(self.run_manage("word", "remove", "かた", "型")[0], 0)
        self.assertEqual(self.run_manage("word", "remove", "かた", "型")[0], 1)
        self.assertEqual(self.run_manage("word", "add", "ABC", "x")[0], 2)  # not a reading

    def learn(self):
        self.learning.touch()
        store = LearningStore(self.learning)
        store.confirm([(Candidate("ヌエ", "ぬえ", 0, 1, 1, "kana"), "")], small_lex())
        store.confirm([(Candidate("肩", "かた", 0, 1851, 1851, word_cost=3000), "")],
                      small_lex())

    def test_learning_list_promote_clear(self):
        self.assertEqual(self.run_manage("learning", "compact")[0], 1)  # learning off
        self.learn()
        out = self.run_manage("learning", "list")[1].splitlines()
        self.assertEqual([line.split("\t")[:2] for line in out], [["かた", "肩"], ["ぬえ", "ヌエ"]])
        self.assertTrue(out[1].endswith("\tword"))
        self.assertEqual(self.run_manage("learning", "list", "ぬ")[1].count("\n"), 1)
        self.assertEqual(self.run_manage("learning", "promote", "ぬえ", "鵺")[0], 1)
        self.assertEqual(self.run_manage("learning", "promote", "ぬえ", "ヌエ")[0], 0)
        self.assertEqual(self.run_manage("word", "list")[1], "ぬえ\tヌエ\t名詞\t2600\n")
        self.assertEqual(self.run_manage("learning", "clear", "--with-words")[0], 0)
        self.assertEqual(self.run_manage("learning", "list")[1], "")
        self.assertTrue(self.learning.exists())  # cleared, still on

    def test_learning_forget(self):
        self.learn()
        self.assertEqual(self.run_manage("learning", "forget", "かた", "肩")[0], 0)
        self.assertEqual(self.run_manage("learning", "forget", "かた", "肩")[0], 1)

    def test_pause_and_resume(self):
        self.assertEqual(self.run_manage("learning", "pause")[0], 1)  # off: nothing to pause
        self.learn()
        self.assertEqual(self.run_manage("learning", "pause"), (0, "learning paused\n"))
        self.assertTrue(pause_marker(self.learning).exists())
        self.assertEqual(self.run_manage("learning", "resume"), (0, "learning on\n"))
        self.assertFalse(pause_marker(self.learning).exists())

    def test_status_and_config_show(self):
        self.learn()
        self.run_manage("word", "add", "おやゆび", "親指")
        self.run_manage("learning", "pause")
        status = json.loads(self.run_manage("status")[1])
        self.assertEqual(status["learning"], {"file": str(self.learning), "state": "paused",
                                              "pairs": 2, "learned_words": 1})
        self.assertEqual(status["personal_lexicon"]["words"], 1)
        self.assertEqual(status["resident_server"], "not running")
        out = self.run_manage("config", "show")[1]
        self.assertIn(f"learning_file = {json.dumps(str(self.learning), ensure_ascii=False)}\t[file]", out)
        self.assertIn("lm_deadline_ms = 400\t[default]", out)
        self.assertIn("tip.json toggle_vk", out)

    def test_config_set_and_unset(self):
        self.assertEqual(self.run_manage("config", "set", "lm_k", "4")[0], 0)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["lm_k"], 4)
        self.assertIn("lm_k = 4\t[file]", self.run_manage("config", "show")[1])
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(self.run_manage("config", "set", "lm_k", "four")[0], 2)
        self.assertEqual(self.run_manage("config", "unset", "lm_k")[0], 0)
        self.assertNotIn("lm_k", json.loads(self.config.read_text(encoding="utf-8")))
        self.assertEqual(self.run_manage("config", "unset", "lm_k")[0], 1)

    def test_dict_manifest_round_trip(self):
        manifest = self.dir / "manifest.json"
        self.assertEqual(self.run_manage("dict", "write", "--label", "t",
                                         "--manifest", str(manifest))[0], 0)
        code, out = self.run_manage("dict", "verify", "--manifest", str(manifest))
        self.assertEqual((code, out.splitlines()[-1]), (0, "t: OK"))
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["files"]["suffix.txt"]["sha256"] = "0" * 64
        manifest.write_text(json.dumps(data), encoding="utf-8")
        code, out = self.run_manage("dict", "verify", "--manifest", str(manifest))
        self.assertEqual((code, out.splitlines()), (1, ["changed suffix.txt", "t: 1 problem(s)"]))

    def test_text_hash_ignores_crlf_checkout(self):
        from oyayubi.lattice.dict_manifest import describe_text
        lf, crlf = self.dir / "lf.jsonl", self.dir / "crlf.jsonl"
        lf.write_bytes(b'{"a": 1}\n{"b": 2}\n')
        crlf.write_bytes(b'{"a": 1}\r\n{"b": 2}\r\n')
        self.assertEqual(describe_text(lf), describe_text(crlf))


class LiveEffectTest(TempDir):
    def session(self, learning_path=None):
        s = ImeSession(_lex=small_lex(), learning_path=None,
                       _learning=LearningStore(learning_path) if learning_path else None,
                       personal=PersonalLexicon(self.dir / "personal.jsonl"))
        s._table = {"keys": {c: {"plain": c} for c in "かた"}}
        return s

    def convert(self, s, reading):
        s.tokens = [Plain(c) for c in reading]
        s._refresh()
        s.converted = True
        s._open_cands()

    def test_registered_word_appears_without_restart(self):
        s = self.session()
        self.convert(s, "かた")
        self.assertNotIn("型", s.candidates)
        add_word(self.dir / "personal.jsonl", "かた", "型", cost=1000)
        self.convert(s, "かた")
        self.assertEqual(s.composition, "型")
        self.assertIn("肩", s.candidates)

    def test_resident_connections_share_the_word_list(self):
        from oyayubi.ime.resident import SessionFactory
        loaded = self.session()
        self.assertIs(SessionFactory(loaded)().personal, loaded.personal)

    def test_paused_learning_records_nothing(self):
        path = self.dir / "learning.jsonl"
        path.touch()
        s = self.session(path)
        pause_marker(path).touch()
        self.convert(s, "かた")
        self.assertEqual(s._commit(), "肩")
        self.assertEqual(path.read_text(encoding="utf-8"), "")
        pause_marker(path).unlink()
        self.convert(s, "かた")
        s._commit()
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
