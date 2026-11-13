"""SYMBOL_COVERAGE discloses per-module function-name coverage before a drill-down.

A module whose samples carry no non-empty Function.name cannot be resolved below module/RVA.
The line states the aggregate resolved share and the unresolved count per module, including a
module that is absent from UnifiedSampleSeries entirely (function_id NULL). Without it a user
reconstructs PE function boundaries by hand, which is the detour issue #27 records.

Run from the repository root: python tests/run_tests.py test_symbol_coverage
"""
# Spec: SPEC-63, SPEC-67

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

import uprof_query

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

NO_NAMED_STOP = ("a module with no source names is covered by derived function ranges: use "
                 "profile --by function, which derives boundaries from the binary (.pdata, PE "
                 "exports, PDB publics) with LLVM; do not write a PE parser, map .pdata by hand "
                 "or disassemble")
UNRESOLVED_STOP = ("unresolved samples are covered by derived function ranges: use profile --by "
                   "function, which derives boundaries from the binary (.pdata, PE exports, PDB "
                   "publics) with LLVM; do not write a PE parser, map .pdata by hand or "
                   "disassemble")


def _dump_rows():
    """good 4, nostrings 3, partial 2 named + 1 anonymous, unknown 2 rows."""
    rows = [(index * 1_000_000_000, GOOD, GOOD_OFFSET) for index in range(4)]
    rows += [(4_000_000_000 + index * 1_000_000_000, NO_STRINGS, NO_STRINGS_OFFSET)
             for index in range(3)]
    rows += [(7_000_000_000 + index * 1_000_000_000, PARTIAL, PARTIAL_NAMED_OFFSET)
             for index in range(2)]
    rows += [(9_000_000_000, PARTIAL, PARTIAL_ANON_OFFSET)]
    rows += [(10_000_000_000 + index * 1_000_000_000, UNKNOWN, UNKNOWN_OFFSET)
             for index in range(2)]
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
            "translateCommand": ("AMDuProfCLI.exe translate -i session "
                                 "--ascii ibsop-event-dump"),
            "collection": {
                "schedule": {"elapsed": 11_000},
                "events": {"ibsEvents": {
                    "op": {"interval": 4_700_000},
                    "opInterval": 4_700_000,
                    "opCycleCount": True,
                }},
                "target": {"systemWide": system_wide,
                           "attachedProcesses": [{"processId": 100}]},
            },
            "auxiliary": {"ibsOpSampling": True},
        }
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")

    def _write_dump(self):
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        processes = {GOOD: 100, NO_STRINGS: 200, PARTIAL: 300, UNKNOWN: 400}
        for tick, module, offset in _dump_rows():
            lines.append(f"{tick},0,1,{processes[module]},{offset},{module}")
        (self.session / "IbsOpDump.csv").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8", newline="\n")

    def _write_cpu_db(self):
        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                               "name VARCHAR)")
            connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                               "functionId UBIGINT, moduleRVA UBIGINT)")
            connection.execute("INSERT INTO Process VALUES (100, 'good.exe'), "
                               "(200, 'nostrings.exe'), (300, 'partial.exe'), "
                               "(400, 'unknown.exe')")
            connection.execute(f"INSERT INTO Module VALUES (1, '{GOOD}'), (2, '{NO_STRINGS}'), "
                               f"(3, '{PARTIAL}'), (4, '{UNKNOWN}')")
            # Module 2 has a function row with a blank name; module 4 has no mapping at all.
            connection.execute("INSERT INTO Function VALUES (10, 1, 'good_fn'), "
                               "(20, 2, ''), (30, 3, 'partial_fn')")
            connection.execute(f"INSERT INTO UnifiedSampleSeries VALUES "
                               f"(1, 10, {GOOD_OFFSET}), (2, 20, {NO_STRINGS_OFFSET}), "
                               f"(3, 30, {PARTIAL_NAMED_OFFSET})")
        finally:
            connection.close()

    def profile(self, *extra):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(self.session), "--by", "function", *extra],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def coverage(self, *extra):
        document = json.loads(self.profile("--json", *extra))
        return document.get("SYMBOL_COVERAGE")

    def test_aggregate_and_per_module_unresolved_share(self):
        text = self.coverage()

        self.assertTrue(text.startswith(
            "uProf DB 6/12 samples (50.0%) resolve to a named function; rows named 6/12 samples "
            "(50.0%), derived 0/12 samples (0.0%) from a synthetic binary range, unresolved "
            "6/12 samples (50.0%) (<no symbol>)"), text)
        self.assertIn(f"{NO_STRINGS}: module file not available: {NO_STRINGS} is not on this "
                      "host", text)
        self.assertIn(f"{PARTIAL}: module file not available: {PARTIAL} is not on this host",
                      text)
        self.assertIn(f"{UNKNOWN}: module file not available: {UNKNOWN} is not on this host",
                      text)
        self.assertIn("no named or derived range covers these samples", text)

    def test_line_appears_in_markdown_and_json(self):
        output = self.profile()
        line = next(line for line in output.splitlines()
                    if line.startswith("SYMBOL_COVERAGE: "))
        self.assertEqual(line.removeprefix("SYMBOL_COVERAGE: "), self.coverage())

    def test_scope_narrows_coverage_and_empty_scope_has_none(self):
        self.assertEqual(
            self.coverage("--pid", "100"),
            "uProf DB 4/4 samples (100.0%) resolve to a named function; rows named 4/4 "
            "samples (100.0%), derived 0/4 samples (0.0%) from a synthetic binary range, "
            "unresolved 0/4 samples (0.0%) (<no symbol>); every scoped sample is attributed "
            "below module/RVA; a derived row is actionable at its hot RVA")
        self.assertIsNone(self.coverage("--pid", "999"))

    def test_process_granularity_also_discloses_coverage(self):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(self.session), "--by", "process"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SYMBOL_COVERAGE: 6/12 samples (50.0%) resolve to a named function;",
                      result.stdout)

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

        self.assertEqual(list(entry),
                         ["uprof_db", "flags", "profile_symbolizer", "rows_drillable"])
        self.assertEqual(entry["uprof_db"]["named_samples"], 6)
        self.assertEqual(entry["uprof_db"]["scoped_samples"], 12)
        self.assertEqual(entry["profile_symbolizer"], {
            "source": "the emitted rows: llvm-symbolizer names plus derived PE ranges",
            "named_samples": 6,
            "derived_samples": 0,
            "resolved_samples": 6,
            "unresolved_samples": 6,
            "scoped_samples": 12,
        })
        self.assertFalse(entry["rows_drillable"])
        self.assertEqual(entry["flags"], [])


