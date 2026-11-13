"""Printed uprof.py calls start with a bare `python`.

No absolute interpreter path runs as the first token in all of Git Bash, PowerShell 5.1 and
cmd.exe (unquoted backslashes are lost in bash, an unquoted forward slash is a switch in cmd.exe,
a quoted first token needs `&` in PowerShell), so every NEXT, candidate, failure-alternative and
recapture command starts with `python` followed by the quoted uprof.py path. That cmd.exe also
looks a bare `python` up in the current directory is an accepted owner decision.
Run from the repository root: python tests/run_tests.py test_printed_python
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof
import uprof_native
import uprof_query
import uprof_shell
from uprof_util import UprofError

SESSION = "C:/caps/run" if os.name == "nt" else "/caps/run"
TOOL = uprof_shell.quote(str(uprof_query.TOOLS_DIR / "uprof.py"))


class BarePythonTest(unittest.TestCase):
    def test_next_calls_start_with_bare_python(self):
        args = uprof.build_parser().parse_args(
            [
                "profile",
                "--session",
                SESSION,
                "--by",
                "line",
                "--function",
                "f",
                "--module",
                "m.dll",
            ]
        )
        data = {}
        calls = uprof_query._next_calls(args, {}, data=data)
        self.assertTrue(calls)
        for call in calls:
            self.assertTrue(call.startswith(f"python {TOOL} "), call)
        self.assertNotIn("PYTHON", data)

    def test_candidate_calls_start_with_bare_python(self):
        with tempfile.TemporaryDirectory(prefix="uprof-python-") as temp:
            capture = Path(temp) / "run1"
            capture.mkdir()
            (capture / "session.uprof").write_text("{}", encoding="utf-8")
            (capture / "cpu.db").write_bytes(b"")
            args = uprof.build_parser().parse_args(["profile", "--session", temp, "--by", "module"])
            with self.assertRaises(UprofError) as caught:
                uprof_query.run_query_command(args)
        self.assertIn(f"run one of: python {TOOL} ", str(caught.exception))

    def test_failure_alternatives_and_recapture_start_with_bare_python(self):
        with mock.patch.object(uprof_native, "find_uprof", return_value=None):
            message = str(uprof_native.failure("cause", ["whole capture"], session=SESSION))
        whole = next(
            line for line in message.splitlines() if line.startswith("ALTERNATIVE: whole capture: ")
        )
        self.assertTrue(whole.split(": ", 2)[2].startswith(f"python {TOOL} "), whole)
        cycles, ibs, _ = uprof_native.recapture_commands(Path(SESSION))
        for command in (cycles, ibs):
            self.assertTrue(command.startswith(f"python {TOOL} "), command)


if __name__ == "__main__":
    unittest.main()
