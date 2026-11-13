"""top_n is typed as BIGINT in views.sql; verify it is rebound as an integer and not VARCHAR."""
# Spec: SPEC-41

import unittest
from pathlib import Path

import duckdb

import uprof_query


class TopNVariableTypeTest(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("SET VARIABLE top_n = CAST(20 AS BIGINT)")
        self.con.execute(
            "CREATE TABLE v_function_rows ("
            "attribution VARCHAR, samples BIGINT, "
            "function VARCHAR, module VARCHAR, "
            "hot_rva BIGINT, hot_samples BIGINT, total_samples BIGINT)"
        )
        self.con.execute("CREATE TABLE v_region_rows (attribution VARCHAR, samples BIGINT)")

    def tearDown(self):
        self.con.close()

    def _top_n_type(self) -> str:
        row = self.con.execute("SELECT typeof(getvariable('top_n'))").fetchone()
        return str(row[0]) if row else ""

    def _top_n_value(self) -> int:
        row = self.con.execute("SELECT getvariable('top_n')").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def test_row_attribution_binds_top_n_as_int(self):
        self.assertEqual(self._top_n_type(), "BIGINT")
        self.assertEqual(self._top_n_value(), 20)

        counts = uprof_query._row_attribution(self.con, "function")
        self.assertEqual(counts["scoped"], 0)

        current_type = self._top_n_type()
        self.assertNotEqual(
            current_type,
            "VARCHAR",
            f"top_n was rebound as VARCHAR instead of integer: {current_type}",
        )
        self.assertIn(current_type, ("BIGINT", "INTEGER"))
        self.assertEqual(self._top_n_value(), 20)

    def test_hot_rva_field_binds_top_n_as_int(self):
        self.assertEqual(self._top_n_type(), "BIGINT")
        self.assertEqual(self._top_n_value(), 20)

        fields = uprof_query._hot_rva_field(self.con)
        self.assertEqual(fields, [])

        current_type = self._top_n_type()
        self.assertNotEqual(
            current_type,
            "VARCHAR",
            f"top_n was rebound as VARCHAR instead of integer: {current_type}",
        )
        self.assertIn(current_type, ("BIGINT", "INTEGER"))
        self.assertEqual(self._top_n_value(), 20)


if __name__ == "__main__":
    unittest.main()
