"""`profile --by module` answers the module rollup without ad-hoc SQL against cpu.db.

The rows carry samples, the shares, `Module.isSystemModule` and `cpu_seconds`, so the third-party
rule and the absolute CPU time are read from supported output (issues #25, #26). The NEXT chain is
the two-call path process -> module -> instruction rather than every granularity.

Run from this directory: python -m unittest test_module_profile -v
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

TOOLS_DIR = Path(__file__).resolve().parent

WORKLOAD = "c:\\app\\workload.exe"
NTDLL = "c:\\windows\\system32\\ntdll.dll"
HELPER = "c:\\app\\helper.dll"
WORKLOAD_KEY = "c:/app/workload.exe"
NTDLL_KEY = "ntdll.dll"
HELPER_KEY = "c:/app/helper.dll"

COLUMNS = ["module", "samples", "sample_share", "time_share", "tag_to_retire_share",
           "isSystemModule", "cpu_seconds"]


def _meta(cycle_controlled):
    return {
        "translateCommand": "AMDuProfCLI.exe translate -i session --ascii ibsop-event-dump",
        "collection": {
            "schedule": {"elapsed": 10_000},
            "events": {"ibsEvents": {"op": True, "fetch": False, "opInterval": 4_700_000,
                                     "opCycleCount": cycle_controlled}},
            "target": {"systemWide": True},
        },
        "auxiliary": {"ibsOpSampling": True},
    }


class ModuleProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def session(self, name="cycle", flags=True, cycle_controlled=True, modules=None):
        session = self.root / name
        if session.is_dir():
            return session
        session.mkdir()
        (session / "session.uprof").write_text(json.dumps(_meta(cycle_controlled)),
                                               encoding="utf-8")
        modules = modules or [(1, WORKLOAD, False), (2, NTDLL, True), (3, HELPER, False)]
        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            flag_column = ", isSystemModule BOOLEAN" if flags else ""
            connection.execute(f"CREATE TABLE Module (id UINTEGER, path VARCHAR{flag_column})")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                               "name VARCHAR)")
            connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                               "functionId UBIGINT, moduleRVA UBIGINT)")
            connection.execute("INSERT INTO Process VALUES (100, 'workload.exe')")
            for module_id, path, system in modules:
                if flags:
                    connection.execute("INSERT INTO Module VALUES (?, ?, ?)",
                                       [module_id, path, system])
                else:
                    connection.execute("INSERT INTO Module VALUES (?, ?)", [module_id, path])
        finally:
            connection.close()
        rows = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName,IbsTagToRetCtr"]
        rows += self._dump_rows()
        (session / "IbsOpDump.csv").write_text("\n".join(rows) + "\n", encoding="utf-8",
                                               newline="\n")
        return session

    @staticmethod
    def _dump_rows():
        """workload 3 samples, ntdll 2, helper 1; ntdll's dump name is a basename."""
        rows = []
        tick = 0
        for module, tags in ((WORKLOAD, (100, 200, 300)), ("ntdll.dll", (50, 150)),
                             (HELPER, (25,))):
            for tag in tags:
                tick += 1_000_000_000
                rows.append(f"{tick},0,1,100,4096,{module},{tag}")
        return rows

    def run_sql(self, session, interval="0.001"):
        variables = {
            "uprof_image": "%", "uprof_pid": "", "uprof_top": "0",
            "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
            "uprof_ibs_dump": str(session / "IbsOpDump.csv"),
            "uprof_event_column": "", "uprof_session_meta": str(session / "session.uprof"),
            "uprof_tag_available": "true", "uprof_symbols": "", "uprof_aliases": "",
            "uprof_pdbs": "", "uprof_sample_interval_s": interval,
        }
        with uprof_db.open_query(session / "cpu.db", [TOOLS_DIR / "views.sql"],
                                 variables) as connection:
            columns, rows = uprof_db.fetch(connection, TOOLS_DIR / "profile-module.sql")
        return columns, [dict(zip(columns, row)) for row in rows]

    def profile(self, session, by="module", *extra):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(session), "--by", by, *extra],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def document(self, session, by="module", *extra):
        return json.loads(self.profile(session, by, "--json", *extra))

    def test_module_rows_carry_shares_classification_and_cpu_seconds(self):
        columns, rows = self.run_sql(self.session())
        self.assertEqual(columns, COLUMNS)
        by_module = {row["module"]: row for row in rows}

        self.assertEqual([row["module"] for row in rows],
                         [WORKLOAD_KEY, NTDLL_KEY, HELPER_KEY])
        self.assertEqual([by_module[name]["samples"]
                          for name in (WORKLOAD_KEY, NTDLL_KEY, HELPER_KEY)], [3, 2, 1])
        self.assertAlmostEqual(sum(row["sample_share"] for row in rows), 1.0, places=12)
        self.assertAlmostEqual(sum(row["tag_to_retire_share"] for row in rows), 1.0, places=12)
        # False from the full-path join, True from the basename fallback for ntdll.
        self.assertIs(by_module[WORKLOAD_KEY]["isSystemModule"], False)
        self.assertIs(by_module[NTDLL_KEY]["isSystemModule"], True)
        self.assertIs(by_module[HELPER_KEY]["isSystemModule"], False)
        # cpu_seconds = samples x interval: three 1 ms samples are 3 ms.
        self.assertAlmostEqual(by_module[WORKLOAD_KEY]["cpu_seconds"], 0.003, places=12)
        self.assertAlmostEqual(by_module[HELPER_KEY]["cpu_seconds"], 0.001, places=12)

    def test_cpu_seconds_is_hidden_when_the_interval_is_work_not_time(self):
        columns, rows = self.run_sql(self.session(), interval="")
        self.assertEqual(columns, COLUMNS[:-1])
        self.assertNotIn("cpu_seconds", rows[0])

    def test_missing_system_module_column_is_null_not_a_classification(self):
        session = self.session("no-flags", flags=False)
        document = self.document(session)
        self.assertEqual(list(document["rows"][0]), COLUMNS)
        self.assertTrue(all(row["isSystemModule"] is None for row in document["rows"]))
        legend = document["legend"]
        self.assertNotIn("isSystemModule", legend["columns"])
        self.assertIn("isSystemModule", legend["absent"])
        self.assertIn("no Module.isSystemModule column", legend["absent"]["isSystemModule"])
        output = self.profile(session)
        self.assertIn("isSystemModule absent", output)

    def test_cycle_controlled_module_output_matches_the_interval_math(self):
        session = self.session()
        document = self.document(session)
        self.assertEqual(list(document["rows"][0]), COLUMNS)
        legend = document["legend"]
        sampler = uprof_session.derive_sampler(session)
        interval_s, _, _ = uprof_session.requested_interval(sampler)
        if interval_s is None:
            self.assertNotIn("cpu_seconds", legend["columns"])
            self.assertIn("cpu_seconds", legend["absent"])
            return
        self.assertIn("cpu_seconds", legend["columns"])
        self.assertNotIn("cpu_seconds", legend["absent"])
        for row in document["rows"]:
            self.assertAlmostEqual(row["cpu_seconds"], row["samples"] * interval_s, places=12)
        output = self.profile(session)
        self.assertIn("isSystemModule = ", output)
        self.assertIn("cpu_seconds = sampled CPU seconds", output)

    def test_op_count_module_output_omits_cpu_seconds_with_the_reason(self):
        session = self.session("op-count", cycle_controlled=False)
        document = self.document(session)
        legend = document["legend"]
        self.assertNotIn("cpu_seconds", legend["columns"])
        self.assertIn("work count", legend["absent"]["cpu_seconds"])
        self.assertNotIn("cpu_seconds", document["rows"][0])
        output = self.profile(session)
        self.assertIn("cpu_seconds absent (the requested interval is a work count", output)

    def test_next_is_the_two_call_module_chain(self):
        session = self.session()
        process_calls = self.document(session, "process")["legend"]["next"]
        self.assertEqual(len(process_calls), 2)
        self.assertIn("--by module", process_calls[0])
        self.assertIn("--by instruction", process_calls[-1])
        self.assertTrue(process_calls[-1].endswith("--json"))

        module_calls = self.document(session)["legend"]["next"]
        self.assertEqual(len(module_calls), 3)
        self.assertIn("--by function", module_calls[0])
        self.assertIn("--by region", module_calls[1])
        self.assertIn("--by instruction", module_calls[2])
        self.assertTrue(module_calls[2].endswith("--json"))

    def test_truncated_module_rollup_keeps_a_remainder_last(self):
        session = self.session()
        document = self.document(session, "module", "--top", "1")
        rows = document["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["module"], "<other modules>")
        self.assertIsNone(rows[-1]["isSystemModule"])
        self.assertIsNotNone(rows[-1]["cpu_seconds"])


if __name__ == "__main__":
    unittest.main()
