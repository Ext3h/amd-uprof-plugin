"""A failed AMDuProfCLI translate names its cause from the CLI log, not only the exit code.

AMDuProfCLI 5.3 crashes with an access violation (0xC0000005) on a session with no translatable
sample category; stdout carries only "Translation started" and the cause is only in
<temp>/<user>-AMDuProfCLI.log. Both translate paths (the translate command and the dump cache)
read what the run appended to that log and stop with one cause line and runnable alternatives.
Run from the repository root: python tests/run_tests.py test_translate_failure
"""

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_dump
import uprof_native
from uprof_session import Sampler
from uprof_util import UprofError

ACCESS_VIOLATION = -1073741819
NO_CATEGORY_LOG = (
    "[PROFILE_COMMON][AMDTProfileSession::ReadSessionFileContents:815][TID: 3248][19:48:09]: "
    "CALLSTACK SAMPLING category database does not exist...\n"
    "[PROFILE_COMMON][AMDTProfileTranslationManager::Execute:384][TID: 1996][19:48:09]: "
    "No categories to translate...\n"
    "[PROFILE_COMMON][AMDTProfileManager::Translate:1199][TID: 1996][19:48:09]: "
    "Translation error encountered...\n")
OTHER_LOG = ("[PROFILE_COMMON][AMDTProfileManager::Translate:1199][TID: 1][19:48:09]: "
             "Translation error encountered...\n")


class TranslateFailureTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="uprof-translate-failure-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.session = self.root / "session"
        (self.session / "cpu").mkdir(parents=True)
        (self.session / "session.uprof").write_text("{}", encoding="utf-8")
        (self.session / "cpu" / "capture.prd").write_bytes(b"raw")
        self.log = Path(tempfile.gettempdir()) / "tester-AMDuProfCLI.log"
        self.addCleanup(lambda: self.log.unlink(missing_ok=True))

    def crashing_run(self, appended):
        def fake_run(command, **_kwargs):
            with open(self.log, "a", encoding="utf-8") as handle:
                handle.write(appended)
            return subprocess.CompletedProcess(command, ACCESS_VIOLATION, "Translation started\n", "")
        return fake_run

    def translate(self, appended):
        with mock.patch.object(uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")), \
                mock.patch.object(uprof_native, "run", side_effect=self.crashing_run(appended)), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(UprofError) as caught:
            uprof_native.translate(self.session, self.root / "out")
        return str(caught.exception)

    def test_no_translatable_category_is_named_with_alternatives(self):
        message = self.translate(NO_CATEGORY_LOG)

        self.assertNotIn("\n", message)
        for part in ("no translatable sample category", "0xC0000005"):
            self.assertIn(part, message)
        # Both alternatives are complete commands: no placeholder, an absolute script path and
        # an output directory derived from the session.
        self.assertNotIn("<", message)
        self.assertNotIn(">", message)
        script = Path(uprof_native.__file__).resolve().with_name("uprof.py")
        out_dir = f"{self.session}-recapture"
        cycles = f'python "{script}" collect --out-dir "{out_dir}" --seconds 60'
        self.assertIn(cycles, message)
        self.assertIn(cycles + " --event ibs-op", message)

    def test_an_earlier_run_in_the_log_is_not_blamed(self):
        self.log.write_text(NO_CATEGORY_LOG, encoding="utf-8")

        message = self.translate(OTHER_LOG)

        self.assertNotIn("no translatable sample category", message)
        self.assertIn("access violation (0xC0000005)", message)
        self.assertIn(str(self.log), message)

    def test_the_dump_cache_names_the_same_cause(self):
        with mock.patch.object(uprof_dump, "cache_dir", return_value=self.root / "cache" / "k"), \
                mock.patch.object(uprof_dump, "require_uprof", return_value=Path("AMDuProfCLI")), \
                mock.patch.object(uprof_dump, "translate_command",
                                  side_effect=lambda _uprof, out, **_kw: ["AMDuProfCLI", str(out)]), \
                mock.patch.object(uprof_dump, "run",
                                  side_effect=self.crashing_run(NO_CATEGORY_LOG)), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(UprofError) as caught:
            uprof_dump.resolve_dump(self.session, Sampler(ibs_dump_kind="ibsop-event-dump"))

        self.assertIn("no translatable sample category", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
