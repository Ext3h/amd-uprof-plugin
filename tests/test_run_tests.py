"""The suite runner's UPROF_REQUIRE_TOOLS mode, which CI sets on its toolchain jobs.

Run from the repository root: python tests/run_tests.py test_run_tests
"""

import io
import unittest

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
