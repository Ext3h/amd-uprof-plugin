"""Names from capture data or from disk never reach a runnable command with live shell syntax.

A process image name (the system-wide ranking's NEXT when the top row has no PID) and the names
of capture directories found below a wrong --session come from the capture or an extracted
archive, not from the caller. Pasted into Git Bash, PowerShell 5.1 or cmd.exe, a `$(...)`,
backtick, `%VAR%`, `!`, quote or `&` in such a name must not run or expand. The process name
enters --image as an ILIKE pattern with each unsafe character replaced by the one-character
wildcard; a directory that cannot be written safely is named as data, not as a command.
Run from the repository root: python tests/run_tests.py test_hostile_names
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

import uprof
import uprof_query
from uprof_util import UprofError

SESSION = "C:/captures/run" if os.name == "nt" else "/captures/run"
HOSTILE = ["x$(touch pwned).exe", "a`id`.exe", "%PATH%.exe", "a&b.exe", 'a"b.exe', "a'b.exe",
           "a!b.exe", "a;b|c.exe", "$HOME.exe"]
# Characters that expand, run or end the quoting inside "..." in at least one of the shells.
LIVE = set("$`\"%!")


def image_argument(call):
    match = re.search(r'--image ("[^"]*"|\S+)', call)
    return match.group(1).strip('"') if match else None


class ProcessNameTest(unittest.TestCase):
    def next_call(self, name):
        args = uprof.build_parser().parse_args(["profile", "--session", SESSION,
                                                "--by", "process"])
        calls = uprof_query._next_calls(args, {}, system_wide=True, top_process=(None, name))
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    def test_a_hostile_image_name_becomes_an_inert_pattern_that_still_matches(self):
        for name in HOSTILE:
            with self.subTest(name=name):
                pattern = image_argument(self.next_call(name))
                self.assertIsNotNone(pattern)
                self.assertFalse(LIVE & set(pattern), pattern)
                self.assertNotIn(";", pattern.replace("_", ""))
                regex = "".join("." if char == "_" else re.escape(char) for char in pattern)
                self.assertRegex(name, re.compile(f"^{regex}$", re.IGNORECASE))

    def test_a_plain_image_name_is_kept(self):
        self.assertEqual(image_argument(self.next_call("workload.exe")), "workload.exe")


class CandidateDirectoryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="uprof-hostile-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def capture(self, name):
        directory = self.root / name
        directory.mkdir()
        (directory / "session.uprof").write_text("{}", encoding="utf-8")
        (directory / "cpu.db").write_bytes(b"")
        return directory

    def message(self):
        args = uprof.build_parser().parse_args(["profile", "--session", str(self.root),
                                                "--by", "module"])
        with self.assertRaises(UprofError) as caught:
            uprof_query.run_query_command(args)
        return str(caught.exception)

    def test_hostile_directories_are_named_as_data_not_commands(self):
        names = [name for name in ("$(touch pwned)", "a`id`", "%PATH%", "a!b")
                 if os.name != "nt" or '"' not in name]
        for name in names:
            self.capture(name)
        safe = self.capture("run 1")
        message = self.message()
        self.assertNotIn("\n", message)
        commands = message.split("run one of: ", 1)[1].split("; not offered as commands", 1)[0]
        self.assertIn(f"--session {uprof_query._quote(str(safe))} --by module", commands)
        for name in names:
            with self.subTest(name=name):
                self.assertNotIn(name, commands)
                self.assertIn(repr(str(self.root / name)), message)

    def test_only_hostile_directories_still_end_in_one_cause_line(self):
        self.capture("$(touch pwned)")
        message = self.message()
        self.assertNotIn("\n", message)
        self.assertNotIn("run one of", message)
        self.assertIn("not offered as commands", message)


if __name__ == "__main__":
    unittest.main()
