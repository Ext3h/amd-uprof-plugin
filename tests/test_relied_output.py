"""Printed items the skill's call sequence relies on keep printing under their condition.

The skill's instructions rely on the tools to say what to do next, instead of restating it.
Most such items have their own tests; this file covers the ones that had none: the anchor NOTE
that names --time-filter, the PMC --time-filter error that names --event ibs-op, and the
SESSION= and CPU_DB= lines translate prints for the next call.

Run from the repository root: python tests/run_tests.py test_relied_output
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import debug_fixtures as F
import uprof_native
import uprof_session

REPO = Path(__file__).resolve().parents[1]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"


class AnchorNoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Path(self.tmp.name)
        meta = {"collection": {"schedule": {"start": 1_000_000_000}}}
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")

    def note(self, qpc: int) -> str:
        lines = uprof_session.anchor_report(self.session, qpc, None)
        return next(line for line in lines if line.startswith("NOTE="))

    def test_a_reading_inside_the_capture_is_comparable_to_the_time_filter(self):
        # 20,000,000 ticks at the default 10 MHz is 2 s, 1 s after the capture start.
        self.assertIn("comparable to --time-filter", self.note(20_000_000))

    def test_a_reading_before_the_start_is_attach_latency(self):
        self.assertIn("attach latency", self.note(5_000_000))


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class PmcTimeFilterTest(unittest.TestCase):
    def test_the_error_names_the_capture_that_supports_the_filter(self):
        sys.path.insert(0, str(REPO / "trials"))
        import build_captures

        with tempfile.TemporaryDirectory() as directory:
            session = build_captures.build(Path(directory) / "c", ["pmc"])["pmc"]
            result = subprocess.run(
                [
                    sys.executable,
                    str(UPROF),
                    "profile",
                    "--session",
                    str(session),
                    "--by",
                    "module",
                    "--time-filter",
                    "0:1",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--time-filter cannot be applied to a PMC capture", result.stderr)
        self.assertIn("--event ibs-op", result.stderr)


class TranslateSessionLinesTest(unittest.TestCase):
    def test_translate_prints_the_session_and_database_for_the_next_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session, out = root / "session", root / "out"
            session.mkdir()
            out.mkdir()
            (session / "session.uprof").write_bytes(b"session")

            def fake_run(command, **_kwargs):
                (out / "cpu.db").write_text("db", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            stdout = io.StringIO()
            with (
                mock.patch.object(uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")),
                mock.patch.object(uprof_native, "run_watched", side_effect=fake_run),
                contextlib.redirect_stdout(stdout),
            ):
                uprof_native.translate(session, out, force=False)
            lines = stdout.getvalue().splitlines()
        self.assertIn(f"SESSION={out}", lines)
        self.assertIn(f"CPU_DB={out / 'cpu.db'}", lines)


if __name__ == "__main__":
    unittest.main()
