"""SANITY must judge the scoped population when a filter narrows the report.

The dump-wide late-coverage check cannot see that a --pid/--image scope stops sampling before
the capture does; these fixtures make one process stop early while another samples near the end.

Run from this directory: python -m unittest test_sanity_scope -v
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

TOOLS_DIR = Path(__file__).resolve().parent
ELAPSED_MS = 10_000
# 100 samples on the late CPU, so the dump-wide cadence estimate is 0.1 s.
LATE_SPACING_NS = 100_909_090
ALL_NS = "9223372036854775807"


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

    def write_session(self, *, system_wide):
        meta = {
            "translateCommand": ("AMDuProfCLI.exe translate -i session "
                                 "--ascii ibsop-event-dump"),
            "collection": {
                "schedule": {"elapsed": ELAPSED_MS, "profileDuration": 10},
                "target": {"systemWide": system_wide},
                "events": {"ibsEvents": {
                    "op": {"interval": 4_700_000},
                    "opInterval": 4_700_000,
                    "opCycleCount": True,
                }},
            },
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
                                                     encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main()
