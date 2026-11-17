"""READ states the top row's sampling uncertainty in every capture mode (issue #201).

Only timer captures printed the top-row clause, so an IBS or PMC answer quoted a 9-sample share at
full precision with nothing marking it coarse. The clause (top row share, 95% interval, n) is now
part of READ for an IBS dump and a PMC capture as well.

Run from the repository root: python tests/run_tests.py test_top_row_every_mode
"""
# Spec: SPEC-15, SPEC-106

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import duckdb
import uprof

EVENT_COLUMN = "event#0x3000076"


def _tables(connection, sample_columns=""):
    connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
    connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
    connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)")
    connection.execute(
        f"CREATE TABLE UnifiedSampleSeries ({sample_columns}moduleId UINTEGER, "
        "functionId UBIGINT, moduleRVA UBIGINT)"
    )
    connection.execute(r"INSERT INTO Process VALUES (100, 'c:\app\workload.exe')")


class TopRowEveryModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _ibs_session(self):
        session = Path(self.tmp.name) / "ibs"
        session.mkdir()
        meta = {
            "translateCommand": "AMDuProfCLI.exe translate -i session --ascii ibsop-event-dump",
            "collection": {
                "schedule": {"elapsed": 1000},
                "events": {"ibsEvents": {"op": True, "opInterval": 100_000, "opCycleCount": False}},
                "target": {"systemWide": True},
            },
            "auxiliary": {"ibsOpSampling": True},
        }
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        for index in range(40):
            module = "alpha.exe" if index % 4 else "beta.dll"
            lines.append(f"{(index + 1) * 10_000_000},0,100,100,4096,{module}")
        (session / "IbsOpDump.csv").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )
        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            _tables(connection)
            connection.execute("INSERT INTO Module VALUES (1, 'alpha.exe'), (2, 'beta.dll')")
        finally:
            connection.close()
        return session

    def _pmc_session(self):
        session = Path(self.tmp.name) / "pmc"
        session.mkdir()
        event = {
            "hwId": 118,
            "interval": 4_700_000,
            "isSampling": True,
            "user": True,
            "os": False,
            "encodedEventId": 0x03000076,
        }
        meta = {
            "collection": {
                "schedule": {"elapsed": 1000},
                "events": {"pmcEvents": {"pmcEventList": [event]}},
                "target": {"systemWide": True},
            },
            "auxiliary": {},
        }
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            _tables(connection, "processId UINTEGER, threadId UINTEGER, coreId UINTEGER, ")
            connection.execute(
                f'ALTER TABLE UnifiedSampleSeries ADD COLUMN "{EVENT_COLUMN}" DOUBLE'
            )
            connection.execute(r"INSERT INTO Module VALUES (1, 'c:\app\workload.exe')")
            connection.execute("INSERT INTO Function VALUES (1, 1, 'main')")
            connection.execute(
                "INSERT INTO UnifiedSampleSeries (processId, threadId, coreId, moduleId, "
                f'functionId, moduleRVA, "{EVENT_COLUMN}") VALUES (100, 1, 0, 1, 1, 4096, 9)'
            )
        finally:
            connection.close()
        return session

    def _read(self, session, by):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = uprof.main(["profile", "--session", str(session), "--by", by, "--json"])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())["READ"]

    def test_ibs_dump_read_states_the_top_row_interval(self):
        read = self._read(self._ibs_session(), "module")

        self.assertIn("top alpha.exe 75.0% (95% CI ", read)
        self.assertIn("n=40, row2 separated)", read)

    def test_pmc_read_states_the_top_row_interval(self):
        read = self._read(self._pmc_session(), "process")

        self.assertIn("top c:\\app\\workload.exe 100.0% (95% CI ", read)
        self.assertIn("n=9)", read)


if __name__ == "__main__":
    unittest.main()
