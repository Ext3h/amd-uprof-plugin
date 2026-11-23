"""SYMBOL_COVERAGE and AGREEMENT split nearest-symbol names out as derived (#283).

Without a PDB or debug info llvm-symbolizer names the nearest preceding export or symbol-table
entry. Such a frame is not a source symbol, so the coverage line must not call every sample
drillable, and both the Markdown header and the JSON legend carry the derived count. A covering
export range or a uProf name over such a frame repeats the same unproven name, so the row stays
derived: one name in one capture gets one category in the function, line and instruction views.

Run from the repository root: python tests/run_tests.py test_derived_coverage
"""
# Spec: SPEC-03, SPEC-04

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
import uprof_query
from test_function_attribution import MODULE, query
from uprof_functions import FunctionRange

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "plugin" / "skills" / "amd-uprof" / "tools"
AGREEMENT_SQL = TOOLS / "agreement.sql"
UPROF = TOOLS / "uprof.py"


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

    def test_an_all_derived_capture_is_drillable_at_its_addresses(self):
        # rows_drillable means no scoped sample is unresolved, in every view: a derived row is
        # attributed and actionable at its address, as the function and region legends say.
        legend = uprof_query._symbol_coverage_legend(
            COVERAGE, symbolizer(10, 0, 10), True, [], by="line"
        )
        rows = {"scoped": 10, "named": 0, "derived": 10, "unresolved": 0}
        function = uprof_query._symbol_coverage_legend(
            COVERAGE, None, True, [], by="function", row_attribution=rows
        )

        self.assertEqual(legend["profile_symbolizer"]["derived_samples"], 10)
        self.assertEqual(legend["profile_symbolizer"]["named_samples"], 0)
        self.assertTrue(legend["rows_drillable"])
        self.assertEqual(legend["rows_drillable"], function["rows_drillable"])

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
        self.assertNotIn("sample attribution", agreement)
        self.assertIn("mismatch_rate=", agreement)

    def test_the_function_view_counts_a_nearest_name_as_derived(self):
        rows = query(
            "SELECT attribution, samples FROM v_function_rows WHERE module IS NOT NULL",
            ranges=[FunctionRange(MODULE, 0x3000, 0x3010, "export", "far")],
            frames=[(0x1050, "second", "second", "second", "", 0, True)],
            samples=[(None, 0x1050, 2)],
            uprof_by="function",
        )

        self.assertEqual(rows, [("derived", 2)])

    def test_a_named_export_range_over_a_nearest_name_stays_derived(self):
        rows = query(
            'SELECT "function", attribution, samples FROM v_function_rows WHERE module IS NOT NULL',
            ranges=[FunctionRange(MODULE, 0x1000, 0x1080, "export", "second")],
            frames=[(0x1050, "second", "second", "second", "", 0, True)],
            samples=[(None, 0x1050, 2)],
            uprof_by="function",
        )

        self.assertEqual(rows, [("second", "derived", 2)])

    def test_a_uprof_name_over_a_nearest_name_stays_derived(self):
        rows = query(
            'SELECT "function", attribution, samples FROM v_function_rows WHERE module IS NOT NULL',
            functions=[(7, "Exported")],
            frames=[(0x1050, "second", "second", "second", "", 0, True)],
            samples=[(7, 0x1050, 2)],
            uprof_by="function",
        )

        self.assertEqual(rows, [("Exported", "derived", 2)])

    def test_a_folded_start_over_a_nearest_name_is_shared_as_in_the_drills(self):
        capture = {
            "ranges": [FunctionRange(MODULE, 0x1000, 0x1080, "export", "second")],
            "frames": [(0x1050, "second", "second", "second", "", 0, True)],
            "aliases": [(0x1000, 2, "second | other")],
            "samples": [(None, 0x1050, 2)],
        }
        function = query(
            "SELECT attribution, samples FROM v_function_rows WHERE module IS NOT NULL",
            uprof_by="function",
            **capture,
        )
        line = query(
            "SELECT attribution, samples FROM v_line_rows WHERE module IS NOT NULL",
            uprof_by="line",
            **capture,
        )

        self.assertEqual(line, [("shared", 2)])
        self.assertEqual(function, line)

    def test_a_sized_symbol_over_a_named_range_stays_unique(self):
        rows = query(
            "SELECT attribution, samples FROM v_function_rows WHERE module IS NOT NULL",
            ranges=[FunctionRange(MODULE, 0x1000, 0x1080, "export", "second")],
            frames=[(0x1050, "second", "second", "second", "", 0, False)],
            samples=[(None, 0x1050, 2)],
            uprof_by="function",
        )

        self.assertEqual(rows, [("unique", 2)])


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class CrossViewTest(unittest.TestCase):
    """The function view and the drills classify one capture's nearest-symbol names alike."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "trials"))
        import build_captures

        cls.tmp = tempfile.TemporaryDirectory()
        cls.session = build_captures.build(Path(cls.tmp.name) / "c", ["attached"])["attached"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def profile(self, *args) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(UPROF),
                "profile",
                "--session",
                str(self.session),
                "--json",
                *args,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def categories(rows) -> dict:
        found: dict = {}
        for row in rows:
            if row.get("module") and row.get("attribution"):
                found.setdefault((row["module"], row["function"]), set()).add(row["attribution"])
        return found

    def test_coverage_splits_named_and_derived_alike(self):
        function = self.profile("--by", "function")["legend"]["symbol_coverage"]
        line = self.profile("--by", "line")["legend"]["symbol_coverage"]

        split = ("named_samples", "derived_samples", "unresolved_samples", "scoped_samples")
        self.assertEqual(
            {key: function["profile_symbolizer"][key] for key in split},
            {key: line["profile_symbolizer"][key] for key in split},
        )

    def test_rows_drillable_agrees_between_the_function_view_and_the_drills(self):
        function = self.profile("--by", "function")["legend"]["symbol_coverage"]
        for by in ("line", "instruction"):
            with self.subTest(by=by):
                drill = self.profile("--by", by)["legend"]["symbol_coverage"]
                self.assertEqual(function["rows_drillable"], drill["rows_drillable"])

    def test_agreement_counts_the_same_derived_samples(self):
        function = self.profile("--by", "function")["AGREEMENT"]
        line = self.profile("--by", "line")["AGREEMENT"]
        derived = re.search(r"derived=\d+ addresses, (\d+) samples", line)

        self.assertIsNotNone(derived, line)
        self.assertIn(f"derived {derived.group(1)} samples", function)

    def test_a_name_gets_one_category_in_every_view(self):
        function = self.categories(self.profile("--by", "function")["rows"])
        drills = {}
        for by in ("line", "instruction"):
            for name, found in self.categories(self.profile("--by", by)["rows"]).items():
                drills.setdefault(name, set()).update(found)
        for module, name in [key for key in function if key in drills]:
            for by in ("line", "instruction"):
                scoped = self.profile("--by", by, "--function", name, "--module", module)
                for key, found in self.categories(scoped["rows"]).items():
                    drills.setdefault(key, set()).update(found)
        shared = sorted(set(function) & set(drills))

        self.assertTrue(shared)
        for key in shared:
            with self.subTest(name=key):
                self.assertEqual(function[key], drills[key])


if __name__ == "__main__":
    unittest.main()
