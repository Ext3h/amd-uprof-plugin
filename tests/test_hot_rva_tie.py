"""The HOT_RVA summary breaks a hot-sample tie by the numeric RVA, then the module.

Run from the repository root: python tests/run_tests.py test_hot_rva_tie
"""
# Spec: SPEC-79

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_function_region_profile import READOBJ, SKIP, Session

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


@unittest.skipUnless(READOBJ, SKIP)
class HotRvaTieTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def hot_rva(self, session):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(session.write()),
                "--by",
                "function",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["HOT_RVA"]

    def test_a_tie_picks_the_lower_numeric_rva(self):
        # As strings '0x10' sorts before '0x9'; as addresses 0x9 is lower.
        session = Session(self.root, "rvas")
        session.module(1, self.root / "absent" / "lib.dll", False)
        session.function(10, 1, "high").function(11, 1, "low")
        session.sample(1, 10, 0x10, 2).sample(1, 11, 0x9, 2)

        self.assertIn(" low at 0x9 with 2 samples", self.hot_rva(session))

    def test_a_tie_at_one_rva_picks_the_first_module(self):
        session = Session(self.root, "modules")
        session.module(1, self.root / "absent" / "b.dll", False)
        session.module(2, self.root / "absent" / "a.dll", False)
        session.function(10, 1, "in_b").function(20, 2, "in_a")
        session.sample(1, 10, 0x10, 2).sample(2, 20, 0x10, 2)

        self.assertIn("a.dll in_a at 0x10", self.hot_rva(session))


if __name__ == "__main__":
    unittest.main()
