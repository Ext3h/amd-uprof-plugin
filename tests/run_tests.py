#!/usr/bin/env python
"""Run the maintainer test suite from the repository's tests/ tree.

The tests import the skill's tool modules and each other as top-level modules, so the
plugin's tools directory and this directory are put on sys.path before discovery.

Run from the repository root:
    python tests/run_tests.py                  # every test_*.py here
    python tests/run_tests.py test_symbols     # named modules

With UPROF_REQUIRE_TOOLS=1 (set by the CI jobs that install LLVM and DuckDB) every skip
fails the run unless its reason starts with HOST_SKIP: a missing tool, module or golden
then fails instead of passing green.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "plugin" / "skills" / "amd-uprof" / "tools"

# Prefix of a skip reason decided by the host itself (operating system, CPU count), which no
# tool install can change; such skips pass even when tools are required.
HOST_SKIP = "host: "


class _RequiredToolsResult(unittest.TextTestResult):
    """Records every skip whose reason does not start with HOST_SKIP as a failure."""

    def addSkip(self, test, reason):
        if reason.startswith(HOST_SKIP):
            super().addSkip(test, reason)
            return
        try:
            raise AssertionError(f"UPROF_REQUIRE_TOOLS=1 forbids this skip: {reason}")
        except AssertionError:
            self.addFailure(test, sys.exc_info())


def run(suite: unittest.TestSuite, require_tools: bool, stream=None) -> bool:
    """Run suite verbosely; True when it passed."""
    runner = unittest.TextTestRunner(
        stream=stream, verbosity=2,
        resultclass=_RequiredToolsResult if require_tools else None)
    return runner.run(suite).wasSuccessful()


# Symbol-path variables cleared by isolate() so a developer's environment never leaks
# local symbol caches into the test suite or trace comparisons.
SYMBOL_PATH_VARS = ("_NT_SYMBOL_PATH", "_NT_ALT_SYMBOL_PATH", "DEBUGINFOD_URLS")


def isolate(root: Path) -> None:
    """Point the session cache, the user cache directory and every temp directory of this process and its children
    below root, overriding the caller's settings so a run never touches their cache,
    and clear symbol-path variables so the developer's environment never leaks into tests."""
    cache, temp, user_cache = root / "cache", root / "tmp", root / "user-cache"
    for directory in (cache, temp, user_cache):
        directory.mkdir()
    os.environ["UPROF_SESSION_CACHE_ROOT"] = str(cache)
    os.environ["XDG_CACHE_HOME"] = str(user_cache)
    for name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[name] = str(temp)
    tempfile.tempdir = str(temp)
    for name in SYMBOL_PATH_VARS:
        os.environ.pop(name, None)


def main(argv: list[str]) -> int:
    root = Path(tempfile.mkdtemp(prefix="uprof-suite-"))
    try:
        isolate(root)
        for entry in (str(TOOLS), str(HERE)):
            if entry not in sys.path:
                sys.path.insert(0, entry)
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(argv) if argv else loader.discover(str(HERE), pattern="test_*.py")
        require_tools = os.environ.get("UPROF_REQUIRE_TOOLS") == "1"
        return 0 if run(suite, require_tools) else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
