"""SYMBOL_COVERAGE discloses per-module function-name coverage before a drill-down.

A module whose samples carry no non-empty Function.name cannot be resolved below module/RVA.
The line states the aggregate resolved share and the unresolved count per module, including a
module that is absent from UnifiedSampleSeries entirely (function_id NULL). Without it a user
reconstructs PE function boundaries by hand, which is the detour issue #27 records.

Run from the repository root: python tests/run_tests.py test_symbol_coverage
"""
# Spec: SPEC-63, SPEC-67

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb
import uprof_native
import uprof_query
import uprof_symbols
from test_function_region_profile import FIXTURE, Session

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"

GOOD = "good.dll"
NO_STRINGS = "nostrings.dll"
PARTIAL = "partial.dll"
UNKNOWN = "unknown.dll"

GOOD_OFFSET = 4096
NO_STRINGS_OFFSET = 8192
PARTIAL_NAMED_OFFSET = 16384
PARTIAL_ANON_OFFSET = 20480
UNKNOWN_OFFSET = 24576

NO_NAMED_STOP = (
    "a module with no source names is covered by derived function ranges: use "
    "profile --by function, which derives boundaries from the binary (.pdata, PE "
    "exports, PDB publics) with LLVM; do not write a PE parser, map .pdata by hand "
    "or disassemble"
)
PROCESS_STOP = (
    "missing source names do not block the NEXT call, profile --by module, which attributes "
    "these samples to their module; do not write a PE parser, map .pdata by hand or disassemble"
)
UNRESOLVED_STOP = (
    "unresolved samples are covered by derived function ranges: use profile --by "
    "function, which derives boundaries from the binary (.pdata, PE exports, PDB "
    "publics) with LLVM; do not write a PE parser, map .pdata by hand or "
    "disassemble"
)


def _dump_rows():
    """good 4, nostrings 3, partial 2 named + 1 anonymous, unknown 2 rows."""
    rows = [(index * 1_000_000_000, GOOD, GOOD_OFFSET) for index in range(4)]
    rows += [
        (4_000_000_000 + index * 1_000_000_000, NO_STRINGS, NO_STRINGS_OFFSET) for index in range(3)
    ]
    rows += [
        (7_000_000_000 + index * 1_000_000_000, PARTIAL, PARTIAL_NAMED_OFFSET) for index in range(2)
    ]
    rows += [(9_000_000_000, PARTIAL, PARTIAL_ANON_OFFSET)]
    rows += [
        (10_000_000_000 + index * 1_000_000_000, UNKNOWN, UNKNOWN_OFFSET) for index in range(2)
    ]
    return rows


class SymbolCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Path(self.tmp.name) / "session"
        self.session.mkdir()
        self._write_session()
        self._write_dump()
        self._write_cpu_db()

    def _write_session(self, system_wide=False):
        meta = {
            "translateCommand": ("AMDuProfCLI.exe translate -i session --ascii ibsop-event-dump"),
            "collection": {
                "schedule": {"elapsed": 11_000},
                "events": {
                    "ibsEvents": {
                        "op": {"interval": 4_700_000},
                        "opInterval": 4_700_000,
                        "opCycleCount": True,
                    }
                },
                "target": {"systemWide": system_wide, "attachedProcesses": [{"processId": 100}]},
            },
            "auxiliary": {"ibsOpSampling": True},
        }
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")

    def _write_dump(self):
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        processes = {GOOD: 100, NO_STRINGS: 200, PARTIAL: 300, UNKNOWN: 400}
        for tick, module, offset in _dump_rows():
            lines.append(f"{tick},0,1,{processes[module]},{offset},{module}")
        (self.session / "IbsOpDump.csv").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )

    def _write_cpu_db(self):
        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute(
                "CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)"
            )
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                "functionId UBIGINT, moduleRVA UBIGINT)"
            )
            connection.execute(
                "INSERT INTO Process VALUES (100, 'c:\\capture\\good.exe'), "
                "(200, 'c:\\capture\\nostrings.exe'), "
                "(300, 'c:\\capture\\partial.exe'), "
                "(400, 'c:\\capture\\unknown.exe')"
            )
            connection.execute(
                f"INSERT INTO Module VALUES (1, '{GOOD}'), (2, '{NO_STRINGS}'), "
                f"(3, '{PARTIAL}'), (4, '{UNKNOWN}')"
            )
            # Module 2 has a function row with a blank name; module 4 has no mapping at all.
            connection.execute(
                "INSERT INTO Function VALUES (10, 1, 'good_fn'), (20, 2, ''), (30, 3, 'partial_fn')"
            )
            connection.execute(
                f"INSERT INTO UnifiedSampleSeries VALUES "
                f"(1, 10, {GOOD_OFFSET}), (2, 20, {NO_STRINGS_OFFSET}), "
                f"(3, 30, {PARTIAL_NAMED_OFFSET})"
            )
        finally:
            connection.close()

    def profile(self, *extra):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(self.session),
                "--by",
                "function",
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def coverage(self, *extra):
        document = json.loads(self.profile("--json", *extra))
        return document.get("SYMBOL_COVERAGE")

    def test_aggregate_and_per_module_unresolved_share(self):
        text = self.coverage()

        self.assertTrue(
            text.startswith(
                "uProf DB 6/12 samples (50.0%) resolve to a named function; rows named 6/12 samples "
                "(50.0%), derived 0/12 samples (0.0%) from a synthetic binary range, unresolved "
                "6/12 samples (50.0%) (<no symbol>)"
            ),
            text,
        )
        self.assertIn(
            f"{NO_STRINGS}: module file not available: {NO_STRINGS} is not on this host", text
        )
        self.assertIn(f"{PARTIAL}: module file not available: {PARTIAL} is not on this host", text)
        self.assertIn(f"{UNKNOWN}: module file not available: {UNKNOWN} is not on this host", text)
        self.assertIn("no named or derived range covers these samples", text)

    def test_line_appears_in_markdown_and_json(self):
        output = self.profile()
        line = next(line for line in output.splitlines() if line.startswith("SYMBOL_COVERAGE: "))
        self.assertEqual(line.removeprefix("SYMBOL_COVERAGE: "), self.coverage())

    def test_scope_narrows_coverage_and_empty_scope_has_none(self):
        self.assertEqual(
            self.coverage("--pid", "100"),
            "uProf DB 4/4 samples (100.0%) resolve to a named function; rows named 4/4 "
            "samples (100.0%), derived 0/4 samples (0.0%) from a synthetic binary range, "
            "unresolved 0/4 samples (0.0%) (<no symbol>); every scoped sample is attributed "
            "below module/RVA; a derived row is actionable at its hot RVA",
        )
        self.assertIsNone(self.coverage("--pid", "999"))

    def test_process_granularity_also_discloses_coverage(self):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(self.session),
                "--by",
                "process",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "SYMBOL_COVERAGE: 6/12 samples (50.0%) resolve to a named function;", result.stdout
        )
        self.assertIn(f"unnamed samples by module: {NO_STRINGS} 3/3 (100.0%)", result.stdout)
        self.assertIn(f"{PARTIAL} 1/3 (33.3%)", result.stdout)
        self.assertIn(PROCESS_STOP, result.stdout)
        coverage_line = result.stdout.split("SYMBOL_COVERAGE:")[1].splitlines()[0]
        self.assertNotIn("unresolved", coverage_line)
        self.assertNotIn("use profile --by function", result.stdout)

    def test_coverage_is_deterministic(self):
        self.assertEqual(self.coverage(), self.coverage())

    def test_cycle_controlled_attached_lists_by_sample_count(self):
        # nostrings (3) and partial (3) tie on samples and keep module-name order; unknown (2)
        # follows. A time-weight order would be free to differ, so the explicit order matters.
        text = self.coverage()
        self.assertLess(text.index(NO_STRINGS), text.index(PARTIAL))
        self.assertLess(text.index(PARTIAL), text.index(UNKNOWN))

    def test_json_legend_carries_the_db_channel_and_the_unused_symbolizer_channel(self):
        document = json.loads(self.profile("--json"))
        entry = document["legend"]["symbol_coverage"]

        self.assertEqual(list(entry), ["uprof_db", "flags", "profile_symbolizer", "rows_drillable"])
        self.assertEqual(entry["uprof_db"]["named_samples"], 6)
        self.assertEqual(entry["uprof_db"]["scoped_samples"], 12)
        self.assertEqual(
            entry["profile_symbolizer"],
            {
                "source": "the emitted rows: llvm-symbolizer names plus derived PE ranges",
                "named_samples": 6,
                "derived_samples": 0,
                "resolved_samples": 6,
                "unresolved_samples": 6,
                "scoped_samples": 12,
            },
        )
        self.assertFalse(entry["rows_drillable"])
        self.assertEqual(entry["flags"], [])


def _coverage(rows, total, named):
    return {"rows": rows, "total": total, "named": named}


