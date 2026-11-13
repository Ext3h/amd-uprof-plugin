"""A recorded translate time filter suppresses the whole-capture kernel check for every mode.

KERNEL_COVERAGE claims something about the whole capture. When the session records a translate
--time-filter, what profile reads may hold only that window: the IBS dump, and also a PMC or
timer cpu.db translated with the filter. So the check is skipped whenever the window is
recorded, not only when an IBS dump was resolved.
Run from the repository root: python tests/run_tests.py test_windowed_capture
"""

import json
import tempfile
import unittest
from pathlib import Path

import uprof_query
import uprof_session


def session(translate_command):
    temp = tempfile.TemporaryDirectory(prefix="uprof-window-")
    path = Path(temp.name)
    meta = {"collection": {"target": {"systemWide": True}}}
    if translate_command is not None:
        meta["translateCommand"] = translate_command
    (path / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
    return temp, path


class WindowedCaptureTest(unittest.TestCase):
    def windowed(self, translate_command):
        temp, path = session(translate_command)
        self.addCleanup(temp.cleanup)
        return uprof_query._capture_windowed(path)

    def test_a_pmc_translate_with_a_time_filter_is_windowed(self):
        self.assertTrue(self.windowed("AMDuProfCLI.exe translate -i sess --time-filter 10:12"))

    def test_an_ibs_dump_translate_with_a_time_filter_is_windowed(self):
        self.assertTrue(self.windowed(
            "AMDuProfCLI.exe translate -i sess --ascii ibsop-event-dump --time-filter 10:12"))

    def test_a_translate_without_a_time_filter_is_not_windowed(self):
        self.assertFalse(self.windowed("AMDuProfCLI.exe translate -i sess"))
        self.assertFalse(self.windowed(None))

    def test_a_windowed_capture_emits_no_kernel_field(self):
        self.assertEqual(uprof_query._kernel_coverage_field(
            uprof_session.Sampler(os_requested=True), False, windowed=True), [])


if __name__ == "__main__":
    unittest.main()
