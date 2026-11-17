"""Function rows have one total order and one `<unresolved>` row per module.

Blank-named uProf Function.ids carry no name, so their samples join the module's `<unresolved>`
row instead of adding look-alike rows that --function cannot tell apart. Rows that tie on every
measure and on function and module name still have a fixed order, so --top keeps the same rows
on every run.

Run from the repository root: python tests/run_tests.py test_function_row_order
"""
# Spec: SPEC-58, SPEC-59

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_function_region_profile import READOBJ, SKIP, Session, module_key

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


@unittest.skipUnless(READOBJ, SKIP)
class FunctionRowOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # No file at the module path: no boundary source and no symbolizer frame, so only the
        # uProf Function table names the samples.
        self.module_path = self.root / "absent" / "lib.dll"
        self.module = module_key(self.module_path)

    def profile(self, session, *extra, by="function"):
        return subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(session),
                "--by",
                by,
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def rows(self, session, *extra):
        result = self.profile(session, "--json", *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["rows"]

    def unresolved_session(self):
        """Two blank-named Function.ids and samples without any Function.id, in one module."""
        session = Session(self.root, "unresolved")
        session.module(1, self.module_path, False).function(10, 1, "").function(11, 1, "")
        session.sample(1, 10, 0x1010, 3).sample(1, 11, 0x2010, 2).sample(1, None, 0x3010, 1)
        return session.write()

    def test_blank_named_function_ids_join_the_module_unresolved_row(self):
        rows = [row for row in self.rows(self.unresolved_session()) if row["module"] == self.module]

        self.assertEqual([row["function"] for row in rows], ["<unresolved>"])
        self.assertEqual(rows[0]["samples"], 6)
        self.assertEqual(rows[0]["attribution"], "unresolved")

    def test_the_unresolved_row_is_selected_by_name_and_module(self):
        result = self.profile(
            self.unresolved_session(),
            "--function",
            "<unresolved>",
            "--module",
            self.module,
            by="instruction",
        )

        # One row matches; it has no address set of its own, so the refusal names the page view.
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("--function-start cannot separate them", result.stderr)
        self.assertIn("--by region", result.stderr)

    def test_tied_same_name_rows_order_by_function_id(self):
        session = Session(self.root, "tied")
        session.module(1, self.module_path, False).function(11, 1, "dup").function(10, 1, "dup")
        session.sample(1, 11, 0x2010, 2).sample(1, 10, 0x1010, 2)
        path = session.write()

        rows = self.rows(path, "--top", "1")

        self.assertEqual([row["function"] for row in rows], ["dup", "<other functions>"])
        self.assertEqual(rows[0]["hot_rva"], "0x1010")


if __name__ == "__main__":
    unittest.main()