def _symbolizer(scoped, unresolved):
    return {
        "scoped_samples": scoped,
        "unresolved_samples": unresolved,
        "unresolved": 0,
        "unresolved_pct": 0.0,
    }


def _unresolved_row(module, samples, named):
    return {
        "module_name": module,
        "samples": samples,
        "named_samples": named,
        "scoped_samples": samples,
        "named_total": named,
    }


class CoverageChannelsTest(unittest.TestCase):
    """The DB channel and the symbolizer channel are reported separately, and the stop line follows
    the channel the profile rows actually use."""

    def test_explicit_pdb_does_not_pair_resolved_rows_with_a_stop_line(self):
        text = uprof_query._coverage_field(
            None,
            {},
            _symbolizer(5575, 0),
            True,
            ["--symbol-path"],
            _coverage([_unresolved_row("a.dll", 5575, 0)], 5575, 0),
        )[0][1]

        self.assertIn("uProf DB 0/5575 samples (0.0%) resolve to a named function", text)
        self.assertIn(
            "profile symbolizer 5575/5575 samples (100.0%) resolve below module/RVA "
            "(flags: --symbol-path)",
            text,
        )
        self.assertIn("every scoped sample is drillable below module/RVA", text)
        self.assertNotIn(NO_NAMED_STOP, text)
        self.assertNotIn(UNRESOLVED_STOP, text)

    def test_partial_symbolizer_resolution_keeps_the_boundary_and_names_the_flag(self):
        text = uprof_query._coverage_field(
            None,
            {"b.dll": "no PDB found"},
            _symbolizer(10, 4),
            True,
            ["--symbol-path"],
            _coverage([_unresolved_row("b.dll", 10, 0)], 10, 0),
        )[0][1]

        self.assertIn("uProf DB 0/10 samples (0.0%) resolve to a named function", text)
        self.assertIn(
            "profile symbolizer 6/10 samples (60.0%) resolve below module/RVA "
            "(flags: --symbol-path)",
            text,
        )
        self.assertIn("cause by module: b.dll: no PDB found", text)
        self.assertIn(UNRESOLVED_STOP, text)
        self.assertIn("remedy: pass profile --symbol-path", text)

    def test_an_image_without_readable_debug_info_is_not_blamed_on_the_capture(self):
        evidence = uprof_symbols.ModuleEvidence(
            "libnosym.so",
            "/opt/lib/libnosym.so",
            outcome="no_debug_info",
            note=uprof_symbols.NO_DEBUG_INFO_NOTE,
        )
        cause = uprof_query._evidence_cause(evidence)
        self.assertEqual(cause, uprof_symbols.NO_DEBUG_INFO_NOTE)
        evidence.found_debug_info = False
        self.assertIn(
            "recorded for this module in the capture", uprof_query._evidence_cause(evidence)
        )

    def no_pdb_cause(self):
        evidence = uprof_symbols.ModuleEvidence(
            "driver.dll",
            "C:/drivers/driver.dll",
            outcome="no_pdb_record",
            note=uprof_symbols.NO_PDB_NOTE,
        )
        return uprof_query._evidence_cause(evidence)

    def test_an_image_recording_no_pdb_is_a_stable_outcome_the_cache_keeps(self):
        def evidence(outcome):
            return uprof_symbols.ModuleEvidence("driver.dll", "C:/d/driver.dll", outcome=outcome)

        self.assertFalse(uprof_query._transient_outcome(evidence("no_pdb_record")))
        self.assertFalse(uprof_query._transient_outcome(evidence("resolved")))
        self.assertTrue(uprof_query._transient_outcome(evidence("readobj_failed")))

    def test_an_image_recording_no_pdb_is_named_as_such_not_as_a_readobj_failure(self):
        cause = self.no_pdb_cause()
        self.assertNotIn("could not read", cause)
        self.assertIn("no PDB can be found or staged", cause)

    def test_no_pdb_staging_remedy_when_every_image_records_no_pdb(self):
        text = uprof_query._coverage_field(
            None,
            {"driver.dll": self.no_pdb_cause()},
            _symbolizer(10, 10),
            True,
            [],
            _coverage([_unresolved_row("driver.dll", 10, 0)], 10, 0),
        )[0][1]
        self.assertNotIn("--symbol-path", text)
        self.assertNotIn("debug file beside it", text)

    def test_another_module_keeps_the_symbol_path_remedy(self):
        text = uprof_query._coverage_field(
            None,
            {"driver.dll": self.no_pdb_cause(), "b.dll": "no PDB found"},
            _symbolizer(10, 10),
            True,
            [],
            _coverage([_unresolved_row("driver.dll", 5, 0), _unresolved_row("b.dll", 5, 0)], 10, 0),
        )[0][1]
        self.assertIn("remedy: pass profile --symbol-path", text)

    def test_agreeing_channels_without_flags_keep_the_original_line(self):
        text = uprof_query._coverage_field(
            None,
            {},
            _symbolizer(10, 0),
            True,
            [],
            _coverage([_unresolved_row("a.dll", 10, 10)], 10, 10),
        )[0][1]

        self.assertEqual(
            text,
            "10/10 samples (100.0%) resolve to a named function; no module has unresolved samples",
        )

    def test_symbolizer_superset_without_flags_still_names_both_channels(self):
        text = uprof_query._coverage_field(
            None,
            {},
            _symbolizer(10, 0),
            True,
            [],
            _coverage([_unresolved_row("a.dll", 10, 0)], 10, 0),
        )[0][1]

        self.assertIn("uProf DB 0/10", text)
        self.assertIn("profile symbolizer 10/10", text)
        self.assertIn("every scoped sample is drillable", text)
        self.assertNotIn(NO_NAMED_STOP, text)
        self.assertNotIn(UNRESOLVED_STOP, text)

    def test_json_legend_records_channel_counts_flags_and_drillability(self):
        entry = uprof_query._symbol_coverage_legend(
            _coverage([_unresolved_row("a.dll", 5575, 0)], 5575, 0),
            _symbolizer(5575, 0),
            True,
            ["--symbol-path"],
        )

        self.assertEqual(list(entry), ["uprof_db", "flags", "profile_symbolizer", "rows_drillable"])
        self.assertEqual(entry["uprof_db"]["named_samples"], 0)
        self.assertEqual(entry["uprof_db"]["scoped_samples"], 5575)
        self.assertEqual(entry["flags"], ["--symbol-path"])
        self.assertEqual(entry["profile_symbolizer"]["resolved_samples"], 5575)
        self.assertEqual(entry["profile_symbolizer"]["unresolved_samples"], 0)
        self.assertEqual(entry["profile_symbolizer"]["scoped_samples"], 5575)
        self.assertTrue(entry["rows_drillable"])

    def test_line_scope_prints_symbolizer_channel_and_causes_when_symbolizer_resolved_nothing(self):
        text = uprof_query._coverage_field(
            None,
            {"fixture.exe": "llvm-symbolizer failed: exit status 1"},
            _symbolizer(30, 30),
            False,
            ["--symbol-path"],
            _coverage([_unresolved_row("fixture.exe", 30, 30)], 30, 30),
            by="line",
        )[0][1]

        self.assertIn("uProf DB 30/30 samples (100.0%) resolve to a named function", text)
        self.assertIn(
            "profile symbolizer 0/30 samples (0.0%) resolve below module/RVA "
            "(flags: --symbol-path)",
            text,
        )
        self.assertIn("cause by module: fixture.exe: llvm-symbolizer failed: exit status 1", text)
        self.assertIn(NO_NAMED_STOP, text)
        self.assertIn("remedy: pass profile --symbol-path", text)

    def test_instruction_scope_prints_symbolizer_channel_even_without_flags(self):
        text = uprof_query._coverage_field(
            None,
            {"fixture.exe": "no PDB found"},
            _symbolizer(30, 30),
            True,
            [],
            _coverage([_unresolved_row("fixture.exe", 30, 30)], 30, 30),
            by="instruction",
        )[0][1]

        self.assertIn("uProf DB 30/30 samples (100.0%) resolve to a named function", text)
        self.assertIn("profile symbolizer 0/30 samples (0.0%) resolve below module/RVA", text)
        self.assertIn("cause by module: fixture.exe: no PDB found", text)
        self.assertIn(NO_NAMED_STOP, text)
        self.assertIn("remedy: pass profile --symbol-path", text)

    def test_json_legend_line_scope_records_zero_resolved_when_symbolizer_resolved_nothing(self):
        entry = uprof_query._symbol_coverage_legend(
            _coverage([_unresolved_row("fixture.exe", 30, 30)], 30, 30),
            _symbolizer(30, 30),
            False,
            ["--symbol-path"],
            by="line",
        )

        self.assertEqual(list(entry), ["uprof_db", "flags", "profile_symbolizer", "rows_drillable"])
        self.assertEqual(entry["uprof_db"]["named_samples"], 30)
        self.assertEqual(entry["uprof_db"]["scoped_samples"], 30)
        self.assertEqual(entry["flags"], ["--symbol-path"])
        self.assertEqual(entry["profile_symbolizer"]["resolved_samples"], 0)
        self.assertEqual(entry["profile_symbolizer"]["unresolved_samples"], 30)
        self.assertEqual(entry["profile_symbolizer"]["scoped_samples"], 30)
        self.assertFalse(entry["rows_drillable"])


