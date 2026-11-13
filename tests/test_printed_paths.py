"""Every printed command keeps a non-plain path out and names it as data instead.

A session or output path can come from an extracted archive, so `C:\\caps\\$(calc)` must not
reach the recapture remedy (an elevated collect when pasted into PowerShell), the failure
alternatives, the translate remedy or the COMMAND= line. Such a path becomes a <PLACEHOLDER>
in the command and a NAME='...' data line; a plain path, including one with `'`, stays runnable
in "..." quoting. The quoting itself is exercised in real shells by test_shell_quoting.
Run from the repository root: python tests/run_tests.py test_printed_paths
"""
# Spec: SPEC-77

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof
import uprof_collect
import uprof_native
import uprof_query
from uprof_util import UprofError

ROOT = "C:\\caps" if os.name == "nt" else "/caps"
HOSTILE = os.path.join(ROOT, "$(calc)")
QUOTED = os.path.join(ROOT, "it's run")


def command_lines(text):
    """The parts of a message that are commands: everything except NAME='...' data lines."""
    return [line for line in text.splitlines() if not line.split("=", 1)[0].isupper()]


class RecaptureTest(unittest.TestCase):
    def test_a_hostile_session_gives_a_placeholder_and_a_data_line(self):
        cycles, ibs, data = uprof_native.recapture_commands(Path(HOSTILE))
        for command in (cycles, ibs):
            self.assertNotIn("$(calc)", command)
            self.assertIn("--out-dir <NEW_OUT_DIR>", command)
        self.assertEqual(data, "NEW_OUT_DIR=" + repr(HOSTILE + "-recapture"))

    def test_a_plain_session_with_a_quote_stays_runnable(self):
        cycles, _ibs, data = uprof_native.recapture_commands(Path(QUOTED))
        self.assertIn(f'--out-dir "{QUOTED}-recapture"', cycles)
        self.assertIsNone(data)

    def test_the_translate_failure_puts_the_path_on_its_own_line(self):
        completed = subprocess.CompletedProcess(["x"], -1073741819, "Translation started\n", "")
        log = mock.Mock()
        log.appended.return_value = (Path("cli.log"), "No categories to translate...")
        message = str(uprof_native.translate_failure("translate", completed, log, Path(HOSTILE)))
        lines = message.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("$(calc)", lines[0])
        self.assertEqual(lines[1], "NEW_OUT_DIR=" + repr(HOSTILE + "-recapture"))


class FailureAlternativesTest(unittest.TestCase):
    def test_a_hostile_session_is_a_placeholder_in_every_alternative(self):
        with mock.patch.object(uprof_native, "find_uprof", return_value=None):
            message = str(
                uprof_native.failure("cause", ["whole capture", "timer window"], session=HOSTILE)
            )
        for line in command_lines(message):
            self.assertNotIn("$(calc)", line)
        self.assertIn("--session <SESSION_DIR>", message)
        self.assertIn("SESSION_DIR=" + repr(HOSTILE), message.splitlines())

    def test_a_plain_session_with_a_quote_stays_runnable(self):
        with mock.patch.object(uprof_native, "find_uprof", return_value=None):
            message = str(uprof_native.failure("cause", ["whole capture"], session=QUOTED))
        self.assertIn(f'--session "{QUOTED}"', message)
        self.assertNotIn("SESSION_DIR=", message)


class TranslateRemedyTest(unittest.TestCase):
    def remedy(self, name):
        with tempfile.TemporaryDirectory(prefix="uprof-remedy-") as temp:
            session = Path(temp) / name
            session.mkdir()
            args = uprof.build_parser().parse_args(
                ["profile", "--session", str(session), "--by", "module"]
            )
            with self.assertRaises(UprofError) as caught:
                uprof_query.run_query_command(args)
            return str(caught.exception), str(session)

    def test_a_hostile_session_is_a_placeholder(self):
        message, session = self.remedy("$(calc)")
        self.assertIn("--session <SESSION_DIR>", message)
        self.assertEqual(message.splitlines()[-1], "SESSION_DIR=" + repr(session))
        command = message.split("run translate", 1)[1].split(" first", 1)[0]
        self.assertNotIn("$(calc)", command)

    def test_a_plain_session_is_double_quoted(self):
        message, session = self.remedy("it's run")
        self.assertIn(f'--session "{session}"', message)
        self.assertNotIn(f"'{session}'", message)


class CommandLineTest(unittest.TestCase):
    def command_output(self, out_dir):
        options = uprof_collect.CollectOptions(out_dir=out_dir, dry_run=True)
        out = io.StringIO()
        with (
            mock.patch.object(uprof_collect, "find_uprof", return_value=None),
            mock.patch.object(
                uprof_collect, "_resolve_interval", return_value=(1_000_000, "INTERVAL=1000000")
            ),
            mock.patch.object(uprof_collect, "is_admin", return_value=True),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            uprof_collect.collect(options)
        return out.getvalue().splitlines()

    def test_a_hostile_out_dir_is_a_placeholder_in_command(self):
        lines = self.command_output(HOSTILE)
        command = next(line for line in lines if line.startswith("COMMAND="))
        self.assertNotIn("$(calc)", command)
        self.assertIn("-o <OUT_DIR>", command)
        self.assertIn("OUT_DIR=" + repr(HOSTILE), lines)

    def test_a_plain_out_dir_stays_in_command(self):
        lines = self.command_output(QUOTED)
        command = next(line for line in lines if line.startswith("COMMAND="))
        self.assertIn(f'-o "{QUOTED}"', command)


if __name__ == "__main__":
    unittest.main()
