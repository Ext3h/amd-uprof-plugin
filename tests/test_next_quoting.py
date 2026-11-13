"""Every argument of a printed NEXT or candidate call goes through the shared shell helper.

The session path, --function and --module (copied from rows), --image, --time-filter,
--symbol-path, --image-path and the install path of uprof.py can all hold characters a shell
runs or expands: a capture extracted from an archive, an MSVC name like
`anonymous namespace'::f`. A plain value is quoted the way every shell reads alike; any other
value becomes a <PLACEHOLDER> in the call and is named as data (legend.next_data, and one header
field per placeholder), never inside a command. A plain call pasted into a real shell delivers
every argument unchanged.
Run from the repository root: python tests/run_tests.py test_next_quoting
"""
# Spec: SPEC-37

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import uprof
import uprof_query
import uprof_shell
from run_tests import HOST_SKIP
from uprof_util import absolute

ROOT = "C:\\caps" if os.name == "nt" else "/caps"


def calls_and_data(*argv):
    args = uprof.build_parser().parse_args(["profile", *argv])
    data = {}
    calls = uprof_query._next_calls(args, {}, data=data)
    return calls, data


class PlaceholderTest(unittest.TestCase):
    def test_hostile_values_become_placeholders_named_as_data(self):
        session = os.path.join(ROOT, "$(calc)")
        smart = os.path.join(ROOT, "a\u201d;calc;\u201cb")
        function = "`anonymous namespace'::f"
        module = "a$(calc).dll"
        for argv, placeholder, value in (
                (["--session", session, "--by", "module"], "<SESSION_DIR>", session),
                (["--session", smart, "--by", "module"], "<SESSION_DIR>", smart),
                (["--session", ROOT, "--by", "line", "--function", function,
                  "--module", "app.exe"], "<FUNCTION>", function),
                (["--session", ROOT, "--by", "line", "--function", "f", "--module", module],
                 "<MODULE>", module),
                (["--session", ROOT, "--by", "module", "--image", "%work$(x)%"], "<IMAGE>",
                 "%work$(x)%"),
                (["--session", ROOT, "--by", "module", "--symbol-path",
                  os.path.join(ROOT, "`id`")], "<SYMBOL_PATH>", os.path.join(ROOT, "`id`"))):
            with self.subTest(placeholder=placeholder, value=value):
                calls, data = calls_and_data(*argv)
                self.assertTrue(calls)
                for call in calls:
                    self.assertNotIn(value, call)
                    self.assertIn(placeholder, call)
                key = placeholder.strip("<>")
                if key == "SESSION_DIR":
                    self.assertEqual(Path(data[key]), Path(value))
                else:
                    self.assertEqual(data[key], value)

    def test_plain_values_need_no_data(self):
        calls, data = calls_and_data("--session", os.path.join(ROOT, "it's & run (1)"),
                                     "--by", "line", "--function", "ns::f", "--module", "a;b.dll")
        self.assertEqual(data, {})
        self.assertNotIn("<", " ".join(calls))


class RealShellNextTest(unittest.TestCase):
    """A plain NEXT call, with uprof.py swapped for an argv echo, run in the real shells."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="uprof-next-shell-")
        cls.script = Path(cls.temp.name) / "echo_args.py"
        cls.script.write_text("import json, sys\nsys.stdout.write(json.dumps(sys.argv[1:]))\n",
                              encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def line_and_expected(self):
        session = os.path.join(self.temp.name, "it's & run (1) [x]{y}")
        args = uprof.build_parser().parse_args(
            ["profile", "--session", session, "--by", "line", "--function", "ns::f;g",
             "--module", "a&b.dll", "--time-filter", "1:2"])
        data = {}
        call = uprof_query._next_calls(args, {}, data=data)[0]
        self.assertEqual(data, {})
        tool = uprof_shell.quote(str(uprof_query.TOOLS_DIR / "uprof.py"))
        self.assertTrue(call.startswith(f"python {tool} "), call)
        for part in (sys.executable, str(self.script)):
            if not uprof_shell.is_plain(part):
                self.skipTest(HOST_SKIP + f"the interpreter or temp path is not plain: {part!r}")
        line = (f"{uprof_shell.quote(sys.executable)} {uprof_shell.quote(str(self.script))} "
                + call[len(f"python {tool} "):])
        # The call carries the session as uprof_util.absolute spells it (no 8.3 expansion).
        expected = ["profile", "--session", str(absolute(session)), "--by", "instruction",
                    "--time-filter", "1:2", "--function", "ns::f;g", "--module", "a&b.dll",
                    "--json"]
        return line, expected

    def check(self, run):
        line, expected = self.line_and_expected()
        completed = run(line)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), expected, completed.stdout)

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
            script = ("& " + line) if line.startswith('"') else line
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            return subprocess.run([str(powershell), "-NoProfile", "-NonInteractive",
                                   "-EncodedCommand", encoded],
                                  capture_output=True, text=True, encoding="utf-8")
        self.check(run)

    @unittest.skipUnless(os.name == "nt", HOST_SKIP + "cmd.exe runs on Windows")
    def test_cmd(self):
        cmd = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/cmd.exe"
        self.check(lambda line: subprocess.run(f'"{cmd}" /d /s /c "{line}"', capture_output=True,
                                               text=True, encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