class FailingSymbolizerTest(unittest.TestCase):
    """profile --by line/instruction when llvm-symbolizer exits 1 for the only module (#195).

    The rows stay unsymbolized, so the line must name the symbolizer channel with 0 resolved,
    the module's cause and the remedy, not a uProf DB share that says every sample resolves.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        session = Session(root, "session")
        session.module(1, FIXTURE, False).function(10, 1, "fixture_entry")
        session.sample(1, 10, 0x101B, 3)
        self.session = session.write()
        self.symbols = root / "symbols"
        self.symbols.mkdir()
        # A symbolizer that reports a supported version and fails every symbolization.
        if os.name == "nt":
            self.symbolizer = root / "llvm-symbolizer.bat"
            self.symbolizer.write_text(
                '@echo off\r\nif "%~1"=="--version" (\r\necho LLVM version 23.1.5\r\n'
                "exit /b 0\r\n)\r\nexit /b 1\r\n",
                encoding="ascii",
                newline="",
            )
            # The real llvm-readobj beside the stub shows that the image records no PDB, so the
            # failing PDB-less symbolizer pass runs; without it no symbolizer pass may run at all.
            readobj = uprof_native.find_sibling(uprof_native.find_symbolizer(), "llvm-readobj")
            if readobj is None:
                self.skipTest("llvm-readobj is required beside llvm-symbolizer or on PATH")
            shutil.copyfile(readobj, root / "llvm-readobj.exe")
        else:
            self.symbolizer = root / "llvm-symbolizer"
            self.symbolizer.write_text(
                '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "LLVM version 23.1.5"; '
                "exit 0; fi\nexit 1\n",
                encoding="ascii",
                newline="",
            )
            self.symbolizer.chmod(0o755)

    def test_line_and_instruction_name_the_failed_symbolizer_channel(self):
        environment = dict(os.environ, LLVM_SYMBOLIZER=str(self.symbolizer))
        for by in ("line", "instruction"):
            with self.subTest(by):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(TOOLS_DIR / "uprof.py"),
                        "profile",
                        "--session",
                        str(self.session),
                        "--by",
                        by,
                        "--symbol-path",
                        str(self.symbols),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                line = next(
                    line
                    for line in result.stdout.splitlines()
                    if line.startswith("SYMBOL_COVERAGE: ")
                )
                self.assertIn("uProf DB 3/3 samples (100.0%) resolve to a named function", line)
                self.assertIn(
                    "profile symbolizer 0/3 samples (0.0%) resolve below module/RVA "
                    "(flags: --symbol-path)",
                    line,
                )
                self.assertIn("llvm-symbolizer exited 1", line)
                self.assertIn(NO_NAMED_STOP, line)
                self.assertIn("remedy: pass profile --symbol-path", line)


class ProcessCoverageStopTest(unittest.TestCase):
    """In process scope the stop names the NEXT call, profile --by module, not --by function."""

    def field(self, named, by):
        rows = [_unresolved_row("a.dll", 10, named)]
        return uprof_query._coverage_field(
            None, {}, None, False, [], _coverage(rows, 10, named), by
        )

    def test_process_scope_points_at_the_module_call(self):
        for named in (0, 5):
            with self.subTest(named=named):
                text = self.field(named, "process")[0][1]
                share = f"{100.0 * (10 - named) / 10:.1f}"
                self.assertIn(f"unnamed samples by module: a.dll {10 - named}/10 ({share}%)", text)
                self.assertTrue(text.endswith(PROCESS_STOP), text)
                self.assertNotIn("unresolved", text)

    def test_module_scope_keeps_the_function_stop(self):
        text = self.field(0, "module")[0][1]
        self.assertIn("a.dll 10/10 (100.0% unresolved, no source names)", text)
        self.assertIn(NO_NAMED_STOP, text)
        self.assertIn(UNRESOLVED_STOP, self.field(5, "module")[0][1])


if __name__ == "__main__":
    unittest.main()
