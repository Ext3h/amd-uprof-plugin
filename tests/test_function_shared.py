"""A function range that starts at a folded public start is reported as shared (#298).

Several public names at one address make a per-name share non-attributable. The line,
instruction and region views already mark such an address `shared`; the function view must do
the same for the range that starts there, and keep `unique` for a single-name start.

Run from the repository root: python tests/run_tests.py test_function_shared
"""
# Spec: SPEC-112

import tempfile
import unittest
from pathlib import Path

import duckdb

import uprof_db
import uprof_functions
import uprof_query
from uprof_functions import FunctionRange

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
MODULE = "c:/bin/a.dll"


class FunctionSharedTest(unittest.TestCase):
    def rows(self) -> dict[str, tuple]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            functions = uprof_functions.write_ranges_csv(
                root / "functions.csv",
                [
                    FunctionRange(MODULE, 0x1000, 0x1040, "pdb_public", "first"),
                    FunctionRange(MODULE, 0x1040, 0x1080, "pdb_public", "single"),
                ],
            )
            aliases = root / "aliases.csv"
            aliases.write_text(
                "module_name,rva,name_count,names\n"
                f"{MODULE},{0x1000},2,first | second\n"
                f"{MODULE},{0x1040},1,single\n",
                encoding="utf-8",
            )
            connection = duckdb.connect(":memory:")
            try:
                connection.execute("CREATE TABLE Process(id UINTEGER, name VARCHAR)")
                connection.execute(
                    "CREATE TABLE Module(id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)"
                )
                connection.execute(
                    "CREATE TABLE Function(id UBIGINT, moduleId UINTEGER, "
                    "name VARCHAR, startOffset UBIGINT, size UBIGINT, "
                    "srcfileId UINTEGER)"
                )
                connection.execute(
                    "CREATE TABLE UnifiedSampleSeries(moduleId UINTEGER, "
                    "functionId UBIGINT, moduleRVA UBIGINT, "
                    'processId UINTEGER, coreId UINTEGER, "event#0x76" BIGINT)'
                )
                connection.execute("INSERT INTO Process VALUES (100, 'c:\\capture\\app.exe')")
                connection.execute("INSERT INTO Module VALUES (1, 'C:\\bin\\a.dll', false)")
                connection.execute("INSERT INTO Function VALUES (1, 1, '', 4096, 128, 0)")
                connection.execute(
                    "INSERT INTO UnifiedSampleSeries VALUES "
                    "(1, 1, 4112, 100, 0, 30), (1, 1, 4176, 100, 0, 10)"
                )
                variables = {
                    "uprof_ibs_dump": "",
                    "uprof_ibs_parquet": "",
                    "uprof_symbols": "",
                    "uprof_aliases": str(aliases),
                    "uprof_pdbs": "",
                    "uprof_functions": str(functions),
                    "uprof_event_column": "event#0x76",
                    "uprof_primary_measure": "samples",
                    "uprof_time_start": "0",
                    "uprof_time_end": "1000000",
                    "uprof_top": "20",
                    "uprof_pid": "",
                    "uprof_image": "%",
                    "uprof_by": "function",
                    "uprof_module": "",
                    "uprof_function": "",
                    "uprof_line": "",
                    "uprof_instruction": "",
                }
                uprof_db.set_variables(connection, variables)
                uprof_query._create_file_views(connection, variables)
                uprof_db.run_script(connection, TOOLS_DIR / "views.sql")
                result = connection.execute(
                    'SELECT "function", attribution, samples FROM v_function_rows '
                    "WHERE module IS NOT NULL"
                ).fetchall()
            finally:
                connection.close()
        return {row[0]: row[1:] for row in result}

    def test_a_range_at_a_folded_public_start_is_shared(self):
        rows = self.rows()

        self.assertEqual(rows["first"], ("shared", 30))
        self.assertEqual(rows["single"], ("unique", 10))


if __name__ == "__main__":
    unittest.main()
