"""SYMBOL_COVERAGE and AGREEMENT split nearest-symbol names out as derived (#283).

Without a PDB or debug info llvm-symbolizer names the nearest preceding export or symbol-table
entry. Such a frame is not a source symbol, so the coverage line must not call every sample
drillable, and both the Markdown header and the JSON legend carry the derived count.

Run from the repository root: python tests/run_tests.py test_derived_coverage
"""
# Spec: SPEC-03, SPEC-04

import unittest
from pathlib import Path

import uprof_query
from test_function_attribution import MODULE, query
from uprof_functions import FunctionRange

AGREEMENT_SQL = (
    Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
) / "agreement.sql"


def symbolizer(scoped, unresolved, derived):
    return {
        "scoped_samples": scoped,
        "unresolved_samples": unresolved,
        "unresolved": 0,
        "unresolved_pct": 0.0,
        "derived": 1 if derived else 0,
        "derived_samples": derived,
        "mismatch_rate": None,
        "mismatched_samples": 0,
        "compared_samples": 0,
        "name_mismatches": 0,
        "compared_addresses": 0,
    }


COVERAGE = {
    "rows": [{"module_name": MODULE, "samples": 10, "named_samples": 0}],
    "total": 10,
    "named": 0,
}


class AgreementSqlTest(unittest.TestCase):
    def test_a_nearest_symbol_frame_counts_as_derived_not_resolved(self):
        rows = query(
            AGREEMENT_SQL.read_text(encoding="utf-8"),
            frames=[
                (0x1050, "second", "second", "second", "", 0, True),
                (0x1090, "slow", "slow", "slow"),
            ],
            samples=[(None, 0x1050, 3), (None, 0x1090, 1), (None, 0x2000, 1)],
            uprof_by="line",
        )
        columns = [
            "mismatch_rate",
            "mismatched_samples",
            "compared_samples",
            "name_mismatches",
            "compared_addresses",
            "unresolved",
            "unresolved_samples",
            "unresolved_pct",
            "scoped_samples",
            "derived",
            "derived_samples",
        ]
        values = dict(zip(columns, rows[0]))

        self.assertEqual(values["derived_samples"], 3)
        self.assertEqual(values["unresolved_samples"], 1)
        self.assertEqual(values["scoped_samples"], 5)


class LineCoverageTest(unittest.TestCase):
    def test_derived_samples_are_not_called_drillable(self):
        text = uprof_query._coverage_field(
            None, {}, symbolizer(10, 0, 4), True, [], COVERAGE, by="line"
        )[0][1]

        self.assertIn("profile symbolizer 6/10 samples (60.0%) resolve below module/RVA", text)
        self.assertIn("derived 4/10 samples (40.0%)", text)
        self.assertNotIn("drillable", text)
        self.assertIn("every scoped sample is attributed below module/RVA", text)

    def test_the_json_legend_carries_the_derived_count(self):
        legend = uprof_query._symbol_coverage_legend(
            COVERAGE, symbolizer(10, 0, 4), True, [], by="line"
        )

        self.assertEqual(legend["profile_symbolizer"]["named_samples"], 6)
        self.assertEqual(legend["profile_symbolizer"]["derived_samples"], 4)
        self.assertEqual(legend["profile_symbolizer"]["unresolved_samples"], 0)

    def test_without_derived_samples_the_line_is_unchanged(self):
        text = uprof_query._coverage_field(
            None, {}, symbolizer(10, 0, 0), True, [], COVERAGE, by="line"
        )[0][1]

        self.assertIn("every scoped sample is drillable below module/RVA", text)
        self.assertNotIn("derived", text)

    def test_agreement_names_the_derived_samples(self):
        text = uprof_query._agreement_text(None, symbolizer(10, 0, 4), by="line")

        self.assertIn("derived=1 addresses, 4 samples", text)


class RangeCoverageTest(unittest.TestCase):
    def test_the_range_split_says_derived_includes_nearest_symbol_names(self):
        rows = {"scoped": 4, "named": 0, "derived": 4, "unresolved": 0}
        text = uprof_query._coverage_field(
            None, {}, None, True, [], COVERAGE, by="function", row_attribution=rows
        )[0][1]
        agreement = uprof_query._agreement_text(
            None, symbolizer(4, 0, 4), by="function", row_attribution=rows
        )

        self.assertIn("nearest-symbol name", text)
        self.assertIn("nearest symbol", agreement)

    def test_the_function_view_counts_a_nearest_name_as_derived(self):
        rows = query(
            "SELECT attribution, samples FROM v_function_rows WHERE module IS NOT NULL",
            ranges=[FunctionRange(MODULE, 0x3000, 0x3010, "export", "far")],
            frames=[(0x1050, "second", "second", "second", "", 0, True)],
            samples=[(None, 0x1050, 2)],
            uprof_by="function",
        )

        self.assertEqual(rows, [("derived", 2)])


if __name__ == "__main__":
    unittest.main()