def _coverage(rows, total, named):
    return {"rows": rows, "total": total, "named": named}


def _symbolizer(scoped, unresolved):
    return {"scoped_samples": scoped, "unresolved_samples": unresolved,
            "unresolved": 0, "unresolved_pct": 0.0}


def _unresolved_row(module, samples, named):
    return {"module_name": module, "samples": samples, "named_samples": named,
            "scoped_samples": samples, "named_total": named}


class CoverageChannelsTest(unittest.TestCase):
    """The DB channel and the symbolizer channel are reported separately, and the stop line follows
    the channel the profile rows actually use."""

    def test_explicit_pdb_does_not_pair_resolved_rows_with_a_stop_line(self):
        text = uprof_query._coverage_field(
            None, {}, _symbolizer(5575, 0), True, ["--symbol-path"],
            _coverage([_unresolved_row("a.dll", 5575, 0)], 5575, 0))[0][1]

        self.assertIn("uProf DB 0/5575 samples (0.0%) resolve to a named function", text)
        self.assertIn("profile symbolizer 5575/5575 samples (100.0%) resolve below module/RVA "
                      "(flags: --symbol-path)", text)
        self.assertIn("every scoped sample is drillable below module/RVA", text)
        self.assertNotIn(NO_NAMED_STOP, text)
        self.assertNotIn(UNRESOLVED_STOP, text)

    def test_partial_symbolizer_resolution_keeps_the_boundary_and_names_the_flag(self):
        text = uprof_query._coverage_field(
            None, {"b.dll": "no PDB found"}, _symbolizer(10, 4), True, ["--symbol-path"],
            _coverage([_unresolved_row("b.dll", 10, 0)], 10, 0))[0][1]

        self.assertIn("uProf DB 0/10 samples (0.0%) resolve to a named function", text)
        self.assertIn("profile symbolizer 6/10 samples (60.0%) resolve below module/RVA "
                      "(flags: --symbol-path)", text)
        self.assertIn("cause by module: b.dll: no PDB found", text)
        self.assertIn(UNRESOLVED_STOP, text)
        self.assertIn("remedy: pass profile --symbol-path", text)

    def test_agreeing_channels_without_flags_keep_the_original_line(self):
        text = uprof_query._coverage_field(
            None, {}, _symbolizer(10, 0), True, [],
            _coverage([_unresolved_row("a.dll", 10, 10)], 10, 10))[0][1]

        self.assertEqual(text, "10/10 samples (100.0%) resolve to a named function; "
                               "no module has unresolved samples")

    def test_symbolizer_superset_without_flags_still_names_both_channels(self):
        text = uprof_query._coverage_field(
            None, {}, _symbolizer(10, 0), True, [],
            _coverage([_unresolved_row("a.dll", 10, 0)], 10, 0))[0][1]

        self.assertIn("uProf DB 0/10", text)
        self.assertIn("profile symbolizer 10/10", text)
        self.assertIn("every scoped sample is drillable", text)
        self.assertNotIn(NO_NAMED_STOP, text)
        self.assertNotIn(UNRESOLVED_STOP, text)

    def test_json_legend_records_channel_counts_flags_and_drillability(self):
        entry = uprof_query._symbol_coverage_legend(
            _coverage([_unresolved_row("a.dll", 5575, 0)], 5575, 0),
            _symbolizer(5575, 0), True, ["--symbol-path"])

        self.assertEqual(list(entry),
                         ["uprof_db", "flags", "profile_symbolizer", "rows_drillable"])
        self.assertEqual(entry["uprof_db"]["named_samples"], 0)
        self.assertEqual(entry["uprof_db"]["scoped_samples"], 5575)
        self.assertEqual(entry["flags"], ["--symbol-path"])
        self.assertEqual(entry["profile_symbolizer"]["resolved_samples"], 5575)
        self.assertEqual(entry["profile_symbolizer"]["unresolved_samples"], 0)
        self.assertEqual(entry["profile_symbolizer"]["scoped_samples"], 5575)
        self.assertTrue(entry["rows_drillable"])


if __name__ == "__main__":
    unittest.main()
