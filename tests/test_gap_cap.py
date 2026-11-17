"""A DeltaTick gap is charged to its ending sample only up to the run-gap limit (issue #279).

An attached thread that waits off CPU leaves one long gap, and an uncapped estimator charges the
whole wait to the first sample after it. This fixture follows the shape from the issue: 999
gaps of 1 ms end in alpha, 9 gaps of 150 ms end in beta. Uncapped, beta's 9 samples carry 1.35 s
of the 2.349 s weight (57 %) and top `--by line`. Capped at 10x the requested interval (10 ms),
beta carries 9 x 10 ms, the 1.26 s above the cap is reported once in WEIGHTS, and line rows
follow the primary measure like every other view.

Run from the repository root: python tests/run_tests.py test_gap_cap
"""
# Spec: SPEC-80, SPEC-96

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb
import uprof
import uprof_collect

ALPHA = "alpha.exe"
BETA = "beta.exe"
ALPHA_OFFSET = 4096
BETA_OFFSET = 8192
MS = 1_000_000
# 4.7 M cycles at 4.7 GHz is a 1 ms interval, so the run-gap limit is 10 ms.
INTERVAL_CYCLES = 4_700_000
FREQUENCY_HZ = 4_700_000_000


def _dump_rows():
    """(delta_tick, module, offset): 999 gaps of 1 ms end in alpha, 9 of 150 ms in beta."""
    rows = [(0, ALPHA, ALPHA_OFFSET)]
    tick = 0
    for index in range(1, 1009):
        if index % 112 == 0:
            tick += 150 * MS
            rows.append((tick, BETA, BETA_OFFSET))
        else:
            tick += MS
            rows.append((tick, ALPHA, ALPHA_OFFSET))
    return rows


class GapCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Path(self.tmp.name) / "session"
        self.session.mkdir()
        rows = _dump_rows()
        self.assertEqual(sum(1 for row in rows if row[1] == BETA), 9)
        self.assertEqual(len(rows), 1009)
        self.assertEqual(rows[-1][0], 2349 * MS)
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        lines += [f"{tick},0,1,100,{offset},{module}" for tick, module, offset in rows]
        (self.session / "IbsOpDump.csv").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )
        self._write_cpu_db()

    def _write_meta(self, cycle_controlled):
        meta = {
            "translateCommand": "AMDuProfCLI.exe translate -i session --ascii ibsop-event-dump",
            "collection": {
                "schedule": {"elapsed": 2350},
                "events": {
                    "ibsEvents": {
                        "op": True,
                        "opInterval": INTERVAL_CYCLES,
                        "opCycleCount": cycle_controlled,
                    }
                },
                "target": {"systemWide": False, "attachedProcesses": [{"processId": 100}]},
            },
            "auxiliary": {"ibsOpSampling": True},
        }
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")

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
            connection.execute("INSERT INTO Process VALUES (100, 'c:\\capture\\workload.exe')")
            connection.execute(f"INSERT INTO Module VALUES (1, '{ALPHA}'), (2, '{BETA}')")
            connection.execute(
                "INSERT INTO Function VALUES (10, 1, 'alpha_fn'), (20, 2, 'beta_fn')"
            )
            connection.execute(
                f"INSERT INTO UnifiedSampleSeries VALUES "
                f"(1, 10, {ALPHA_OFFSET}), (2, 20, {BETA_OFFSET})"
            )
        finally:
            connection.close()

    def _document(self, by, cycle_controlled=True):
        """The JSON document with the core frequency pinned inside the CLI process."""
        self._write_meta(cycle_controlled)
        out = io.StringIO()
        with (
            mock.patch.object(
                uprof_collect,
                "_core_frequency",
                return_value=(FREQUENCY_HZ, f"{FREQUENCY_HZ / 1e6:g} MHz", "test"),
            ),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = uprof.main(["profile", "--session", str(self.session), "--by", by, "--json"])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def _rows_by_module(self, document):
        return {row["module"]: row for row in document["rows"]}

    def test_post_wait_samples_do_not_outrank_the_regular_ones_by_line(self):
        document = self._document("line")
        rows = [row for row in document["rows"] if row["module"] in (ALPHA, BETA)]

        self.assertEqual([row["module"] for row in rows], [ALPHA, BETA])
        # Capped: alpha 999 x 1 ms, beta 9 x 10 ms.
        by_module = self._rows_by_module(document)
        self.assertAlmostEqual(by_module[BETA]["time_share"], 90 / 1089, places=6)
        self.assertAlmostEqual(by_module[ALPHA]["time_share"], 999 / 1089, places=6)

    def test_excluded_gap_time_is_reported_once_in_weights(self):
        document = self._document("function")

        weights = document["WEIGHTS"]
        self.assertIn("capped at 10 ms (10x interval)", weights)
        self.assertIn("1.260 s in 9 longer gaps excluded", weights)
        self.assertNotIn("<excluded", json.dumps(document["rows"]))

    def test_op_count_capture_caps_at_ten_times_the_median_gap(self):
        # An op interval is not time, so the cap is 10x the thread's median gap (1 ms).
        document = self._document("function", cycle_controlled=False)

        weights = document["WEIGHTS"]
        self.assertIn("capped at 10x each context's median gap", weights)
        self.assertIn("1.260 s in 9 longer gaps excluded", weights)
        rows = {row["function"]: row for row in document["rows"]}
        self.assertAlmostEqual(rows["beta_fn"]["time_share"], 90 / 1089, places=6)


if __name__ == "__main__":
    unittest.main()
