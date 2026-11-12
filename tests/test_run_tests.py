"""UPROF_REQUIRE_TOOLS in the suite runner and the trace capture; CI sets it on its toolchain jobs.

Run from the repository root: python tests/run_tests.py test_run_tests
"""

import io
import os
import tempfile
import unittest
from unittest import mock

import capture_debug_traces
import run_tests


def _passes(skip_reason: str, require_tools: bool) -> bool:
    """Whether a suite holding one test that skips with skip_reason passes."""
    def skipping():
        raise unittest.SkipTest(skip_reason)

    suite = unittest.TestSuite([unittest.FunctionTestCase(skipping)])
    return run_tests.run(suite, require_tools=require_tools, stream=io.StringIO())


MISSING_TOOL = "missing llvm-objdump; install LLVM"


class RequireToolsTest(unittest.TestCase):
    def test_a_missing_tool_skip_passes_by_default(self):
        self.assertTrue(_passes(MISSING_TOOL, require_tools=False))

    def test_a_missing_tool_skip_fails_when_tools_are_required(self):
        self.assertFalse(_passes(MISSING_TOOL, require_tools=True))

    def test_a_host_skip_passes_when_tools_are_required(self):
        self.assertTrue(_passes(run_tests.HOST_SKIP + "runs only on Windows",
                                require_tools=True))


class CaptureRequireToolsTest(unittest.TestCase):
    def test_the_capture_stops_when_a_required_tool_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="uprof-require-tools-") as root,                 mock.patch.dict(os.environ, {"UPROF_REQUIRE_TOOLS": "1"}),                 mock.patch.object(capture_debug_traces.F, "tool_skip",
                                  return_value=MISSING_TOOL):
            with self.assertRaises(SystemExit) as raised:
                capture_debug_traces.main(["--out-dir", root])
        self.assertIn(MISSING_TOOL, str(raised.exception.code))
