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


SKIP = F.tool_skip() or F.duckdb_skip()
# The goldens hold the output of an LLVM that reads PDBs and compressed DWARF.
TRACE_SKIP = SKIP or F.llvm_version_skip() or F.zlib_skip()


@unittest.skipIf(TRACE_SKIP, TRACE_SKIP or "")
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


@unittest.skipIf(SKIP, SKIP or "")
class ProfileSectionTest(unittest.TestCase):
    """A profile section keeps the symbol-outcome evidence, not only the JSON rows."""

    def test_profile_section_keeps_warning_and_symbol_coverage_lines(self):
        import capture_debug_traces as C

        case = next(case for case in C.CASES if case.name == "elf_fixture_nosym")
        for by in ("line", "function"):
            with self.subTest(by=by):
                text = C.normalize("\n".join(C.profile_section(case, by)))
                lines = text.splitlines()
                coverage = [line for line in lines if line.startswith("symbol_coverage=")]
                self.assertEqual(len(coverage), 1, text)
                warnings = [line for line in lines if line.startswith("WARNING: ")]
                self.assertTrue(any("<SESSION_ROOT>" in line for line in warnings), text)
                self.assertNotIn("uprof-trace-session-", text)
                self.assertNotIn(str(F.DEBUG), text)
