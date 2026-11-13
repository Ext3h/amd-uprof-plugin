"""Names from capture data or from disk enter runnable commands only as plain characters.

A process image name (the system-wide ranking's NEXT when the top row has no PID) and the names
of capture directories found below a wrong --session come from the capture or an extracted
archive, not from the caller. A command pasted into Git Bash, PowerShell 5.1 or cmd.exe must not
run or expand anything such a name holds: `$(...)`, backticks and `$VAR` (bash, PowerShell),
`%VAR%` and `!VAR!` (cmd.exe), straight or typographic double quotes (PowerShell reads U+201C,
U+201D and U+201E as `"`), control and bidi characters. So only an allow-list of plain
characters may reach a command: the process name enters --image as an ILIKE pattern with every
other character replaced by the one-character wildcard, and a directory with any other character
is named as data, not as a command.
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
# One hostile name per shell-specific hazard, plus shared ones.
HOSTILE_IMAGES = [
    "x$(touch pwned).exe",      # bash and PowerShell subexpression
    "a`id`.exe",                # bash command substitution, PowerShell escape
    "$HOME.exe",                # bash and PowerShell variable
    "%PATH%.exe",               # cmd.exe variable
    "a!b!.exe",                 # cmd.exe delayed expansion
    'a"b.exe',                  # ends the argument everywhere
    "a\u201d;calc;\u201cb.exe",  # PowerShell typographic quotes
    "a\u202eexe.txt",           # bidi override
    "a;b|c.exe",
    "-rf.exe",                  # read as an option when unquoted
]
HOSTILE_DIRECTORIES = ["$(touch pwned)", "a`id`", "%PATH%", "a!b!", "a\u201d;calc;\u201cb",
                       "a\u202eexe.txt"]
# The characters a capture- or disk-derived name may carry into a command; independent of the
# implementation's own list so that a missing hazard shows up here.
IMAGE_PLAIN = set(" ._-+,=@#~()")
PATH_PLAIN = IMAGE_PLAIN | set("\\/:'&;[]{}")


def plain(text, allowed):
    return all(char.isalnum() or char in allowed for char in text)


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

    def test_a_hostile_image_name_becomes_a_plain_pattern_that_still_matches(self):
        for name in HOSTILE_IMAGES:
            with self.subTest(name=name):
                call = self.next_call(name)
                pattern = image_argument(call)
                self.assertIsNotNone(pattern)
                self.assertTrue(plain(pattern, IMAGE_PLAIN), pattern)
                self.assertFalse(pattern.startswith("-"), pattern)
                regex = "".join("." if char == "_" else re.escape(char) for char in pattern)
                self.assertRegex(name, re.compile(f"^{regex}$", re.IGNORECASE))
                args = uprof.build_parser().parse_args(
                    ["profile", "--session", SESSION, "--by", "module", "--image", pattern])
                self.assertEqual(args.image, pattern)

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

    def offered_sessions(self, message):
        return re.findall(r' profile --session ("[^"]*"|\S+)', message)

    def test_hostile_directories_are_named_as_data_not_commands(self):
        for name in HOSTILE_DIRECTORIES:
            self.capture(name)
        safe = self.capture("run 1 & co")
        message = self.message()
        self.assertNotIn("\n", message)
        offered = [value.strip('"') for value in self.offered_sessions(message)]
        self.assertEqual(offered, [str(safe).replace("%", "%%") if os.name == "nt"
                                   else str(safe)])
        for value in offered:
            self.assertTrue(plain(value, PATH_PLAIN), value)
        held = message.split("not offered as commands", 1)[1]
        self.assertIn(f"{len(HOSTILE_DIRECTORIES)} not offered as commands", message)
        for name in sorted(HOSTILE_DIRECTORIES)[:5]:
            with self.subTest(name=name):
                self.assertIn(repr(str(self.root / name)), held)

    def test_only_hostile_directories_still_end_in_one_cause_line(self):
        self.capture("a\u202eexe.txt")
        message = self.message()
        self.assertNotIn("\n", message)
        self.assertNotIn("run one of", message)
        self.assertEqual(self.offered_sessions(message), [])
        self.assertIn("1 not offered as commands", message)
        self.assertIn(repr(str(self.root / "a\u202eexe.txt")), message)
        self.assertNotIn("\u202e", message)


if __name__ == "__main__":
    unittest.main()
