"""The per-mode view closure creates the needed views and skips the unread row views."""

import unittest
from pathlib import Path

import duckdb

import uprof_db
import uprof_query

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


def _plan(by: str, has_dump: bool = True):
    roots = uprof_query._required_views(by, has_dump, False, True)
    text = (TOOLS_DIR / "views.sql").read_text(encoding="utf-8")
    with duckdb.connect() as connection:
        statements = connection.extract_statements(text)
    needed, names = uprof_db._view_plan(duckdb, statements, roots, "views.sql")
    return roots, needed, {name for name in names if name in needed}


class ViewClosureTest(unittest.TestCase):
    def test_process_skips_the_unread_row_views_but_keeps_the_dump_source(self):
        roots, needed, created = _plan("process")

        self.assertIn("v_process_rows", roots)
        self.assertNotIn("v_function_rows", created)
        self.assertNotIn("v_region_rows", created)
        self.assertNotIn("v_module_rows", created)
        self.assertIn("v_dump_source", created)
        self.assertIn("v_process_rows", created)

    def test_module_skips_process_function_and_region_rows(self):
        _roots, _needed, created = _plan("module")

        self.assertIn("v_module_rows", created)
        for skipped in ("v_process_rows", "v_function_rows", "v_region_rows"):
            self.assertNotIn(skipped, created)

    def test_line_keeps_the_function_scope_dependencies(self):
        _roots, _needed, created = _plan("line")

        self.assertIn("v_line_rows", created)
        self.assertIn("v_function_scope", created)
        self.assertIn("v_scope_measures", created)

    def _rows_block(self, marker: str) -> str:
        text = (TOOLS_DIR / "views.sql").read_text(encoding="utf-8")
        return text.split(f"${marker}$", 1)[1].split(f"${marker}$", 1)[0]

    def _assert_function_scope_readers(self, marker: str) -> None:
        block = self._rows_block(marker)

        self.assertIn("WITH fs AS MATERIALIZED (SELECT * FROM v_function_scope)", block)
        self.assertEqual(block.count("FROM v_function_scope"), 1)
        # The EXISTS test and the fs_one build are the only readers; a third reader of the
        # materialized snapshot can stall the DuckDB 2.0.0 task scheduler again.
        self.assertEqual(block.count("FROM fs"), 2)
        self.assertEqual(block.count("FROM fs LIMIT 1"), 1)
        self.assertEqual(block.count("FROM agg"), 1)
        self.assertIn("FROM agg LEFT JOIN fs_one ON true", block)
        for column in ("samples", "weight", "tag_cycles"):
            self.assertNotIn(f"(SELECT {column} FROM fs LIMIT 1)", block)

    def test_the_line_rows_query_keeps_two_function_scope_readers(self):
        self._assert_function_scope_readers("line_rows")

    def test_the_instruction_rows_query_keeps_two_function_scope_readers(self):
        self._assert_function_scope_readers("instruction_rows")

    def test_instruction_keeps_its_detail_view(self):
        _roots, _needed, created = _plan("instruction")

        self.assertIn("v_instruction_rows", created)

    def test_an_unknown_root_names_the_sql_file(self):
        text = (TOOLS_DIR / "views.sql").read_text(encoding="utf-8")
        with duckdb.connect() as connection:
            statements = connection.extract_statements(text)
        with self.assertRaises(uprof_db.UprofError) as caught:
            uprof_db._view_plan(duckdb, statements, {"v_not_there"}, "views.sql")

        self.assertIn("views.sql does not define v_not_there", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
