"""Function attribution names a row only when every sample in it agrees (#78).

A derived range runs from one boundary to the next, so code without its own boundary (an x64 leaf
without .pdata, a static function) falls into the preceding range. When the samples in one range
carry different uProf Function.id names, neither name is true for the whole row: the row keeps the
synthetic range name and is marked derived instead of `unique` under one of the names.

Run from the repository root: python tests/run_tests.py test_function_attribution
"""
# Spec: SPEC-03, SPEC-57

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


def query(sql, *, ranges=(), functions=(), samples=(), aliases=(), symbols=None, **overrides):
    """Run views.sql over an in-memory capture of c:\\bin\\a.dll and return sql's rows.

    functions are (id, name) rows of the uProf Function table; samples are (function_id, rva,
    count) rows; aliases are (rva, names) folded public starts; symbols is a path to a symbol
    Parquet or None.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        functions_csv = uprof_functions.write_ranges_csv(root / "functions.csv", list(ranges))
        aliases_csv = root / "aliases.csv"
        aliases_csv.write_text(
            "module_name,rva,name_count,names\n"
            + "".join(
                f"{MODULE},{rva},{len(names)},{' | '.join(names)}\n" for rva, names in aliases
            ),
            encoding="utf-8",
        )
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("CREATE TABLE Process(id UINTEGER, name VARCHAR)")
            connection.execute(
                "CREATE TABLE Module(id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)"
            )
            connection.execute(
                "CREATE TABLE Function(id UBIGINT, moduleId UINTEGER, name VARCHAR, "
                "startOffset UBIGINT, size UBIGINT, srcfileId UINTEGER)"
            )
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries(moduleId UINTEGER, functionId UBIGINT, "
                'moduleRVA UBIGINT, processId UINTEGER, coreId UINTEGER, "event#0x76" BIGINT)'
            )
            connection.execute("INSERT INTO Process VALUES (100, 'c:\\capture\\app.exe')")
            connection.execute("INSERT INTO Module VALUES (1, 'C:\\bin\\a.dll', false)")
            for function_id, name in functions:
                connection.execute(
                    "INSERT INTO Function VALUES (?, 1, ?, 0, 0, 0)", [function_id, name]
                )
            for function_id, rva, count in samples:
                connection.execute(
                    "INSERT INTO UnifiedSampleSeries VALUES (1, ?, ?, 100, 0, ?)",
                    [function_id, rva, count],
                )
            variables = {
                "uprof_ibs_dump": "",
                "uprof_ibs_parquet": "",
                "uprof_symbols": str(symbols) if symbols else "",
                "uprof_aliases": str(aliases_csv) if aliases else "",
                "uprof_pdbs": "",
                "uprof_functions": str(functions_csv) if ranges else "",
                "uprof_event_column": "event#0x76",
                "uprof_primary_measure": "samples",
                "uprof_time_start": "0",
                "uprof_time_end": "1000000",
                "uprof_top": "0",
                "uprof_pid": "",
                "uprof_image": "%",
                "uprof_by": "function",
                "uprof_module": "",
                "uprof_function": "",
                "uprof_line": "",
                "uprof_instruction": "",
                **overrides,
            }
            uprof_db.set_variables(connection, variables)
            uprof_query._create_file_views(connection, variables)
            uprof_db.run_script(connection, TOOLS_DIR / "views.sql")
            return connection.execute(sql).fetchall()
        finally:
            connection.close()


FUNCTION_ROWS = (
    'SELECT "function", attribution, function_derived, samples FROM v_function_rows '
    "WHERE module IS NOT NULL ORDER BY function_start"
)


class DerivedRangeNameTest(unittest.TestCase):
    def test_a_range_over_two_uprof_names_is_derived(self):
        rows = query(
            FUNCTION_ROWS,
            ranges=[FunctionRange(MODULE, 0x1000, 0x1080, "pdata", None)],
            functions=[(1, "F"), (2, "L")],
            samples=[(1, 0x1010, 5), (2, 0x1050, 7)],
        )

        self.assertEqual(rows, [("pdata_0x1000", "derived", True, 12)])

    def test_a_named_range_over_two_uprof_names_is_derived(self):
        rows = query(
            FUNCTION_ROWS,
            ranges=[FunctionRange(MODULE, 0x1000, 0x1080, "export", "E")],
            functions=[(1, "E"), (2, "leaf")],
            samples=[(1, 0x1010, 5), (2, 0x1050, 7)],
        )

        self.assertEqual(rows, [("sub_0x1000", "derived", True, 12)])

    def test_an_unnamed_range_partly_named_by_uprof_is_derived(self):
        rows = query(
            FUNCTION_ROWS,
            ranges=[FunctionRange(MODULE, 0x1000, 0x1080, "pdata", None)],
            functions=[(1, "F"), (2, "")],
            samples=[(1, 0x1010, 5), (2, 0x1050, 7)],
        )

        self.assertEqual(rows, [("pdata_0x1000", "derived", True, 12)])

    def test_a_range_over_one_uprof_name_keeps_it(self):
        rows = query(
            FUNCTION_ROWS,
            ranges=[
                FunctionRange(MODULE, 0x1000, 0x1080, "pdata", None),
                FunctionRange(MODULE, 0x1080, 0x1100, "export", "E"),
            ],
            functions=[(1, "F"), (2, "E"), (3, "")],
            samples=[(1, 0x1010, 5), (1, 0x1020, 2), (2, 0x1090, 3), (3, 0x10A0, 1)],
        )

        self.assertEqual(rows, [("F", "unique", False, 7), ("E", "unique", False, 4)])


if __name__ == "__main__":
    unittest.main()
