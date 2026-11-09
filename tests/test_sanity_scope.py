"""SANITY must judge the scoped population when a filter narrows the report.

The dump-wide late-coverage check cannot see that a --pid/--image scope stops sampling before
the capture does; these fixtures make one process stop early while another samples near the end.

Run from the repository root: python tests/run_tests.py test_sanity_scope
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

import uprof_db
import uprof_query

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
ELAPSED_MS = 10_000
# 100 samples on the late CPU, so the dump-wide cadence estimate is 0.1 s.
LATE_SPACING_NS = 100_909_090


class SanityScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Path(self.tmp.name) / "session"
        self.session.mkdir()
        self.write_session(system_wide=True)
        self.write_dump()
        self.write_cpu_db()

    def tearDown(self):
        self.tmp.cleanup()

    def write_session(self, *, system_wide, schedule=None):
        collection = {
            "schedule": ({"elapsed": ELAPSED_MS, "profileDuration": 10}
                         if schedule is None else schedule),
            "events": {"ibsEvents": {
                "op": {"interval": 4_700_000},
                "opInterval": 4_700_000,
                "opCycleCount": True,
            }},
        }
        if system_wide is not None:
            collection["target"] = {"systemWide": system_wide}
        meta = {
            "translateCommand": ("AMDuProfCLI.exe translate -i session "
                                 "--ascii ibsop-event-dump"),
            "collection": collection,
            "auxiliary": {"ibsOpSampling": True},
        }
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")

    def write_dump(self):
        """PID 100 stops at 1.9 s; PID 200 samples to 9.99 s of the 10 s capture."""
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        for i in range(20):
            lines.append(f"{i * 100_000_000},0,{100},{100},4096,workload.exe")
        for i in range(100):
            lines.append(f"{i * LATE_SPACING_NS},1,{200},{200},8192,background.exe")
        (self.session / "IbsOpDump.csv").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8", newline="\n")

    def write_cpu_db(self):
        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE UnifiedSampleSeries("
                               "moduleId INTEGER, functionId INTEGER, moduleRVA BIGINT)")
            connection.execute("CREATE TABLE Process(id INTEGER, name VARCHAR)")
            connection.execute(r"INSERT INTO Process VALUES "
                               r"(100, 'c:\workload.exe'), (200, 'c:\background.exe')")
            connection.execute("CREATE TABLE Module(id INTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function(id INTEGER, name VARCHAR)")
        finally:
            connection.close()

    def sanity_line(self, *extra):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(self.session), "--by", "process", *extra],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return next(line for line in result.stdout.splitlines()
                    if line.startswith("SANITY:"))

    def test_scoped_process_that_stops_early_warns(self):
        line = self.sanity_line("--pid", "100")

        self.assertIn("dump-wide", line)
        self.assertIn("scoped coverage (--pid 100): WARN", line)
        self.assertIn("8.100 s before the scope window end at 10.000 s", line)

    def test_scoped_process_with_a_late_sample_is_ok(self):
        line = self.sanity_line("--pid", "200")

        self.assertIn("scoped coverage (--pid 200): OK", line)
        self.assertIn("0.010 s before the scope window end at 10.000 s", line)
        self.assertNotIn("WARN", line)

    def test_image_scope_warns_like_a_pid_scope(self):
        line = self.sanity_line("--image", "%workload%")

        self.assertIn("scoped coverage (--image %workload%): WARN", line)

    def test_time_filter_bounds_the_scoped_window(self):
        line = self.sanity_line("--pid", "100", "--time-filter", "0:5")

        self.assertIn("scoped coverage (--pid 100 --time-filter 0.000:5.000 s): WARN", line)
        self.assertIn("3.100 s before the scope window end at 5.000 s", line)

    def test_unfiltered_run_makes_no_scoped_claim(self):
        line = self.sanity_line()

        self.assertIn("dump-wide", line)
        self.assertNotIn("scoped", line)

    def test_attached_capture_keeps_its_attached_verdict(self):
        self.write_session(system_wide=False)

        line = self.sanity_line("--pid", "100")

        self.assertIn("OK (attached", line)
        self.assertNotIn("scoped", line)

    def test_missing_system_wide_is_treated_as_attached(self):
        # uprof_session.system_wide() counts unknown metadata as attached; ACHIEVED then labels
        # the context a thread, so the scoped population check must not run either.
        self.write_session(system_wide=None)

        line = self.sanity_line("--pid", "100")

        self.assertNotIn("scoped", line)

    def test_start_only_time_filter_still_gets_a_scoped_verdict(self):
        # The end is the default sentinel, so the window ends at the recorded span end.
        line = self.sanity_line("--pid", "200", "--time-filter", "5:9223372036.854775807")

        self.assertIn("scoped coverage (--pid 200 --time-filter 5.000:9223372036.855 s): OK",
                      line)
        self.assertIn("0.010 s before the scope window end at 10.000 s", line)

    def test_empty_time_filter_is_not_a_scope_filter(self):
        line = self.sanity_line("--time-filter", "")

        self.assertNotIn("scoped", line)

    def test_scope_uses_its_own_cadence_not_the_dump_wide_one(self):
        # PID 100 is sampled every 0.5 s and stops one cadence before the window end; PID 200
        # keeps the dump-wide cadence at 0.1 s. The 0.5 s gap is normal for PID 100.
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        for i in range(100):
            lines.append(f"{i * 100_000_000},0,200,200,8192,background.exe")
        for i in range(20):
            lines.append(f"{i * 500_000_000},1,100,100,4096,workload.exe")
        (self.session / "IbsOpDump.csv").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8", newline="\n")

        line = self.sanity_line("--pid", "100", "--time-filter", "0:10")

        self.assertIn("scoped coverage (--pid 100 --time-filter 0.000:10.000 s): OK", line)
        self.assertIn("0.500 s before the scope window end at 10.000 s", line)
        self.assertNotIn("WARN", line)

    def test_time_filter_judges_the_scope_without_a_recorded_duration(self):
        # The time filter fixes the scope window even when session.uprof records no span.
        self.write_session(system_wide=True, schedule={})

        line = self.sanity_line("--pid", "100", "--time-filter", "0:10")

        self.assertIn("WARN (dump-wide across all processes): session.uprof has no usable duration",
                      line)
        self.assertIn("scoped coverage (--pid 100 --time-filter 0.000:10.000 s): WARN", line)

    def test_pmc_scope_has_no_verdict_without_timestamps(self):
        # A hand run can read v_scope_sanity for a PMC capture; its samples carry no DeltaTick,
        # so a zero gap would claim coverage that was never measured.
        pmc = Path(self.tmp.name) / "pmc"
        pmc.mkdir()
        (pmc / "session.uprof").write_text(json.dumps({
            "translateCommand": "AMDuProfCLI.exe translate -i session",
            "collection": {"schedule": {"elapsed": ELAPSED_MS},
                           "target": {"systemWide": True}},
        }), encoding="utf-8")
        connection = duckdb.connect(str(pmc / "cpu.db"))
        try:
            connection.execute(
                'CREATE TABLE UnifiedSampleSeries(moduleId INTEGER, functionId INTEGER, '
                'moduleRVA BIGINT, coreId INTEGER, processId INTEGER, "event#0x1" BIGINT)')
            connection.execute("CREATE TABLE Process(id INTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module(id INTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function(id INTEGER, name VARCHAR)")
            connection.execute(r"INSERT INTO Process VALUES (100, 'c:\workload.exe')")
            connection.execute(r"INSERT INTO Module VALUES (10, 'c:\workload.exe')")
            connection.execute(
                'INSERT INTO UnifiedSampleSeries VALUES (10, 1, 4096, 0, 100, 5)')
        finally:
            connection.close()
        variables = {
            "uprof_image": "%", "uprof_pid": "100", "uprof_top": "20",
            "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
            "uprof_ibs_dump": "", "uprof_event_column": "event#0x1",
            "uprof_session_meta": str(pmc / "session.uprof"),
            "uprof_tag_available": "", "uprof_symbols": "", "uprof_aliases": "",
        }

        with uprof_db.open_query(pmc / "cpu.db", [TOOLS_DIR / "views.sql"],
                                 variables) as connected:
            message = connected.sql("SELECT message FROM v_scope_sanity").fetchall()[0][0]

        self.assertIsNone(message)

    def test_scoped_verdict_does_not_rescan_the_dump(self):
        variables = {
            "uprof_image": "%", "uprof_pid": "", "uprof_top": "20",
            "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
            "uprof_ibs_dump": str(self.session / "IbsOpDump.csv"),
            "uprof_event_column": "",
            "uprof_session_meta": str(self.session / "session.uprof"),
            "uprof_tag_available": "", "uprof_symbols": "", "uprof_aliases": "",
        }

        with uprof_db.open_query(self.session / "cpu.db", [TOOLS_DIR / "views.sql"],
                                 variables) as connected:
            plan = connected.sql("EXPLAIN SELECT message FROM v_scope_sanity").fetchall()[0][1]

        # The dump is materialized when views.sql loads, so the scoped verdict must contain no
        # CSV scan of its own, and it must scan the materialized table exactly once. Reading
        # v_dump_sanity for the span instead would add its two full dump scans (weighted_all and
        # per_cpu) for every filtered profile.
        self.assertNotIn("READ_CSV", plan)
        self.assertEqual(plan.count("v_dump"), 1, plan)


class ScopedQueryTest(unittest.TestCase):
    """The scoped view is only queried when the view can answer for this capture."""

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Connection:
        def __init__(self, scoped_message):
            self.queries = []
            self.scoped_message = scoped_message

        def sql(self, text):
            self.queries.append(text)
            if "v_scope_sanity" in text:
                return ScopedQueryTest._Rows([(self.scoped_message,)])
            return ScopedQueryTest._Rows([("dump-wide", 7)])

    def test_attached_capture_does_not_read_the_scoped_view(self):
        connection = self._Connection("scoped")

        message, tag_values = uprof_query._dump_sanity(connection, True, True, False)

        self.assertEqual(message, "dump-wide")
        self.assertEqual(tag_values, 7)
        self.assertFalse(any("v_scope_sanity" in query for query in connection.queries))

    def test_system_wide_capture_appends_the_scoped_verdict(self):
        connection = self._Connection("scoped coverage: OK")

        message, _ = uprof_query._dump_sanity(connection, True, True, True)

        self.assertEqual(message, "dump-wide; scoped coverage: OK")


if __name__ == "__main__":
    unittest.main()
