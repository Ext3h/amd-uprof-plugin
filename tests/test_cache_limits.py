"""The session cache's size cap, orphan age and attach timeout are fixed, not preferences.

The tool has no preferences, and the cache must never change a result. Only the cache root and
the opt-out are configurable. UPROF_SESSION_CACHE_MAX_MB, UPROF_SESSION_CACHE_TTL_DAYS and
UPROF_SESSION_CACHE_TIMEOUT must have no effect: the limits stay 1024 MB, 7 days and 30 s.
Run from the repository root: python tests/run_tests.py test_cache_limits
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
# The probe reads three module constants only; a stub stands in for duckdb where it is missing.
PROBE = """
import json, sys, types
try:
    import duckdb
except ImportError:
    sys.modules["duckdb"] = types.ModuleType("duckdb")
import uprof_cache
print(json.dumps([uprof_cache.ATTACH_TIMEOUT_S, uprof_cache.MAX_BYTES, uprof_cache.TTL_DAYS]))
"""


def limits(extra_env):
    env = dict(os.environ, PYTHONPATH=str(TOOLS_DIR), **extra_env)
    completed = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True,
                               text=True, encoding="utf-8")
    return completed


class CacheLimitsTest(unittest.TestCase):
    def test_the_limits_are_fixed(self):
        completed = limits({})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [30.0, 1024 * 1024 * 1024, 7.0])

    def test_the_former_tunables_have_no_effect(self):
        completed = limits({"UPROF_SESSION_CACHE_MAX_MB": "1",
                            "UPROF_SESSION_CACHE_TTL_DAYS": "0",
                            "UPROF_SESSION_CACHE_TIMEOUT": "0"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), [30.0, 1024 * 1024 * 1024, 7.0])


if __name__ == "__main__":
    unittest.main()
