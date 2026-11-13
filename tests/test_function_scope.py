"""Function-scoped `--by instruction`/`--by line` views report the parent unit's measurement.

The filter is explicit (`--function` plus `--module`), the rows are limited to the derived range
or the uProf Function.id, and every filtered query carries the parent function's samples, shares
and cpu_seconds next to the capture-level shares. The default, unscoped views keep their old
stops: no instruction breakdown is offered unless one was asked for.

Run from the repository root: python tests/run_tests.py test_function_scope
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_function_region_profile import FIXTURE, READOBJ, SKIP, Session, module_key

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
MODULE = module_key(FIXTURE)


@unittest.skipUnless(READOBJ, SKIP)
class FunctionScopeProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_profile(self, session, *extra, by="instruction"):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile", "--session",
             str(session.path if isinstance(session, Session) else session), "--by", by, *extra],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def document(self, session, *extra, by="instruction"):
        return json.loads(self.run_profile(session, "--json", *extra, by=by).stdout)

    def scoped(self, session, function, *extra, by="instruction"):
        return self.document(session, "--function", function, "--module", MODULE, *extra, by=by)

    def derived_session(self, name="derived"):
        """fixture_entry 0x1000-0x1030, pdata_0x1050 0x1050-0x1062; nine samples, eight weights."""
        session = Session(self.root, name)
        session.module(1, FIXTURE, False).function(10, 1, "")
        session.sample(1, 10, 0x101B, 3).sample(1, 10, 0x1018, 1)
        session.sample(1, 10, 0x1030, 2).sample(1, 10, 0x1050, 2)
        session.sample(1, 10, 0x1055, 1)
        return session.write()

    def named_session(self, name="named"):
        """A fully named module keeps Function.id grouping; no range is derived."""
        session = Session(self.root, name)
        session.module(1, FIXTURE, False).function(10, 1, "fixture_entry")
        session.sample(1, 10, 0x101B, 2).sample(1, 10, 0x201B, 1)
        return session.write()

    def test_a_derived_range_reports_the_parent_measurement_and_the_filter(self):
        document = self.scoped(self.derived_session(), "pdata_0x1050")
        scope = document["legend"]["function_scope"]

        self.assertEqual(scope["range_kind"], "derived_range")
        self.assertEqual((scope["function_start"], scope["function_end"]), ("0x1050", "0x1062"))
        parent = scope["parent"]
        self.assertEqual(parent["samples"], 3)
        self.assertAlmostEqual(parent["sample_share"], 3 / 9)
        self.assertAlmostEqual(parent["time_share"], 3 / 8)
        self.assertIsNone(parent["tag_to_retire_share"])
        self.assertIsNotNone(parent["cpu_seconds"])
        self.assertEqual(parent["shares_are"], "of the scoped capture")
        self.assertEqual(scope["denominators"]["parent_sample_share"], "parent_samples")
        self.assertIn("sum/join", scope["measurement"])
        self.assertIn("relative to the scoped capture", scope["child_shares"])
        self.assertIn("relative to the parent function", scope["child_shares"])

        self.assertEqual(document["FILTER"], scope["filter"])
        self.assertIn("RVA in [0x1050,0x1062)", document["FILTER"])
        self.assertIn("function_source=pdata, synthetic derived name", document["FILTER"])
        self.assertIn("3 samples", document["PARENT"])
        self.assertIn("sample_share 33.3% of the scoped capture", document["PARENT"])
        self.assertIn("time_share 37.5%", document["PARENT"])
        self.assertIn("sum of this function's own rows", document["PARENT"])
        self.assertIn("use the capture denominators", document["PARENT"])

    def test_the_module_matches_in_any_spelling_of_its_path(self):
        # Module.path or a Windows path with backslashes and other case names the same module
        # as the normalized cell; the filter compares normalized keys.
        session = self.derived_session()
        expected = self.scoped(session, "pdata_0x1050")
        spelled = str(FIXTURE).upper().replace("/", "\\")
        document = self.document(session, "--function", "pdata_0x1050", "--module", spelled)
        self.assertEqual(document["legend"]["function_scope"]["parent"],
                         expected["legend"]["function_scope"]["parent"])
        self.assertEqual(document["legend"]["function_scope"]["module"], MODULE)
        self.assertEqual(sum(row["samples"] for row in document["rows"] if row.get("rva")), 3)

    def test_the_derived_range_excludes_samples_outside_it_and_keeps_capture_shares(self):
        document = self.scoped(self.derived_session(), "pdata_0x1050")
        rows = {row["rva"]: row for row in document["rows"] if row.get("rva")}

        self.assertEqual(sum(row["samples"] for row in rows.values()), 3)
        self.assertNotIn("0x1030", rows)
        self.assertNotIn("0x1018", rows)
        self.assertEqual(rows["0x1050"]["samples"], 2)
        self.assertAlmostEqual(rows["0x1050"]["sample_share"], 2 / 9)
        self.assertAlmostEqual(rows["0x1050"]["parent_sample_share"], 2 / 3)
        self.assertEqual(rows["0x1055"]["samples"], 1)
        self.assertAlmostEqual(rows["0x1055"]["sample_share"], 1 / 9)
        self.assertAlmostEqual(rows["0x1055"]["parent_sample_share"], 1 / 3)

    def test_a_truncated_scope_keeps_parent_shares_on_the_remainder(self):
        document = self.scoped(self.derived_session(), "fixture_entry", "--top", "1")
        hot, remainder = document["rows"][0], document["rows"][-1]

        self.assertEqual(hot["rva"], "0x101B")
        self.assertEqual(hot["samples"], 3)
        self.assertAlmostEqual(hot["sample_share"], 3 / 9)
        self.assertAlmostEqual(hot["parent_sample_share"], 3 / 4)
        self.assertEqual(remainder["module"], "<other instructions>")
        self.assertEqual(remainder["samples"], 1)
        self.assertAlmostEqual(remainder["sample_share"], 1 / 9)
        self.assertAlmostEqual(remainder["parent_sample_share"], 1 / 4)
        self.assertAlmostEqual(hot["parent_sample_share"]
                               + remainder["parent_sample_share"], 1.0)

    def test_the_filtered_instruction_rows_pin_the_parent_relative_values(self):
        document = self.scoped(self.derived_session(), "pdata_0x1050")
        rows = [(row["rva"], row["function"], row["file"], row["samples"])
                for row in document["rows"]]

        self.assertEqual(rows, [("0x1050", "fixture_second", "<no source line>", 2),
                                ("0x1055", "fixture_second", "<no source line>", 1)])
        self.assertAlmostEqual(document["rows"][0]["parent_sample_share"], 2 / 3)
        self.assertAlmostEqual(document["rows"][1]["parent_sample_share"], 1 / 3)

    def test_a_line_view_without_source_lines_is_one_parent_relative_row(self):
        document = self.scoped(self.derived_session(), "fixture_entry", by="line")
        rows = document["rows"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file"], "<no source line>")
        self.assertEqual(rows[0]["samples"], 4)
        self.assertAlmostEqual(rows[0]["sample_share"], 4 / 9)
        self.assertAlmostEqual(rows[0]["parent_sample_share"], 1.0)
        self.assertAlmostEqual(rows[0]["parent_time_share"], 1.0)
        self.assertEqual(document["legend"]["function_scope"]["parent"]["samples"], 4)

    def test_a_fully_named_module_filters_by_function_id(self):
        document = self.scoped(self.named_session(), "fixture_entry")
        scope = document["legend"]["function_scope"]
        rows = {row["rva"]: row for row in document["rows"] if row.get("rva")}

        self.assertEqual(scope["range_kind"], "uprof_function_id")
        self.assertIsNone(scope["function_start"])
        self.assertIn("uProf Function.id 10 (no derived range)", document["FILTER"])
        self.assertEqual(scope["parent"]["samples"], 3)
        self.assertEqual(set(rows), {"0x101B", "0x201B"})
        self.assertAlmostEqual(rows["0x101B"]["parent_sample_share"], 2 / 3)
        self.assertAlmostEqual(rows["0x201B"]["parent_sample_share"], 1 / 3)

    def test_function_start_selects_one_derived_range(self):
        document = self.document(self.derived_session(), "--function", "fixture_entry",
                                 "--module", MODULE, "--function-start", "0x1000")
        self.assertEqual(document["legend"]["function_scope"]["function_start"], "0x1000")

    def test_the_filtered_view_offers_its_sibling_and_nothing_else(self):
        derived = self.derived_session()
        instruction = self.scoped(derived, "pdata_0x1050")
        line = self.scoped(derived, "pdata_0x1050", by="line")

        self.assertEqual(len(instruction["legend"]["next"]), 1)
        self.assertIn("--by line", instruction["legend"]["next"][0])
        self.assertIn("--function pdata_0x1050", instruction["legend"]["next"][0])
        self.assertIn("--module", instruction["legend"]["next"][0])
        self.assertEqual(len(line["legend"]["next"]), 1)
        self.assertIn("--by instruction", line["legend"]["next"][0])
        self.assertIn("--json", line["legend"]["next"][0])
        self.assertIn("--function pdata_0x1050", line["legend"]["next"][0])

    def test_the_unscoped_instruction_stop_is_unchanged(self):
        derived = self.derived_session()
        instruction = self.document(derived)
        line = self.document(derived, by="line")

        self.assertEqual(instruction["legend"]["next"], [])
        self.assertNotIn("function_scope", instruction["legend"])
        self.assertNotIn("parent_sample_share", instruction["legend"]["columns"])
        self.assertEqual(len(line["legend"]["next"]), 1)
        self.assertIn("--by instruction", line["legend"]["next"][0])
        self.assertIn("--json", line["legend"]["next"][0])
        self.assertNotIn("--function", line["legend"]["next"][0])

    def test_the_filter_is_rejected_without_an_explicit_scope(self):
        derived = self.derived_session()
        cases = [
            (["--function", "fixture_entry"], "--function needs --module"),
            (["--function", "fixture_entry", "--module", MODULE, "--by", "module"],
             "cannot be used with --by module"),
            (["--function", "nope", "--module", MODULE], "no function row matches"),
            (["--function", "fixture_entry", "--module", MODULE, "--function-start", "zzz"],
             "--function-start needs an RVA"),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                result = subprocess.run(
                    [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile", "--session",
                     str(derived), "--by", "instruction", *extra],
                    capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(expected, result.stderr)

    def test_an_ambiguous_name_without_ranges_cannot_be_resolved_by_start(self):
        session = Session(self.root, "ambiguous")
        session.module(1, FIXTURE, False).function(10, 1, "dup").function(11, 1, "dup")
        session.sample(1, 10, 0x101B, 1).sample(1, 11, 0x201B, 1)
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile", "--session",
             str(session.write()), "--by", "instruction", "--function", "dup", "--module", MODULE],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--function-start cannot separate them", result.stderr)


if __name__ == "__main__":
    unittest.main()
