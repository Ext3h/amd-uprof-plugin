"""`schema` states whether the export matches the contract the query surface reads.

The listing alone made the caller diff columns by eye; the guard in views.sql that profile runs
already names every missing table and column. schema runs that same guard for the capture's
mode: a complete export prints a CONTRACT: line, an incomplete one exits 2 naming the missing
items. The listing itself is unchanged.
Run from the repository root: python tests/run_tests.py test_schema_verdict
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import duckdb
except ImportError:  # the suite reports the skip reason
    duckdb = None

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
# The column name the sampler derives from encodedEventId 0x03000076 below.
EVENT_COLUMN = "event#0x3000076"
PMC_META = {
    "collection": {
        "events": {"pmcEvents": {"pmcEventList": [{
            "hwId": 118, "interval": 4_700_000, "isSampling": True,
            "user": True, "os": True, "encodedEventId": 0x03000076}]}},
        "target": {"systemWide": True},
    },
    "auxiliary": {},
}


@unittest.skipIf(duckdb is None, "duckdb is required; install duckdb==2.0.0")
class SchemaVerdictTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="uprof-schema-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def capture(self, function_columns="id UBIGINT, moduleId UINTEGER, name VARCHAR",
                meta=PMC_META):
        connection = duckdb.connect(str(self.root / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute(f"CREATE TABLE Function ({function_columns})")
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries (processId UINTEGER, threadId UINTEGER, "
                "moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT, coreId UINTEGER, "
                f'"{EVENT_COLUMN}" DOUBLE)')
        finally:
            connection.close()
        if meta is not None:
            (self.root / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        return self.root

    def schema(self, session):
        return subprocess.run([sys.executable, str(TOOLS_DIR / "uprof.py"), "schema",
                               "--session", str(session)],
                              capture_output=True, text=True, encoding="utf-8")

    def test_a_complete_export_prints_the_match_and_the_listing(self):
        result = self.schema(self.capture())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CONTRACT: ", result.stderr)
        self.assertIn("UnifiedSampleSeries", result.stdout)

    def test_the_verdict_names_the_pmc_mode_and_its_event_column(self):
        result = self.schema(self.capture())
        line = next(l for l in result.stderr.splitlines() if l.startswith("CONTRACT: "))
        self.assertIn("PMC", line)
        self.assertIn(EVENT_COLUMN, line)

    def test_an_ibs_verdict_says_the_dump_is_checked_by_profile(self):
        meta = {"collection": {"events": {"ibsEvents": {"op": True, "fetch": False,
                                                        "opInterval": 1_000_000}},
                               "target": {"systemWide": True}},
                "auxiliary": {"ibsOpSampling": True}}
        result = self.schema(self.capture(meta=meta))
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(l for l in result.stderr.splitlines() if l.startswith("CONTRACT: "))
        self.assertIn("IBS", line)
        self.assertIn("dump is checked when profile reads it", line)

    def test_an_unknown_mode_claims_only_the_common_tables(self):
        result = self.schema(self.capture(meta=None))
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(l for l in result.stderr.splitlines() if l.startswith("CONTRACT: "))
        self.assertIn("mode unknown", line)
        self.assertIn("only the tables and columns every mode reads", line)

    def test_a_missing_column_is_named_and_fails(self):
        result = self.schema(self.capture("id UBIGINT, moduleId UINTEGER"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("uProf export incomplete: missing column Function.name", result.stderr)
        self.assertNotIn("CONTRACT: ", result.stderr)


if __name__ == "__main__":
    unittest.main()
