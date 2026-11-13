"""A recorded translate time filter suppresses the whole-capture kernel check for every mode.

KERNEL_COVERAGE claims something about the whole capture. When the session records a translate
--time-filter, what profile reads may hold only that window: the IBS dump, and also a PMC or
timer cpu.db translated with the filter. So the check is skipped whenever the window is
recorded, not only when an IBS dump was resolved.
Run from the repository root: python tests/run_tests.py test_windowed_capture
"""

import json
import subprocess
import sys
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

    def test_an_ibs_dump_translate_with_a_time_filter_is_windowed(self):
        self.assertTrue(
            self.windowed(
                "AMDuProfCLI.exe translate -i sess --ascii ibsop-event-dump --time-filter 10:12"
            )
        )

    def test_a_translate_without_a_time_filter_is_not_windowed(self):
        self.assertFalse(self.windowed("AMDuProfCLI.exe translate -i sess"))
        self.assertFalse(self.windowed(None))

    def test_a_windowed_capture_emits_no_kernel_field(self):
        self.assertEqual(
            uprof_query._kernel_coverage_field(
                uprof_session.Sampler(os_requested=True), False, windowed=True
            ),
            [],
        )


# A PMC capture: the event column is the event#0x... spelling of the recorded encodedEventId.
PMC_COLUMN = "event#0x3000076"


def pmc_capture(root, translate_command):
    """A PMC capture with OS mode requested and only a user-mode module, as translated."""
    import duckdb

    root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(root / "cpu.db"))
    try:
        connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
        connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)")
        connection.execute(
            "CREATE TABLE UnifiedSampleSeries (processId UINTEGER, threadId UINTEGER, "
            "moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT, coreId UINTEGER, "
            f'"{PMC_COLUMN}" DOUBLE)'
        )
        connection.execute("INSERT INTO Process VALUES (1, 'c:\\capture\\app.exe')")
        connection.execute("INSERT INTO Module VALUES (1, 'c:/app/app.exe')")
        connection.execute("INSERT INTO UnifiedSampleSeries VALUES (1, 1, 1, NULL, 16, 0, 5)")
    finally:
        connection.close()
    meta = {
        "collection": {
            "events": {
                "pmcEvents": {
                    "pmcEventList": [
                        {
                            "hwId": 118,
                            "interval": 4_700_000,
                            "isSampling": True,
                            "user": True,
                            "os": True,
                            "encodedEventId": 0x03000076,
                        }
                    ]
                }
            },
            "target": {"systemWide": True},
            "schedule": {"elapsed": 1000},
        },
        "auxiliary": {},
    }
    if translate_command is not None:
        meta["translateCommand"] = translate_command
    (root / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
    return root


class PmcProfileTest(unittest.TestCase):
    """End to end: profile on a PMC capture with no kernel sample and OS mode requested."""

    def setUp(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("duckdb is required; install duckdb==2.0.0")
        temp = tempfile.TemporaryDirectory(prefix="uprof-pmc-window-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def profile(self, translate_command):
        session = pmc_capture(self.root / "s", translate_command)
        result = subprocess.run(
            [
                sys.executable,
                str(uprof_query.TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(session),
                "--by",
                "process",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout + result.stderr

    def test_a_recorded_time_filter_suppresses_kernel_coverage(self):
        output = self.profile("AMDuProfCLI.exe translate -i sess --time-filter 1:2")
        self.assertNotIn("KERNEL_COVERAGE", output)
        self.assertNotIn("whole capture has no sample in any kernel image", output)

    def test_without_a_time_filter_the_field_appears(self):
        self.assertIn("KERNEL_COVERAGE", self.profile("AMDuProfCLI.exe translate -i sess"))


if __name__ == "__main__":
    unittest.main()
