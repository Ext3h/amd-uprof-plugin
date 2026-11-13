"""An elevated run finds AMDuProfCLI only in an admin-writable install location.

AMDUPROF_CLI, PATH and the Program Files variables come from the caller's environment, which a
non-admin process of the same user controls. Elevated, a binary named there would run with
administrator rights, so only the install locations the OS reports are accepted.
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_native
import uprof_util

NAME = "AMDuProfCLI.exe" if os.name == "nt" else "AMDuProfCLI"


class ElevatedLookupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="uprof-lookup-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.planted_dir = self.root / "user-bin"
        self.planted_dir.mkdir()
        self.planted = self.planted_dir / NAME
        self.planted.write_bytes(b"")
        self.planted.chmod(0o755)
        self.installed = self.root / "admin" / NAME
        self.installed.parent.mkdir()
        self.installed.write_bytes(b"")
        self.installed.chmod(0o755)
        env = mock.patch.dict(
            os.environ, {"AMDUPROF_CLI": str(self.planted), "PATH": str(self.planted_dir)}
        )
        env.start()
        self.addCleanup(env.stop)

    def find(self, elevated, installed=True):
        with (
            mock.patch.object(uprof_native, "is_admin", return_value=elevated),
            mock.patch.object(
                uprof_native,
                "_admin_install_candidates",
                return_value=[self.installed] if installed else [],
            ),
        ):
            return uprof_native.find_uprof()

    def test_elevated_ignores_the_environment_variable_and_path(self):
        self.assertEqual(self.find(elevated=True), self.installed)

    def test_unelevated_still_honours_the_environment_variable(self):
        self.assertEqual(Path(self.find(elevated=False)), self.planted)

    def test_elevated_without_an_install_fails_with_a_runnable_alternative(self):
        with (
            mock.patch.object(uprof_native, "is_admin", return_value=True),
            mock.patch.object(uprof_native, "_admin_install_candidates", return_value=[]),
            self.assertRaisesRegex(uprof_util.UprofError, "AMDUPROF_CLI") as raised,
        ):
            uprof_native.require_uprof()
        message = str(raised.exception)
        self.assertIn("default location", message)
        self.assertIn("unelevated", message)
        self.assertIn(str(self.planted), message)

    def refusal(self):
        with (
            mock.patch.object(uprof_native, "is_admin", return_value=True),
            mock.patch.object(uprof_native, "_admin_install_candidates", return_value=[]),
            self.assertRaises(uprof_util.UprofError) as raised,
        ):
            uprof_native.require_uprof()
        return raised.exception

    def test_the_refusal_offers_a_runnable_unelevated_call(self):
        error = self.refusal()
        self.assertIsInstance(error, uprof_util.LinesError)
        lines = str(error).splitlines()
        alternative = next(line for line in lines if line.startswith("ALTERNATIVE: "))
        self.assertIn("collect --out-dir <NEW_OUT_DIR> --seconds <S>", alternative)
        self.assertIn("unelevated", alternative)

    def test_a_long_ignored_value_keeps_the_refusal_bounded(self):
        with mock.patch.dict(os.environ, {"AMDUPROF_CLI": "C:\\" + "y" * 3000}):
            error = self.refusal()
        self.assertLess(max(len(line) for line in str(error).splitlines()), 1500)

    def test_tools_offers_no_override_an_elevated_run_ignores(self):
        out = io.StringIO()
        with (
            mock.patch.object(uprof_native, "is_admin", return_value=True),
            mock.patch.object(uprof_native, "_admin_install_candidates", return_value=[]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                uprof_native.tool_report()
            except uprof_util.UprofError as exc:
                print(exc)
        missing = [line for line in out.getvalue().splitlines() if "uProf CLI" in line]
        self.assertTrue(missing, out.getvalue())
        for line in missing:
            self.assertNotIn("set AMDUPROF_CLI", line)

    def test_install_locations_do_not_come_from_the_environment(self):
        fake = {
            name: str(self.root) for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")
        }
        with mock.patch.dict(os.environ, fake):
            candidates = uprof_native._admin_install_candidates()
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertFalse(uprof_util.contains(self.root, Path(candidate)), candidate)


if __name__ == "__main__":
    unittest.main()
