"""fetch_mozc_dict keeps only files the manifest vouches for."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.wp0 import fetch_mozc_dict as fetch


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.out = root / "dict"
        self.out.mkdir()
        self.manifest = root / "manifest.json"
        self.pinned = {name: f"pinned {name}\n".encode() * 20 for name in fetch.FILES}
        self.got: list[str] = []
        self.addCleanup(patch.stopall)
        patch.object(fetch, "OUT", self.out).start()
        patch.object(fetch, "MANIFEST", self.manifest).start()
        patch.object(fetch.urllib.request, "urlretrieve", self.fake_retrieve).start()

    def fake_retrieve(self, url, dest):
        name = url.rsplit("/", 1)[1]
        self.assertTrue(url.startswith(fetch.BASE))
        self.got.append(name)
        Path(dest).write_bytes(self.pinned[name])

    def write_manifest(self):
        files = {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                 for name, data in self.pinned.items()}
        self.manifest.write_text(json.dumps({"files": files}), encoding="utf-8")

    def place_all(self):
        for name, data in self.pinned.items():
            (self.out / name).write_bytes(data)

    def test_stale_file_is_refetched_when_manifest_disagrees(self):
        self.write_manifest()
        self.place_all()
        (self.out / "evaluation.tsv").write_bytes(b"from master\n" * 50)
        fetch.main()
        self.assertEqual(self.got, ["evaluation.tsv"])
        self.assertEqual((self.out / "evaluation.tsv").read_bytes(), self.pinned["evaluation.tsv"])

    def test_missing_file_is_fetched_and_matching_ones_skipped(self):
        self.write_manifest()
        self.place_all()
        (self.out / "id.def").unlink()
        fetch.main()
        self.assertEqual(self.got, ["id.def"])

    def test_without_manifest_large_files_are_kept_as_before(self):
        self.place_all()
        (self.out / "evaluation.tsv").write_bytes(b"from master\n" * 50)
        fetch.main()
        self.assertEqual(self.got, [])


if __name__ == "__main__":
    unittest.main()
