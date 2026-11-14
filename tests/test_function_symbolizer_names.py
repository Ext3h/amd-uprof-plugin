"""Function rows outside every derived range take the symbolizer's containing function.

An ELF image with DWARF has no PE boundary source, so no range is derived; uProf recorded no
function name. llvm-symbolizer still names each address, so the rows are named, not
'<unresolved>', and --function selects the samples of that name.

Run from the repository root: python tests/run_tests.py test_function_symbolizer_names
"""
# Spec: SPEC-58

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
from test_function_region_profile import Session, module_key

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
IMAGE = F.ELF / "fixture_dwarf"
MODULE = module_key(IMAGE)
SKIP = F.tool_skip() or F.duckdb_skip()


@unittest.skipIf(SKIP, SKIP or "")
class SymbolizerNamedFunctionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="uprof-symbolizer-names-")
        session = Session(Path(cls.tmp.name), "elf")
        session.module(1, IMAGE, False).function(10, 1, "")
        session.sample(1, 10, F.ELF_HELPER, 1)
        session.sample(1, 10, F.ELF_ENTRY, 2).sample(1, 10, F.ELF_ENTRY_BODY, 3)
        cls.session = session.write()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def document(self, by, *extra):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(self.session),
                "--by",
                by,
                "--json",
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_function_rows_carry_the_symbolizer_name(self):
        rows = {row["function"]: row for row in self.document("function")["rows"]}

        self.assertEqual(set(rows), {"fixture_entry", "fixture_helper"})
        self.assertEqual(rows["fixture_entry"]["samples"], 5)
        self.assertEqual(rows["fixture_helper"]["samples"], 1)
        for row in rows.values():
            self.assertEqual(row["attribution"], "unique")
            self.assertEqual(row["function_source"], "none")

    def test_function_scope_selects_the_symbolizer_named_samples(self):
        document = self.document("line", "--function", "fixture_entry", "--module", MODULE)
        scope = document["legend"]["function_scope"]

        self.assertEqual(scope["range_kind"], "symbolizer_function")
        self.assertEqual(scope["parent"]["samples"], 5)
        lines = {row["line"]: row["samples"] for row in document["rows"]}
        self.assertEqual(lines, {6: 2, 7: 3})


if __name__ == "__main__":
    unittest.main()
