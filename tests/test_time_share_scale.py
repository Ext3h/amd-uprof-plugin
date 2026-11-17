"""time_share is described as time only at process, thread or phase scale (issue #278).

In op-count and fetch mode, code interleaved within one sampling interval sees the same expected
gap per sample, so below process or phase scale the gap share equals the sample frequency share.
This fixture is a loop of F (10 ops per iteration) and G (1 op that holds the tag-to-retire
cycles) at uniform 1 ms gaps: time_share is 10:1 for F although G carries the cost. The output
must not call time_share "where time went" there, and must not tell the reader that a high
sample_share with a low time_share is frequency rather than impact.

Run from the repository root: python tests/run_tests.py test_time_share_scale
"""
# Spec: SPEC-15, SPEC-96, SPEC-105

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import duckdb
import uprof

MODULE = "loop.exe"
F_OFFSET = 4096
G_OFFSET = 8192
ITERATIONS = 50
SCALE = "time only at process, thread or phase scale"


def _rows():
    """(delta_tick, offset, tag): 10 F ops then 1 G op per iteration, 1 ms apart."""
    rows = []
    tick = 0
    for _ in range(ITERATIONS):
        for _ in range(10):
            tick += 1_000_000
            rows.append((tick, F_OFFSET, 100))
        tick += 1_000_000
        rows.append((tick, G_OFFSET, 60000))
    return rows


class TimeShareScaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _session(self, fetch):
        session = Path(self.tmp.name) / ("fetch" if fetch else "op")
        session.mkdir()
        events = {
            "op": not fetch,
            "fetch": fetch,
            "opInterval": 100_000,
            "fetchInterval": 100_000,
            "opCycleCount": False,
        }
        meta = {
            "translateCommand": "AMDuProfCLI.exe translate -i session --ascii "
            + ("ibsfetch-event-dump" if fetch else "ibsop-event-dump"),
            "collection": {
                "schedule": {"elapsed": ITERATIONS * 11 + 1},
                "events": {"ibsEvents": events},
                "target": {"systemWide": True},
            },
            "auxiliary": {"ibsOpSampling": not fetch},
        }
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        header = "DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"
        lines = [header if fetch else header + ",IbsTagToRetCtr"]
        for tick, offset, tag in _rows():
            row = f"{tick},0,100,100,{offset},{MODULE}"
            lines.append(row if fetch else f"{row},{tag}")
        name = "IbsFetchDump.csv" if fetch else "IbsOpDump.csv"
        (session / name).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        connection = duckdb.connect(str(session / "cpu.db"))
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
            connection.execute(r"INSERT INTO Process VALUES (100, 'c:\loop.exe')")
            connection.execute(f"INSERT INTO Module VALUES (1, '{MODULE}')")
            connection.execute("INSERT INTO Function VALUES (10, 1, 'F'), (20, 1, 'G')")
            connection.execute(
                f"INSERT INTO UnifiedSampleSeries VALUES (1, 10, {F_OFFSET}), (1, 20, {G_OFFSET})"
            )
        finally:
            connection.close()
        return session

    def _document(self, session, by):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = uprof.main(["profile", "--session", str(session), "--by", by, "--json"])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_op_count_function_view_states_the_time_share_scale(self):
        document = self._document(self._session(fetch=False), "function")
        rows = {row["function"]: row for row in document["rows"]}

        # The fixture's shape: time_share follows the 10:1 op frequency, the tag cycles sit on G.
        self.assertAlmostEqual(rows["F"]["time_share"], 10 / 11, delta=0.01)
        self.assertGreater(rows["G"]["tag_to_retire_share"], rows["F"]["tag_to_retire_share"])
        self.assertIn(f"time_share = gap share, {SCALE}", document["COLUMNS"])
        self.assertIn(SCALE, document["legend"]["columns"]["time_share"])

    def test_context_help_does_not_call_a_low_time_share_frequency(self):
        document = self._document(self._session(fetch=False), "instruction")

        self.assertNotIn("frequency, not an impact finding", document["context_help"]["next"])

    def test_fetch_ranking_does_not_claim_where_time_went(self):
        document = self._document(self._session(fetch=True), "function")
        impact = document["legend"]["impact"]

        self.assertEqual(impact["primary_measure"], "time_share")
        self.assertNotIn("where time went", document["READ"])
        self.assertIn(SCALE, document["READ"])
        self.assertNotIn("where time went", impact["primary_measure_reason"])
        self.assertIn(SCALE, impact["primary_measure_reason"])


if __name__ == "__main__":
    unittest.main()
