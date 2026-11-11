"""Compare the debug-format trace capture with the goldens committed for this OS.

The goldens are produced by tests/capture_debug_traces.py in the
.github/workflows/capture-debug-traces.yml job and committed under
fixtures/debug/traces/<os>/. A Linux golden is evidence from the Linux runner, not from a
Windows cross-read. Run from the repository root: python tests/run_tests.py test_debug_traces
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CAPTURE = HERE / "capture_debug_traces.py"
OS_NAME = "windows" if os.name == "nt" else "linux"
GOLDENS = F.DEBUG / "traces" / OS_NAME


def _duckdb_installed() -> bool:
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return False
    return True


SKIP = F.tool_skip() or ("duckdb is required; install duckdb==2.0.0"
                         if not _duckdb_installed() else None)


@unittest.skipIf(SKIP, SKIP or "")
class DebugTraceTest(unittest.TestCase):
    def test_capture_matches_the_committed_goldens(self):
        if not GOLDENS.is_dir():
            self.skipTest(f"no {OS_NAME} goldens; run the capture-debug-traces workflow")
        with tempfile.TemporaryDirectory(prefix="uprof-trace-compare-") as root:
            completed = subprocess.run(
                [sys.executable, str(CAPTURE), "--out-dir", root],
                cwd=str(REPO), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            captured = Path(root) / OS_NAME
            generated = {path.name: path for path in captured.glob("*.txt")}
            committed = {path.name: path for path in GOLDENS.glob("*.txt")}
            self.assertEqual(sorted(generated), sorted(committed),
                             "trace case set differs from the goldens")
            for name in sorted(committed):
                with self.subTest(trace=name):
                    actual = generated[name].read_text(encoding="utf-8")
                    expected = committed[name].read_text(encoding="utf-8")
                    self.assertEqual(actual, expected,
                                     f"{OS_NAME}/{name} differs from the golden")
