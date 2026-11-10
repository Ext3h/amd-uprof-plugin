#!/usr/bin/env python
"""Run the maintainer test suite from the repository's tests/ tree.

The tests import the skill's tool modules and each other as top-level modules, so the
plugin's tools directory and this directory are put on sys.path before discovery.

Run from the repository root:
    python tests/run_tests.py                  # every test_*.py here
    python tests/run_tests.py test_symbols     # named modules
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "plugin" / "skills" / "amd-uprof" / "tools"


def main(argv: list[str]) -> int:
    for entry in (str(TOOLS), str(HERE)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(argv) if argv else loader.discover(str(HERE), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
