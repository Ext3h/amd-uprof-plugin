"""translate must start from a clean copy instead of appending to an earlier translation.

Run from this directory: python -m unittest test_translate_out_dir -v
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_native
from uprof_util import UprofError

SAMPLE = "sample\n"


class TranslateOutDirTest(unittest.TestCase):
    """The fake CLI appends to cpu.db when one survives, as AMDuProfCLI does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.session = root / "session"
        self.session.mkdir()
        (self.session / "session.uprof").write_bytes(b"session")
        (self.session / "cpu").mkdir()
        (self.session / "cpu" / "new.prd").write_bytes(b"new")
        self.out = root / "out"
        self.out.mkdir()
        (self.out / "unrelated.txt").write_text("keep me", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def seed_previous_translation(self):
        (self.out / "cpu.db").write_text(SAMPLE, encoding="utf-8")
        (self.out / "callstack.db").write_text(SAMPLE, encoding="utf-8")
        (self.out / "cpu").mkdir()
        (self.out / "cpu" / "old.prd").write_bytes(b"old")

    def fake_run(self, command, **_kwargs):
        with open(self.out / "cpu.db", "a", encoding="utf-8") as handle:
            handle.write(SAMPLE)
        return subprocess.CompletedProcess(command, 0, "", "")

    def translate(self, *, force):
        with mock.patch.object(uprof_native, "require_uprof",
                               return_value=Path("AMDuProfCLI")), \
                mock.patch.object(uprof_native, "run", side_effect=self.fake_run):
            uprof_native.translate(self.session, self.out, force=force)

    def test_existing_cpu_db_is_refused_without_force(self):
        self.seed_previous_translation()

        with self.assertRaisesRegex(UprofError, "--force"):
            self.translate(force=False)

        self.assertEqual((self.out / "cpu.db").read_text(encoding="utf-8"), SAMPLE)

    def test_force_replaces_instead_of_appending(self):
        self.seed_previous_translation()

        for _ in range(2):
            self.translate(force=True)
            self.assertEqual((self.out / "cpu.db").read_text(encoding="utf-8"), SAMPLE)

        self.assertFalse((self.out / "callstack.db").exists())
        self.assertFalse((self.out / "cpu" / "old.prd").exists())
        self.assertTrue((self.out / "cpu" / "new.prd").exists())
        self.assertEqual((self.out / "unrelated.txt").read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
