"""The event-column guard is case-insensitive; verify the COLUMNS selectors are too."""

import unittest
from pathlib import Path

import duckdb
import uprof_db

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


class EventColumnCaseTest(unittest.TestCase):
    def test_event_column_selector_matches_case_insensitively(self):
        connection = duckdb.connect(":memory:")
        connection.execute("CREATE TABLE Process(id UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE Module(id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)")
        connection.execute(
            "CREATE TABLE Function(id UBIGINT, moduleId UINTEGER, name VARCHAR, "
            "startOffset UBIGINT, size UBIGINT, srcfileId UINTEGER)"
        )
        connection.execute(
            "CREATE TABLE UnifiedSampleSeries("
            "moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT, "
            'processId UINTEGER, coreId UINTEGER, "event#0X76" BIGINT)'
        )
        connection.execute("INSERT INTO UnifiedSampleSeries VALUES (1, 1, 4096, 100, 0, 42)")
        connection.execute("INSERT INTO Process VALUES (100, 'c:\\capture\\test.exe')")
        connection.execute("INSERT INTO Module VALUES (1, 'test.dll', false)")
        connection.execute("INSERT INTO Function VALUES (1, 1, 'test_fn', 4096, 64, 0)")

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

        # Before the fix, run_script fails with a BinderException on v_scoped / v_capture_modules.
        uprof_db.run_script(connection, TOOLS_DIR / "views.sql")

        rows = connection.execute("SELECT samples FROM v_scoped").fetchall()
        self.assertEqual(rows, [(42,)])

        capture_rows = connection.execute("SELECT samples FROM v_capture_modules").fetchall()
        self.assertEqual(capture_rows, [(42,)])

        connection.close()

    def test_event_column_selector_matches_when_variable_is_uppercase(self):
        connection = duckdb.connect(":memory:")
        connection.execute("CREATE TABLE Process(id UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE Module(id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)")
        connection.execute(
            "CREATE TABLE Function(id UBIGINT, moduleId UINTEGER, name VARCHAR, "
            "startOffset UBIGINT, size UBIGINT, srcfileId UINTEGER)"
        )
        connection.execute(
            "CREATE TABLE UnifiedSampleSeries("
            "moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT, "
            'processId UINTEGER, coreId UINTEGER, "event#0x76" BIGINT)'
        )
        connection.execute("INSERT INTO UnifiedSampleSeries VALUES (1, 1, 4096, 100, 0, 99)")
        connection.execute("INSERT INTO Process VALUES (100, 'c:\\capture\\test.exe')")
        connection.execute("INSERT INTO Module VALUES (1, 'test.dll', false)")
        connection.execute("INSERT INTO Function VALUES (1, 1, 'test_fn', 4096, 64, 0)")

        variables = {
            "uprof_ibs_dump": "",
            "uprof_ibs_parquet": "",
            "uprof_symbols": "",
            "uprof_aliases": "",
            "uprof_pdbs": "",
            "uprof_functions": "",
            "uprof_event_column": "event#0X76",
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
        uprof_db.run_script(connection, TOOLS_DIR / "views.sql")

        rows = connection.execute("SELECT samples FROM v_scoped").fetchall()
        self.assertEqual(rows, [(99,)])

        capture_rows = connection.execute("SELECT samples FROM v_capture_modules").fetchall()
        self.assertEqual(capture_rows, [(99,)])

        connection.close()


if __name__ == "__main__":
    unittest.main()
