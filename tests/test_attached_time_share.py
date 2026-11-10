"""Attached captures weight DeltaTick gaps per thread, not per CPU.

A thread that migrates between CPUs leaves a gap on every CPU it visited. The per-CPU lag
charges each of those overlapping intervals to the sample that ends it, so the weighted total can
exceed the capture span and reorder the modules. This fixture reproduces that shape: one attached
TID bounces between two CPUs, a sparse module (beta) ends the long per-CPU gaps, and a dense
module (alpha) ends most of the thread's own gaps.

The per-CPU estimator reports beta first and 40 s of weight over a 21 s span. Partitioning by the
thread gives exactly the 21 s span, puts alpha first, and matches the fixed-cycle sample share.

Run from the repository root: python tests/run_tests.py test_attached_time_share
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

import uprof_db
import uprof_session

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
SPAN_NS = 21_000_000_000
ALPHA_OFFSET = 4096
BETA_OFFSET = 8192
ALPHA = "alpha.exe"
BETA = "beta.exe"
TIME_MEANING = ("time share estimated from the gaps between per-sample timestamps within each "
                "thread (PID, TID), so a thread that migrates between CPUs is counted once; the "
                "gap includes off-CPU time and is charged to the ending sample, the first sample "
                "per thread is ignored (no gap before it), and NULL sorts last; for a "
                "cycle-controlled IBS capture sample_share (fixed cycles per sample) is the "
                "CPU-cycle share and the better ranking")


def _dump_rows():
    """(delta_tick, cpu, module, offset) rows for the migrating attached thread."""
    rows = [(0, 0, BETA, BETA_OFFSET), (1_000_000_000, 1, BETA, BETA_OFFSET),
            (10_000_000_000, 0, BETA, BETA_OFFSET),
            (11_000_000_000, 1, BETA, BETA_OFFSET),
            (20_000_000_000, 0, BETA, BETA_OFFSET),
            (21_000_000_000, 1, BETA, BETA_OFFSET)]
    rows += [(9_000_000_000 + index * 1_000_000, 1, ALPHA, ALPHA_OFFSET)
             for index in range(20)]
    rows += [(19_000_000_000 + index * 1_000_000, 1, ALPHA, ALPHA_OFFSET)
             for index in range(20)]
    return sorted(rows)


class AttachedTimeShareTest(unittest.TestCase):
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
                "schedule": {"elapsed": SPAN_NS // 1_000_000},
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

    def _write_rows(self, rows, *, tid=1, pid=100):
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        for tick, cpu, module, offset in rows:
            lines.append(f"{tick},{cpu},{tid},{pid},{offset},{module}")
        (self.session / "IbsOpDump.csv").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8", newline="\n")

    def _write_dump(self):
        self._write_rows(_dump_rows())

    def _write_cpu_db(self):
        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                               "name VARCHAR)")
            connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                               "functionId UBIGINT, moduleRVA UBIGINT)")
            connection.execute("INSERT INTO Process VALUES (100, 'workload.exe'), "
                               "(200, 'other.exe')")
            connection.execute(f"INSERT INTO Module VALUES (1, '{ALPHA}'), (2, '{BETA}')")
            connection.execute("INSERT INTO Function VALUES "
                               f"(10, 1, 'alpha_fn'), (20, 2, 'beta_fn')")
            connection.execute(f"INSERT INTO UnifiedSampleSeries VALUES "
                               f"(1, 10, {ALPHA_OFFSET}), (2, 20, {BETA_OFFSET})")
        finally:
            connection.close()

    def _variables(self):
        return {
            "uprof_image": "%", "uprof_pid": "", "uprof_top": "20",
            "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
            "uprof_ibs_dump": str(self.session / "IbsOpDump.csv"),
            "uprof_event_column": "",
            "uprof_session_meta": str(self.session / "session.uprof"),
            "uprof_tag_available": "false", "uprof_symbols": "", "uprof_aliases": "",
        }

    def _run_profile(self, *extra):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(self.session), "--by", "function", *extra],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def _run_profile_failure(self):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(self.session), "--by", "function"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2, result.stderr)
        return result

    def _profile(self):
        return json.loads(self._run_profile("--json"))

    def test_weight_total_is_the_capture_span(self):
        with uprof_db.open_query(self.session / "cpu.db", [TOOLS_DIR / "views.sql"],
                                 self._variables()) as connection:
            total = connection.sql("SELECT sum(weight) FROM v_dump_weighted").fetchall()[0][0]

        # The per-CPU estimator sums 40 s over a 21 s span; per thread the two CPUs' gaps do not
        # overlap, so the total is the thread's own span.
        self.assertAlmostEqual(float(total) / SPAN_NS, 1.0, delta=0.02)

    def test_time_share_ranking_matches_the_fixed_cycle_sample_share(self):
        document = self._profile()
        rows = document["rows"]

        self.assertEqual([row["function"] for row in rows], ["alpha_fn", "beta_fn"])
        # Cycle-controlled IBS gives every sample the same fixed cycle count, so sample_share is
        # the CPU-cycle share; time_share must not reverse it.
        self.assertGreater(rows[0]["sample_share"], rows[1]["sample_share"])
        self.assertGreater(rows[0]["time_share"], rows[1]["time_share"])

    def test_sanity_reports_thread_partitioning_not_a_cpu_capacity(self):
        document = self._profile()
        sanity = document["SANITY"]

        self.assertIn("OK (attached", sanity)
        self.assertIn("1 active TID (46/46 samples in the busiest)", sanity)
        self.assertIn("2 CPU(s) visited", sanity)
        self.assertIn("partitions gaps by (PID, TID)", sanity)
        # A single thread cannot have 2 CPUs' worth of capacity.
        self.assertNotIn("x 2 CPUs", sanity)

    def test_sanity_names_the_outside_samples_not_a_capacity(self):
        # The per-thread guard must not pre-empt the accurate out-of-span error: a thread whose
        # first sample is at 0 and last at 25 s against a 21 s span has samples outside it.
        self._write_rows(_dump_rows() + [(25_000_000_000, 0, BETA, BETA_OFFSET)])

        result = self._run_profile_failure()

        self.assertIn("1 TID(s) hold samples outside the recorded elapsed", result.stderr)
        self.assertIn("sample DeltaTick range 0.000..25.000 s", result.stderr)
        self.assertNotIn("one thread covers", result.stderr)

    def test_single_sample_threads_still_get_a_sanity_line(self):
        # One row per thread, so every weight is NULL and the message must not be built from a
        # NULL argument.
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        for index, tick in enumerate((0, 1_000_000_000, 2_000_000_000, 4_000_000_000,
                                      8_000_000_000), start=1):
            lines.append(f"{tick},0,{index},100,{ALPHA_OFFSET},{ALPHA}")
        (self.session / "IbsOpDump.csv").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8", newline="\n")

        document = self._profile()

        self.assertIn("OK (attached", document["SANITY"])
        self.assertIn("5 TID(s)", document["SANITY"])
        self.assertIn("0.000 s of thread time over 5 TIDs", document["SANITY"])

    def test_achieved_keeps_same_tid_in_different_processes_apart(self):
        # PID 100 samples once a second from 0 to 9 s; PID 200 reuses TID 4242 from 10.5 to 19.5 s.
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        for index in range(10):
            lines.append(f"{index * 1_000_000_000},0,4242,100,{ALPHA_OFFSET},{ALPHA}")
        for index in range(10):
            tick = 10_500_000_000 + index * 1_000_000_000
            lines.append(f"{tick},1,4242,200,{ALPHA_OFFSET},{ALPHA}")
        (self.session / "IbsOpDump.csv").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8", newline="\n")

        output = self._run_profile()

        # Two contexts of ten samples each, not one TID context of twenty.
        self.assertIn("2 active threads", output)

    def test_non_boolean_system_wide_is_attached_in_both_languages(self):
        # The SQL test is "lower(json_extract_string(...)) = 'true'"; Python must agree, or the
        # legend describes the CPU partition while the weights use the thread one.
        self._write_session(system_wide=1)

        document = self._profile()

        self.assertIn("within each thread (PID, TID)",
                      document["legend"]["columns"]["time_share"])
        self.assertIn("OK (attached", document["SANITY"])

    def test_legend_states_the_thread_partition_and_the_cycle_basis(self):
        legend = self._profile()["legend"]

        self.assertEqual(legend["columns"]["time_share"], TIME_MEANING)
        self.assertEqual(legend["denominators"]["time_share"], "total_weight")
        self.assertIn("within each thread (PID, TID)", legend["columns"]["time_share"])
        self.assertIn("counts fixed cycles per sample, so sample_share is the core-cycle share",
                      legend["impact"]["estimate"])

    def test_headers_state_the_thread_partition(self):
        output = self._run_profile()

        self.assertIn("time_share = per-thread time share (off-CPU gaps are charged to the "
                      "ending sample)", output)
        self.assertIn("sample_share orders the functions", output)
        self.assertIn("the partition is (PID, TID)", output)
        self.assertIn("exact DeltaTick-gap weights per (PID, TID)", output)
        self.assertIn("sample_share (fixed cycles per sample) is the CPU-cycle share and the "
                      "better ranking", output)


class CollectModeParseTest(unittest.TestCase):
    """verify_collect must read the recorded mode with the same rule as the profiler."""

    def write_session(self, tmp, value):
        session = Path(tmp) / "session"
        session.mkdir()
        meta = {
            "collection": {
                "events": {"ibsEvents": {"op": {"interval": 4_700_000},
                                          "opInterval": 4_700_000,
                                          "opCycleCount": True}},
                "target": {"systemWide": value,
                           "attachedProcesses": [{"processId": 100}]},
            },
            "auxiliary": {"ibsOpSampling": True},
        }
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        return session

    def test_string_false_records_an_attached_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.write_session(tmp, "false")

            self.assertFalse(uprof_session.system_wide(session))
            # bool("false") is True, so this value used to be read as system-wide here.
            uprof_session.verify_collect(session, "ibsop", 4_700_000, "cycles", False, [100])

    def test_string_true_records_a_system_wide_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.write_session(tmp, "true")

            self.assertTrue(uprof_session.system_wide(session))
            uprof_session.verify_collect(session, "ibsop", 4_700_000, "cycles", True, [])


if __name__ == "__main__":
    unittest.main()
