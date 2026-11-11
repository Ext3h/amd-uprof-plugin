"""The filtered row views read the function-scope snapshot through the fs/fs_one shape."""

import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


class FilteredRowViewCteTest(unittest.TestCase):
    def _rows_block(self, marker: str) -> str:
        text = (TOOLS_DIR / "views.sql").read_text(encoding="utf-8")
        return text.split(f"${marker}$", 1)[1].split(f"${marker}$", 1)[0]

    def _assert_function_scope_shape(self, marker: str) -> None:
        block = self._rows_block(marker)

        self.assertIn("WITH fs AS MATERIALIZED (SELECT * FROM v_function_scope)", block)
        self.assertEqual(block.count("v_function_scope"), 1)
        # The EXISTS test and the fs_one build are the only readers; a third reader of the
        # materialized snapshot can stall the DuckDB 2.0.0 task scheduler again.
        self.assertEqual(block.count("FROM fs"), 2)
        self.assertEqual(block.count("FROM fs LIMIT 1"), 1)
        self.assertEqual(block.count("FROM agg"), 1)
        self.assertEqual(block.count("FROM agg LEFT JOIN fs_one ON true"), 1)
        for column in ("samples", "weight", "tag_cycles"):
            self.assertNotIn(f"(SELECT {column} FROM fs LIMIT 1)", block)
            self.assertNotIn(f"(SELECT {column} FROM v_function_scope LIMIT 1)", block)

    def test_the_line_rows_query_keeps_two_function_scope_readers(self):
        self._assert_function_scope_shape("line_rows")

    def test_the_instruction_rows_query_keeps_two_function_scope_readers(self):
        self._assert_function_scope_shape("instruction_rows")


if __name__ == "__main__":
    unittest.main()
