"""The dump cache belongs to one capture's raw content, not to its path or file times.

Run from the repository root: python tests/run_tests.py test_dump_identity
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import uprof_dump
import uprof_util
from uprof_session import Sampler
from uprof_util import DUMP_FILENAMES, UprofError

KIND = "ibsop-event-dump"
DAY = 24 * 60 * 60


class DumpIdentityTest(unittest.TestCase):
    """A fake AMDuProfCLI writes a dump holding the raw .prd content it translated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / "run"
        self.translations = 0
        patches = [
            mock.patch.object(uprof_dump, "cache_dir", return_value=self.root / "cache" / "k"),
            mock.patch.object(uprof_dump, "require_uprof", return_value=Path("AMDuProfCLI")),
            mock.patch.object(uprof_dump, "translate_command",
                              side_effect=lambda _uprof, out, **_kw: ["AMDuProfCLI", str(out)]),
            mock.patch.object(uprof_dump, "run", side_effect=self.fake_run),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_run(self, command, **_kwargs):
        self.translations += 1
        out = Path(command[1])
        content = b"".join(path.read_bytes() for path in sorted((out / "cpu").glob("*.prd")))
        (out / DUMP_FILENAMES[KIND]).write_bytes(content)
        return subprocess.CompletedProcess(command, 0, "", "")

    def make_capture(self, directory, raw, age_days=0):
        (directory / "cpu").mkdir(parents=True)
        (directory / "session.uprof").write_text("{}", encoding="utf-8")
        prd = directory / "cpu" / "capture.prd"
        prd.write_bytes(raw)
        stamp = time.time() - age_days * DAY
        os.utime(prd, (stamp, stamp))

    def resolve(self):
        with contextlib.redirect_stderr(io.StringIO()):
            info = uprof_dump.resolve_dump(self.session, Sampler(ibs_dump_kind=KIND))
        return info.source, info.path.read_bytes()

    def test_an_older_capture_copied_to_the_same_path_regenerates_the_dump(self):
        self.make_capture(self.session, b"capture A")
        self.assertEqual(self.resolve(), ("generated", b"capture A"))

        earlier = self.root / "earlier"
        self.make_capture(earlier, b"capture B", age_days=2)
        shutil.rmtree(self.session)
        shutil.copytree(earlier, self.session)

        self.assertEqual(self.resolve(), ("generated", b"capture B"))

    def test_the_same_capture_reuses_the_dump(self):
        self.make_capture(self.session, b"capture A")
        self.resolve()

        with mock.patch.object(uprof_util, "sha256_file", side_effect=AssertionError("hashed")):
            self.assertEqual(self.resolve(), ("reused", b"capture A"))
        self.assertEqual(self.translations, 1)

    def test_a_copy_of_the_same_capture_reuses_the_dump(self):
        self.make_capture(self.session, b"capture A")
        self.resolve()
        copy = self.root / "copy"
        shutil.copytree(self.session, copy)
        shutil.rmtree(self.session)
        copy.rename(self.session)
        later = time.time() + DAY
        os.utime(self.session / "cpu" / "capture.prd", (later, later))

        self.assertEqual(self.resolve(), ("reused", b"capture A"))
        self.assertEqual(self.translations, 1)
        with mock.patch.object(uprof_util, "sha256_file", side_effect=AssertionError("hashed")):
            self.assertEqual(self.resolve(), ("reused", b"capture A"))

    def test_a_cache_without_a_recorded_capture_regenerates(self):
        self.make_capture(self.session, b"capture A")
        self.resolve()
        meta_path = self.root / "cache" / "k" / "cache.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        del meta["capture"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        self.assertEqual(self.resolve()[0], "generated")
        self.assertEqual(self.translations, 2)

    def test_a_session_without_raw_files_never_reuses(self):
        self.make_capture(self.session, b"capture A")
        (self.session / "cpu" / "capture.prd").unlink()
        cache = self.root / "cache" / "k"
        cache.mkdir(parents=True)
        (cache / DUMP_FILENAMES[KIND]).write_bytes(b"other")
        (cache / "cache.json").write_text(json.dumps(
            {"kind": KIND, "timeFilter": "", "capture": []}), encoding="utf-8")

        # Not reused: generation runs, and the fake translates no raw file into an empty dump.
        with self.assertRaisesRegex(UprofError, "empty"):
            self.resolve()
        self.assertEqual(self.translations, 1)


if __name__ == "__main__":
    unittest.main()
