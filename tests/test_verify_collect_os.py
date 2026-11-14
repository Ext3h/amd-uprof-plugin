"""collect fails when the applied IBS OP sampler did not record OS mode (opOs).

The IBS metadata comes from a real committed capture, which records opOs and opUser under
collection.events.ibsEvents.

Run from repository root: python tests/run_tests.py test_verify_collect_os
"""

import json
import tempfile
import unittest
from pathlib import Path

import uprof_session
import uprof_util

CAPTURE = (
    Path(__file__).resolve().parents[1]
    / "trials"
    / "defect_lab"
    / "captures"
    / "windows-x64-llvm-23.1.4"
    / "branch.ibs"
    / "session.uprof"
)


class VerifyCollectOsTest(unittest.TestCase):
    def setUp(self):
        self.meta = json.loads(CAPTURE.read_text(encoding="utf-8-sig"))
        tmp = tempfile.TemporaryDirectory(prefix="uprof-verify-os-")
        self.addCleanup(tmp.cleanup)
        self.session = Path(tmp.name)

    def verify(self):
        (self.session / "session.uprof").write_text(json.dumps(self.meta), encoding="utf-8")
        ibs = self.meta["collection"]["events"]["ibsEvents"]
        control = "cycles" if ibs["opCycleCount"] else "ops"
        return uprof_session.verify_collect(
            self.session, "ibsop", ibs["opInterval"], control, True, []
        )

    def test_the_real_capture_passes(self):
        self.assertTrue(self.meta["collection"]["events"]["ibsEvents"]["opOs"])
        self.verify()

    def test_an_ibs_capture_without_os_mode_fails(self):
        for value in (False, None):
            with self.subTest(opOs=value):
                self.meta["collection"]["events"]["ibsEvents"]["opOs"] = value
                with self.assertRaises(uprof_util.UprofError) as cm:
                    self.verify()
                self.assertIn(f"opOs={value}", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
