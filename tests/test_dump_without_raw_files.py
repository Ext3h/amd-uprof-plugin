"""A session without raw profile files and without an IBS dump fails immediately.

Run from the repository root: python tests/run_tests.py test_dump_without_raw_files
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_dump
import uprof_native
from uprof_session import Sampler
from uprof_util import UprofError

KIND = "ibsop-event-dump"


class DumpWithoutRawFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        (self.session / "session.uprof").write_text("{}", encoding="utf-8")
        (self.session / "cpu.db").write_bytes(b"")
        self.translations = 0
        patches = [
            mock.patch.object(uprof_dump, "cache_dir", return_value=self.root / "cache" / "k"),
            mock.patch.object(uprof_dump, "require_uprof", return_value=Path("AMDuProfCLI")),
            mock.patch.object(
                uprof_dump,
                "translate_command",
                side_effect=lambda _uprof, out, **_kw: ["AMDuProfCLI", str(out)],
            ),
            mock.patch.object(uprof_dump, "run", side_effect=self.fake_run),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_run(self, command, **_kwargs):
        self.translations += 1
        return mock.Mock(returncode=0, stdout="", stderr="")

    def test_session_without_raw_files_fails_before_generating_dump(self):
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            with self.assertRaises(UprofError) as caught:
                uprof_dump.resolve_dump(self.session, Sampler(ibs_dump_kind=KIND))

        self.assertEqual(self.translations, 0)
        self.assertNotIn("GENERATING DUMP:", stderr.getvalue())

        lines = str(caught.exception).splitlines()
        self.assertEqual(
            lines[0],
            "this session has no raw profile files and no IBS dump; the dump can't be generated here",
        )
        self.assertIn("ALTERNATIVE: dump beside session: python ", lines[1])
        self.assertIn(" profile --session ", lines[1])
        self.assertIn(" --by process", lines[1])
        self.assertIn("ALTERNATIVE: original session: python ", lines[2])
        self.assertIn(" profile --session <ORIGINAL_SESSION_DIR> --by process", lines[2])

    def test_session_with_unreadable_raw_files_fails_before_generating_dump(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(uprof_dump, "capture_identity", return_value=None),
            mock.patch("sys.stderr", stderr),
            self.assertRaises(UprofError) as caught,
        ):
            uprof_dump.resolve_dump(self.session, Sampler(ibs_dump_kind=KIND))

        self.assertEqual(self.translations, 0)
        self.assertNotIn("GENERATING DUMP:", stderr.getvalue())

        lines = str(caught.exception).splitlines()
        self.assertEqual(
            lines[0],
            "the session's raw cpu/*.prd cannot be read; the dump can't be generated here",
        )
        self.assertIn("ALTERNATIVE: dump beside session: python ", lines[1])
        self.assertIn("ALTERNATIVE: original session: python ", lines[2])

    def test_session_with_dump_beside_it_succeeds_without_raw_files(self):
        (self.session / "IbsOpDump.csv").write_text("dummy dump", encoding="utf-8")
        info = uprof_dump.resolve_dump(self.session, Sampler(ibs_dump_kind=KIND))
        self.assertEqual(info.source, "beside")
        self.assertEqual(self.translations, 0)


if __name__ == "__main__":
    unittest.main()
