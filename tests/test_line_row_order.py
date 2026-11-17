"""`--by line` orders and cuts `--top` by the capture's primary measure.

A cycle-controlled IBS capture ranks by sample_share, as the function and instruction views do;
the gap weight of a line after a long wait must not put it above the line with the most samples.

Run from the repository root: python tests/run_tests.py test_line_row_order
"""
# Spec: SPEC-80, SPEC-92

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_function_region_profile import FIXTURE, READOBJ, SKIP, Session, module_key

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


@unittest.skipUnless(READOBJ, SKIP)
class LineRowOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_most_sampled_line_tops_a_sample_share_capture(self):
        absent = self.root / "absent" / "lib.dll"
        session = Session(self.root, "lines")
        session.module(1, FIXTURE, False).module(2, absent, False)
        session.function(10, 1, "").function(20, 2, "")
        # Three samples a millisecond apart in the fixture, one after a 0.9 s wait elsewhere.
        session.sample(1, 10, 0x101B, 1, gap_ns=1_000_000)
        session.sample(2, 20, 0x2010, 1, gap_ns=900_000_000)
        session.sample(1, 10, 0x101B, 2, gap_ns=1_000_000)
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(session.write()),
                "--by",
                "line",
                "--top",
                "1",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)

        rows = document["rows"]
        self.assertEqual(rows[0]["module"], module_key(FIXTURE))
        self.assertEqual(rows[0]["samples"], 3)
        self.assertEqual(rows[-1]["samples"], 1)
        # The legend names the order the rows follow.
        self.assertEqual(document["legend"]["impact"]["primary_measure"], "sample_share")


if __name__ == "__main__":
    unittest.main()
