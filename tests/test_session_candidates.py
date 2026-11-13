"""A --session without cpu.db names the captures below it as runnable calls.

Small models listed the capture directory before the first profile call to find the session.
A wrong --session now answers that itself: one cause line naming the directory, then each
translated capture found below it as a complete call with the same --by and scope, so listing
first gains nothing.
Run from the repository root: python tests/run_tests.py test_session_candidates
"""
# Spec: SPEC-107

import tempfile
import unittest
from pathlib import Path

import uprof
import uprof_query
import uprof_shell
from uprof_util import UprofError


def capture(directory: Path) -> Path:
    directory.mkdir(parents=True)
    (directory / "session.uprof").write_text("{}", encoding="utf-8")
    (directory / "cpu.db").write_bytes(b"")
    return directory


class SessionCandidatesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="uprof-candidates-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def refused(self, *argv):
        args = uprof.build_parser().parse_args(argv)
        with self.assertRaises(UprofError) as caught:
            uprof_query.run_query_command(args)
        return str(caught.exception)

    def test_captures_below_the_directory_are_offered_as_runnable_calls(self):
        first = capture(self.root / "run1")
        second = capture(self.root / "nested" / "AMDuProf-SWP-Timer_Nov-13-2026_10-00-00")
        (self.root / "raw").mkdir()
        (self.root / "raw" / "session.uprof").write_text("{}", encoding="utf-8")

        message = self.refused("profile", "--session", str(self.root), "--by", "function",
                               "--pid", "100")

        self.assertNotIn("\n", message)
        self.assertIn("cpu.db not found", message)
        for found in (first, second):
            self.assertIn(f"--session {uprof_shell.quote(str(found))} --by function --pid 100",
                          message)
        self.assertNotIn(str(self.root / "raw"), message)

    def test_without_captures_below_the_translate_remedy_stays(self):
        message = self.refused("profile", "--session", str(self.root), "--by", "module")
        self.assertIn("run translate", message)
        self.assertNotIn("--by module", message)

    def test_the_list_is_bounded(self):
        for index in range(7):
            capture(self.root / f"run{index}")
        message = self.refused("profile", "--session", str(self.root), "--by", "module")
        self.assertEqual(message.count("--by module"), 5)
        self.assertIn("7 captures", message)


if __name__ == "__main__":
    unittest.main()
