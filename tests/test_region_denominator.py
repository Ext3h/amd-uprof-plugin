"""`--by region` names its denominator: the scoped samples that carry an address.

A sample with an empty Offset lands in no page, so the region total is smaller than the scoped
capture. The legend says so in both views instead of calling it the scoped capture total.

Run from the repository root: python tests/run_tests.py test_region_denominator
"""
# Spec: SPEC-02

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_function_region_profile import READOBJ, SKIP, Session

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
EXCLUSION = "samples without an address are in no page"


@unittest.skipUnless(READOBJ, SKIP)
class RegionDenominatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        session = Session(Path(self.tmp.name), "regions")
        session.module(1, Path(self.tmp.name) / "absent" / "lib.dll", False)
        session.function(10, 1, "")
        session.sample(1, 10, 0x1010, 3).sample(1, 10, None, 1)
        self.session = session.write()

    def profile(self, *extra):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(self.session),
                "--by",
                "region",
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_json_legend_names_the_address_denominator(self):
        document = json.loads(self.profile("--json"))

        pages = [row for row in document["rows"] if row.get("page_start")]
        self.assertEqual(sum(row["samples"] for row in pages), 3)
        self.assertAlmostEqual(sum(row["sample_share"] for row in pages), 1.0)
        columns = document["legend"]["columns"]
        self.assertIn(EXCLUSION, columns["region"])
        self.assertNotIn("scoped capture total", columns["hot_rva_share"])

    def test_the_text_legend_names_the_address_denominator(self):
        text = self.profile()

        self.assertIn(EXCLUSION, text)
        self.assertNotIn("hot_rva_share = share of the scoped capture total", text)


if __name__ == "__main__":
    unittest.main()
