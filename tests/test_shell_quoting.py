"""Every plain string survives the printed quoting in the real shells, byte for byte.

uprof_shell.PLAIN is the allow-list for text the caller did not type (capture, disk and install
paths and names) that may go into a printed, runnable command. Each character on it, and
combinations that would run a command if quoting failed, is printed with uprof_shell.quote and
run through the shells a pasted command meets: Git Bash/bash on POSIX hosts, PowerShell 5.1 and
cmd.exe on Windows. The argument must arrive unchanged and nothing else may run. Strings off the
list are rejected and become a placeholder plus a data line.
Run from the repository root: python tests/run_tests.py test_shell_quoting
"""
# Spec: SPEC-47

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import uprof_shell
from run_tests import HOST_SKIP

ECHO = "import json, sys\nsys.stdout.write(json.dumps(sys.argv[1:]))\n"
EVERY_PLAIN = "A" + "".join(sorted(uprof_shell.PLAIN)).replace("\\", "") + "\\z"
CASES = [
    EVERY_PLAIN,
    "a&echo PWNED&b",
    "a;echo PWNED;b",
    "a & echo PWNED",
    "x'y'z",
    "(echo PWNED)",
    "a)&(echo PWNED",
    "&;'()",
    "[a]{b}",
    "~/x #y",
    "C:\\Program Files (x86)\\a&b;c'd\\e",
    "a=b,c+d",
    "a@b",
    "plain.exe",
    "C:/dir/sub:x",
    "key=value",
]
NOT_PLAIN = ["", "-rf", "@resp", "C:\\dir\\", "\\\\server\\share", "a$b", "a$(calc)", "a`b",
             "a%PATH%b", "a!b", 'a"b', "a\u201cb\u201d", "a\u201eb", "caf\u00e9", "a\tb",
             "a\u202eb", "a|b", "a<b", "a>b", "a^b", "a*b", "a?b"]


def echoed(stdout: str):
    return json.loads(stdout) if stdout.strip() else None


class PlainSetTest(unittest.TestCase):
    def test_cases_are_plain_and_rejects_are_not(self):
        for text in CASES:
            with self.subTest(text=text):
                self.assertTrue(uprof_shell.is_plain(text), text)
        for text in NOT_PLAIN:
            with self.subTest(text=text):
                self.assertFalse(uprof_shell.is_plain(text), text)
                with self.assertRaises(ValueError):
                    uprof_shell.quote(text)

    def test_a_path_off_the_list_becomes_a_placeholder_and_a_data_line(self):
        argument, data = uprof_shell.path_arg("C:\\caps\\$(calc)", "<SESSION_DIR>")
        self.assertEqual(argument, "<SESSION_DIR>")
        self.assertEqual(data, "SESSION_DIR=" + repr("C:\\caps\\$(calc)"))
        argument, data = uprof_shell.path_arg("C:\\caps\\run 1", "<SESSION_DIR>")
        self.assertEqual((argument, data), ('"C:\\caps\\run 1"', None))

    def test_image_patterns_are_plain_and_match(self):
        for name in NOT_PLAIN[1:]:
            with self.subTest(name=name):
                pattern = uprof_shell.image_pattern(name)
                self.assertTrue(uprof_shell.is_plain(pattern), pattern)


class RealShellTest(unittest.TestCase):
    """The printed command `python <echo script> <quoted case>` run as a pasted line."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="uprof-shell-")
        cls.script = Path(cls.temp.name) / "echo_args.py"
        cls.script.write_text(ECHO, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def command(self, text):
        for part in (sys.executable, str(self.script)):
            if not uprof_shell.is_plain(part):
                self.skipTest(HOST_SKIP + f"the interpreter or temp path is not plain: {part!r}")
        return " ".join(uprof_shell.quote(part) for part in (sys.executable, str(self.script),
                                                            text))

    def check(self, run):
        for text in CASES:
            with self.subTest(text=text):
                completed = run(self.command(text))
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(echoed(completed.stdout), [text],
                                 completed.stdout + completed.stderr)

    @unittest.skipIf(os.name == "nt", HOST_SKIP + "bash runs on the POSIX CI host")
    def test_bash(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest(HOST_SKIP + "no bash on this host")
        self.check(lambda line: subprocess.run([bash, "-c", line], capture_output=True,
                                               text=True, encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", HOST_SKIP + "PowerShell 5.1 runs on Windows")
    def test_powershell(self):
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
            "System32/WindowsPowerShell/v1.0/powershell.exe")

        def run(line):
            # A pasted line whose first token is quoted needs the call operator.
            script = ("& " + line) if line.startswith('"') else line
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            return subprocess.run([str(powershell), "-NoProfile", "-NonInteractive",
                                   "-EncodedCommand", encoded],
                                  capture_output=True, text=True, encoding="utf-8")
        self.check(run)

    @unittest.skipUnless(os.name == "nt", HOST_SKIP + "cmd.exe runs on Windows")
    def test_cmd(self):
        cmd = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/cmd.exe"
        # The string reaches CreateProcess verbatim; /s with outer quotes makes cmd.exe run the
        # line exactly as typed at its prompt, without /c's own quote stripping.
        self.check(lambda line: subprocess.run(f'"{cmd}" /d /s /c "{line}"', capture_output=True,
                                               text=True, encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
