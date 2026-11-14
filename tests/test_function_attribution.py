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


def query(sql, *, ranges=(), functions=(), samples=(), aliases=(), frames=(), **overrides):
    """Run views.sql over an in-memory capture of c:\\bin\\a.dll and return sql's rows.

    functions are (id, name) rows of the uProf Function table; samples are (function_id, rva,
    count) rows; aliases are (rva, name_count, names) public starts; frames are (rva, function,
    function_short, containing_function) symbolizer frames.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        symbols = root / "symbols.parquet"
        with duckdb.connect(":memory:") as writer:
            writer.execute(
                "CREATE TABLE frames(module_name VARCHAR, address VARCHAR, function VARCHAR, "
                "function_short VARCHAR, containing_function VARCHAR, file VARCHAR, "
                "line INTEGER)"
            )
            for rva, function, short, containing in frames:
                writer.execute(
                    "INSERT INTO frames VALUES (?, ?, ?, ?, ?, 'a.cpp', ?)",
                    [MODULE, str(rva), function, short, containing, rva],
                )
            writer.execute("COPY frames TO ? (FORMAT parquet)", [str(symbols)])
        functions_csv = uprof_functions.write_ranges_csv(root / "functions.csv", list(ranges))
        aliases_csv = root / "aliases.csv"
        aliases_csv.write_text(
            "module_name,rva,name_count,names\n"
            + "".join(f"{MODULE},{rva},{count},{names}\n" for rva, count, names in aliases),
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
                "uprof_symbols": str(symbols) if frames else "",
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


INSTRUCTION_ROWS = (
    'SELECT rva, "function", attribution FROM v_instruction_rows '
    "WHERE module IS NOT NULL ORDER BY rva"
)
LINE_ROWS = 'SELECT "function", attribution FROM v_line_rows WHERE module IS NOT NULL ORDER BY 1'


class FoldLabelBoundTest(unittest.TestCase):
    """A folded public start labels only its own body `shared` (#79).

    The public start is ASOF-joined, so every address up to the next public start matches it.
    Code past the folded body (a static function without a public name) is not folded: a derived
    range starting after the fold start, or a symbolizer frame that names a function outside the
    fold, ends the folded body.
    """

    def test_a_range_after_the_fold_start_ends_the_folded_body(self):
        rows = query(
            INSTRUCTION_ROWS,
            ranges=[
                FunctionRange(MODULE, 0x1000, 0x1040, "pdb_public", "fa"),
                FunctionRange(MODULE, 0x1040, 0x1080, "pdata", None),
            ],
            aliases=[(0x1000, 2, "fa | fb")],
            samples=[(None, 0x1010, 3), (None, 0x1050, 2)],
            uprof_by="instruction",
        )

        self.assertEqual(
            rows,
            [("0x1010", "shared: fa | fb", "shared"), ("0x1050", "<no symbol>", "unresolved")],
        )

    def test_a_frame_outside_the_fold_keeps_its_name(self):
        rows = query(
            INSTRUCTION_ROWS,
            aliases=[(0x1000, 2, "fa | fb"), (0x1030, 2, "?ca@@YAHH@Z | ?cb@@YAHH@Z")],
            frames=[
                (0x1004, "fb", "fa", "fb"),
                (0x1034, "cb(int)", "ca", "cb(int)"),
                (0x1090, "slow", "slow", "slow"),
            ],
            samples=[(None, 0x1004, 1), (None, 0x1034, 1), (None, 0x1038, 1), (None, 0x1090, 1)],
            uprof_by="instruction",
        )

        self.assertEqual(
            rows,
            [
                ("0x1004", "shared: fa | fb", "shared"),
                ("0x1034", "shared: ?ca@@YAHH@Z | ?cb@@YAHH@Z", "shared"),
                ("0x1038", "shared: ?ca@@YAHH@Z | ?cb@@YAHH@Z", "shared"),
                ("0x1090", "slow", "unique"),
            ],
        )

    def test_a_method_template_or_truncated_fold_stays_shared(self):
        method = "?m1@K@ns@@QEAAHH@Z | ?m2@K@ns@@QEAAHH@Z"
        template = "??$f@H@@YAHH@Z | ??$f@M@@YAHH@Z"
        member = "?push_back@?$vector@H@std@@QEAAXH@Z | ?push_back@?$vector@M@std@@QEAAXM@Z"
        truncated = "a | b | c | d | ... (+1 more)"
        rows = query(
            INSTRUCTION_ROWS,
            aliases=[
                (0x1000, 2, method),
                (0x1100, 2, template),
                (0x1200, 2, member),
                (0x1300, 5, truncated),
            ],
            frames=[
                (0x1004, "ns::K::m2(int)", "ns::K::m1", "ns::K::m2(int)"),
                (0x1104, "int __cdecl f<float>(int)", "f<float>", "int __cdecl f<float>(int)"),
                (
                    0x1204,
                    "std::vector<int>::push_back(int)",
                    "std::vector<int>::push_back",
                    "std::vector<int>::push_back(int)",
                ),
                (0x1304, "e", "e", "e"),
            ],
            samples=[(None, rva, 1) for rva in (0x1004, 0x1104, 0x1204, 0x1304)],
            uprof_by="instruction",
        )

        self.assertEqual([row[2] for row in rows], ["shared"] * 4, rows)

    def test_a_cpp_frame_outside_the_fold_keeps_its_name(self):
        rows = query(
            INSTRUCTION_ROWS,
            aliases=[(0x1000, 2, "?m1@K@ns@@QEAAHH@Z | ?m2@K@ns@@QEAAHH@Z")],
            frames=[(0x1090, "ns::K::other(int)", "ns::K::other", "ns::K::other(int)")],
            samples=[(None, 0x1090, 1)],
            uprof_by="instruction",
        )

        self.assertEqual(rows, [("0x1090", "ns::K::other(int)", "unique")])

    def test_the_line_view_keeps_the_name_outside_the_fold(self):
        rows = query(
            LINE_ROWS,
            aliases=[(0x1000, 2, "fa | fb")],
            frames=[(0x1004, "fb", "fa", "fb"), (0x1090, "slow", "slow", "slow")],
            samples=[(None, 0x1004, 1), (None, 0x1090, 1)],
            uprof_by="line",
        )

        self.assertEqual(rows, [("shared: fa | fb", "shared"), ("slow", "unique")])


# X has one sample outside every range and three inside the range Exp.
PARENT_SCOPE = {
    "ranges": [FunctionRange(MODULE, 0x1000, 0x1080, "export", "Exp")],
    "functions": [(1, "X")],
    "samples": [(1, 0x1010, 2), (1, 0x1020, 1), (1, 0x2000, 1)],
    "uprof_function_module": MODULE,
}


class FunctionIdParentTest(unittest.TestCase):
    """A Function.id parent scopes only the samples its row holds (#82).

    A sample inside a derived range belongs to the range row, even when uProf gives it the
    Function.id of a row outside every range. A function-scoped view of that Function.id row
    lists only the samples outside every range, so the shares of its children sum to 1.
    """

    def test_instruction_children_of_a_function_id_parent(self):
        rows = query(
            "SELECT rva, samples, parent_samples FROM v_instruction_rows "
            "WHERE module IS NOT NULL ORDER BY rva",
            uprof_by="instruction",
            uprof_function="X",
            **PARENT_SCOPE,
        )

        self.assertEqual(rows, [("0x2000", 1, 1)])

    def test_line_children_of_a_function_id_parent(self):
        rows = query(
            "SELECT samples, parent_samples FROM v_line_rows WHERE module IS NOT NULL",
            uprof_by="line",
            uprof_function="X",
            **PARENT_SCOPE,
        )

        self.assertEqual(rows, [(1, 1)])

    def test_instruction_children_of_a_range_parent(self):
        rows = query(
            "SELECT rva, samples, parent_samples FROM v_instruction_rows "
            "WHERE module IS NOT NULL ORDER BY rva",
            uprof_by="instruction",
            uprof_function="Exp",
            **PARENT_SCOPE,
        )

        self.assertEqual(rows, [("0x1010", 2, 3), ("0x1020", 1, 3)])


REGION_ROWS = (
    "SELECT hot_function, attribution, symbol_reason, function_source FROM v_region_rows "
    "WHERE module IS NOT NULL ORDER BY page_start"
)

RANGE_REASON = (
    "no source line: the derived function range names hot_rva but there is no symbolizer file/line"
)


class RegionSymbolReasonTest(unittest.TestCase):
    """The region symbol_reason names only a source that exists (#86)."""

    def test_a_folded_hot_rva_without_a_range_names_the_fold(self):
        rows = query(
            REGION_ROWS,
            aliases=[(0x1000, 2, "fa | fb")],
            samples=[(None, 0x1010, 1)],
            uprof_by="region",
        )

        self.assertEqual(len(rows), 1)
        function, attribution, reason, source = rows[0]
        self.assertEqual((function, attribution, source), ("shared: fa | fb", "shared", "none"))
        self.assertNotIn("derived function range", reason)
        self.assertIn("folded", reason)

    def test_a_derived_range_without_a_frame_names_the_range(self):
        rows = query(
            REGION_ROWS,
            ranges=[FunctionRange(MODULE, 0x1000, 0x1080, "pdata", None)],
            samples=[(None, 0x1010, 1)],
            uprof_by="region",
        )

        self.assertEqual(
            rows,
            [
                (
                    "pdata_0x1000",
                    "derived",
                    RANGE_REASON,
                    "pdata",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
