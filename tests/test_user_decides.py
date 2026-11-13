"""System security changes and elevation stay the user's decision.

A remedy that turns off Hyper-V, VBS or Memory integrity, or pipes a downloaded script into a
root shell, is not something an agent should run on its own; the tool says to ask the user. Only
collect needs elevation, so an elevated translate or profile says so.
"""

import contextlib
import io
import unittest
from unittest import mock

import uprof
import uprof_native

ELEVATED = "WARNING: running elevated"


class RemedyTest(unittest.TestCase):
    def test_the_vbs_remedy_leaves_the_change_to_the_user(self):
        self.assertIn("ask the user", uprof_native._WINDOWS_IBS_REMEDY)

    def test_the_linux_llvm_remedy_runs_no_downloaded_script_as_root(self):
        self.assertIn("ask the user", uprof_native.LINUX_LLVM_INSTALL)
        self.assertNotIn("sudo bash", uprof_native.LINUX_LLVM_INSTALL)
        self.assertNotIn("wget", uprof_native.LINUX_LLVM_INSTALL)


class ElevatedAnalysisTest(unittest.TestCase):
    def stderr(self, argv, elevated):
        err = io.StringIO()
        with (
            mock.patch.object(uprof_native, "is_admin", return_value=elevated),
            mock.patch.object(uprof, "_dispatch"),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            uprof.main(argv)
        return err.getvalue()

    def test_elevated_translate_and_profile_say_only_collect_needs_it(self):
        for argv in (
            ["translate", "--session", "s", "--out-dir", "o"],
            ["profile", "--session", "s", "--by", "process"],
        ):
            with self.subTest(command=argv[0]):
                text = self.stderr(argv, elevated=True)
                self.assertIn(ELEVATED, text)
                self.assertIn("only collect needs elevation", text)

    def test_unelevated_runs_and_collect_print_no_elevation_warning(self):
        self.assertNotIn(
            ELEVATED, self.stderr(["profile", "--session", "s", "--by", "process"], elevated=False)
        )
        self.assertNotIn(
            ELEVATED, self.stderr(["collect", "--out-dir", "o", "--dry-run"], elevated=True)
        )


if __name__ == "__main__":
    unittest.main()
