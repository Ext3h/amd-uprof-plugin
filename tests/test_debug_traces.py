"""Compare the debug-format trace capture with the goldens committed for this OS.

The goldens are produced by tests/capture_debug_traces.py in the
.github/workflows/capture-debug-traces.yml job and committed under
fixtures/debug/traces/<os>/. A Linux golden is evidence from the Linux runner, not from a
Windows cross-read. Run from the repository root: python tests/run_tests.py test_debug_traces
"""
# Spec: SPEC-113

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
                check=False,
                cwd=str(REPO),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            captured = Path(root) / OS_NAME
            generated = {path.name: path for path in captured.glob("*.txt")}
            committed = {path.name: path for path in GOLDENS.glob("*.txt")}
            self.assertEqual(
                sorted(generated), sorted(committed), "trace case set differs from the goldens"
            )
            for name in sorted(committed):
                with self.subTest(trace=name):
                    actual = generated[name].read_text(encoding="utf-8")
                    expected = committed[name].read_text(encoding="utf-8")
                    self.assertEqual(actual, expected, f"{OS_NAME}/{name} differs from the golden")


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


def _sections(text: str) -> dict[str, list[str]]:
    """The lines of each '## name' section of a trace."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return sections


class GoldenSemanticsTest(unittest.TestCase):
    """The committed goldens never approve a symbol outcome that contradicts the resolved rows."""

    def test_a_module_whose_rows_resolve_gets_no_unresolved_warning(self):
        for path in sorted((F.DEBUG / "traces").glob("*/*.txt")):
            sections = _sections(path.read_text(encoding="utf-8"))
            frames = [
                line
                for line in sections.get("production", [])
                if line.startswith("0x") and line.split()[1] != "<none>"
            ]
            if not frames:
                continue
            with self.subTest(trace=f"{path.parent.name}/{path.name}"):
                outcome = [line for line in sections["production"] if line.startswith("outcome=")]
                self.assertEqual(outcome, ["outcome=resolved"])
                for name in ("profile.line", "profile.function"):
                    warnings = [
                        line
                        for line in sections.get(name, [])
                        if line.startswith("WARNING: no symbols resolved")
                    ]
                    self.assertEqual(warnings, [], name)

    def test_a_module_whose_rows_all_stay_unresolved_names_a_cause(self):
        for path in sorted((F.DEBUG / "traces").glob("*/*.txt")):
            sections = _sections(path.read_text(encoding="utf-8"))
            frames = [line for line in sections.get("production", []) if line.startswith("0x")]
            if not frames or any(line.split()[1] != "<none>" for line in frames):
                continue
            with self.subTest(trace=f"{path.parent.name}/{path.name}"):
                outcome = [line for line in sections["production"] if line.startswith("outcome=")]
                self.assertNotEqual(outcome, ["outcome=resolved"])
                for name in ("profile.line", "profile.function"):
                    coverage = [
                        line
                        for line in sections.get(name, [])
                        if line.startswith("symbol_coverage=")
                    ]
                    self.assertIn("cause by module:", coverage[0], name)


class PdbSectionAddressTest(unittest.TestCase):
    """A PDB public's RVA adds its section's virtual address, read as hex (#141)."""

    HEADERS = "SECTION HEADER #1\n    1A000 virtual address\n"
    PUBLICS = (
        "   0 | S_PUB32 [size = 32] `fixture_hot`\n     flags = function, addr = {section}:16\n"
    )

    def _section(self, publics):
        import capture_debug_traces as C

        case = C.Case(name="pe_fixture", image=Path("fixture.exe"), addresses=(), pdb=Path("x.pdb"))
        outputs = {"--summary": "", "--types": "", "--section-headers": self.HEADERS}

        def fake_tool(tool, *args):
            return outputs.get(args[1], publics)

        with mock.patch.object(C, "run_tool", fake_tool):
            return C.pdb_section(case)

    def test_a_section_address_with_hex_letters_is_added(self):
        lines = self._section(self.PUBLICS.format(section=1))
        self.assertIn("public fixture_hot rva=0x1A010", lines)

    def test_a_public_in_an_unknown_section_fails(self):
        with self.assertRaises(SystemExit) as raised:
            self._section(self.PUBLICS.format(section=2))
        self.assertIn("fixture_hot", str(raised.exception))


class ProfileSectionCacheTest(unittest.TestCase):
    """A profile section also runs the session-cache path, not only the cold run (#136)."""

    def _section(self, outputs):
        import capture_debug_traces as C

        case = next(case for case in C.CASES if case.name == "elf_fixture_nosym")
        sessions = []

        def fake_profile(session, by):
            sessions.append(session)
            return outputs[len(sessions) - 1]

        with (
            mock.patch.object(F, "write_session", lambda root, *_: Path(root) / "session"),
            mock.patch.object(F, "profile_with_stderr", fake_profile),
        ):
            lines = C.profile_section(case, "line")
        return sessions, lines

    def test_the_cached_run_reads_the_same_session(self):
        cold = ({"SYMBOL_COVERAGE": "cov", "rows": []}, "WARNING: cold\n")
        sessions, lines = self._section([cold, cold])
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0], sessions[1])
        self.assertEqual(lines, ["## profile.line", "symbol_coverage=cov", "WARNING: cold"])

    def test_a_differing_cached_run_gets_its_own_section(self):
        cold = ({"SYMBOL_COVERAGE": "cov", "rows": []}, "WARNING: cold\n")
        warm = ({"SYMBOL_COVERAGE": "cov", "rows": []}, "")
        _, lines = self._section([cold, warm])
        self.assertEqual(
            lines,
            [
                "## profile.line",
                "symbol_coverage=cov",
                "WARNING: cold",
                "## profile.line cached",
                "symbol_coverage=cov",
            ],
        )
