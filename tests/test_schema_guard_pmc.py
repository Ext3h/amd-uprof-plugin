"""PMC captures require UnifiedSampleSeries.processId and coreId."""
# Spec: SPEC-107

import unittest
from pathlib import Path

import duckdb

import uprof_db

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


class SchemaGuardPmcTest(unittest.TestCase):
    def _make_pmc_connection(self, include_core_id: bool = True, include_process_id: bool = True):
        connection = duckdb.connect(":memory:")
        connection.execute("CREATE TABLE Process(id UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE Module(id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)")
        connection.execute(
            "CREATE TABLE Function(id UBIGINT, moduleId UINTEGER, name VARCHAR, "
            "startOffset UBIGINT, size UBIGINT, srcfileId UINTEGER)"
        )

        cols = ["moduleId UINTEGER", "functionId UBIGINT", "moduleRVA UBIGINT", '"event#0x76" BIGINT']
        if include_core_id:
            cols.append("coreId UINTEGER")
        if include_process_id:
            cols.append("processId UINTEGER")

        connection.execute(f"CREATE TABLE UnifiedSampleSeries({', '.join(cols)})")

        variables = {
            "uprof_ibs_dump": "",
            "uprof_ibs_parquet": "",
            "uprof_symbols": "",
            "uprof_aliases": "",
            "uprof_pdbs": "",
            "uprof_functions": "",
            "uprof_event_column": "event#0x76",
            "uprof_primary_measure": "samples",
            "uprof_time_start": "0",
            "uprof_time_end": "1000000",
            "uprof_top": "20",
            "uprof_pid": "",
            "uprof_image": "%",
            "uprof_by": "process",
            "uprof_module": "",
            "uprof_function": "",
            "uprof_line": "",
            "uprof_instruction": "",
        }
        uprof_db.set_variables(connection, variables)
        return connection

    def test_schema_guard_reports_missing_core_id(self):
        connection = self._make_pmc_connection(include_core_id=False, include_process_id=True)
        try:
            with self.assertRaises(Exception) as ctx:
                uprof_db.run_script(connection, TOOLS_DIR / "views.sql")
            message = str(ctx.exception)
            self.assertIn("uProf export incomplete: missing column UnifiedSampleSeries.coreId", message)
            self.assertNotIn("Binder Error", message)
        finally:
            connection.close()

    def test_schema_guard_reports_missing_process_id(self):
        connection = self._make_pmc_connection(include_core_id=True, include_process_id=False)
        try:
            with self.assertRaises(Exception) as ctx:
                uprof_db.run_script(connection, TOOLS_DIR / "views.sql")
            message = str(ctx.exception)
            self.assertIn("uProf export incomplete: missing column UnifiedSampleSeries.processId", message)
            self.assertNotIn("Binder Error", message)
        finally:
            connection.close()

    def test_schema_guard_reports_both_missing(self):
        connection = self._make_pmc_connection(include_core_id=False, include_process_id=False)
        try:
            with self.assertRaises(Exception) as ctx:
                uprof_db.run_script(connection, TOOLS_DIR / "views.sql")
            message = str(ctx.exception)
            self.assertIn("missing column UnifiedSampleSeries.coreId, column UnifiedSampleSeries.processId", message)
            self.assertNotIn("Binder Error", message)
        finally:
            connection.close()

    def test_schema_guard_passes_when_all_present(self):
        connection = self._make_pmc_connection(include_core_id=True, include_process_id=True)
        try:
            uprof_db.run_script(connection, TOOLS_DIR / "views.sql")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
