"""The test runner gives every run its own cache and temp root and removes it at exit.

A developer's own UPROF_SESSION_CACHE_ROOT and temp directory must stay untouched by a run.
Run from the repository root: python tests/run_tests.py test_run_tests_isolation
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parent / "run_tests.py"

PROBE = textwrap.dedent("""
    import json
    import os
    import tempfile
    import unittest


    class Probe(unittest.TestCase):
        def test_probe(self):
            scratch = tempfile.mkdtemp(prefix="probe-")
            with open(os.environ["PROBE_REPORT"], "w", encoding="utf-8") as handle:
                json.dump({"cache": os.environ.get("UPROF_SESSION_CACHE_ROOT"),
                           "temp": tempfile.gettempdir(),
                           "user_cache": os.environ.get("XDG_CACHE_HOME"),
                           "scratch": scratch,
                           "env_temp": [os.environ.get(name) for name in ("TMPDIR", "TEMP", "TMP")],
                           "symbol_vars": {name: os.environ.get(name) for name in (
                               "_NT_SYMBOL_PATH", "_NT_ALT_SYMBOL_PATH", "DEBUGINFOD_URLS")}},
                          handle)
""")


class RunnerIsolationTest(unittest.TestCase):
    def test_a_run_clears_symbol_path_variables(self):
        with tempfile.TemporaryDirectory() as outer:
            outer = Path(outer)
            probe_dir = outer / "probe"
            probe_dir.mkdir()
            (probe_dir / "probe_symbols.py").write_text(PROBE, encoding="utf-8")
            report = outer / "report.json"
            sentinels = {
                "_NT_SYMBOL_PATH": r"srv*C:\developer\symbols*https://symbols.invalid",
                "_NT_ALT_SYMBOL_PATH": r"C:\developer\alt_symbols",
                "DEBUGINFOD_URLS": "https://debuginfod.invalid",
            }
            env = dict(os.environ, PYTHONPATH=str(probe_dir), PROBE_REPORT=str(report), **sentinels)
            run = subprocess.run(
                [sys.executable, str(RUNNER), "probe_symbols"],
                check=False,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            seen = json.loads(report.read_text(encoding="utf-8"))
            for name in sentinels:
                self.assertIsNone(seen["symbol_vars"][name], f"{name} leaked into the test suite")

    def test_a_run_uses_and_removes_its_own_cache_and_temp_root(self):
        with tempfile.TemporaryDirectory() as outer:
            outer = Path(outer)
            probe_dir, user_cache, user_temp = (outer / name for name in ("probe", "cache", "temp"))
            for directory in (probe_dir, user_cache, user_temp):
                directory.mkdir()
            (probe_dir / "probe_isolation.py").write_text(PROBE, encoding="utf-8")
            report = outer / "report.json"
            env = dict(
                os.environ,
                PYTHONPATH=str(probe_dir),
                PROBE_REPORT=str(report),
                UPROF_SESSION_CACHE_ROOT=str(user_cache),
                XDG_CACHE_HOME=str(user_cache),
                TMPDIR=str(user_temp),
                TEMP=str(user_temp),
                TMP=str(user_temp),
            )
            run = subprocess.run(
                [sys.executable, str(RUNNER), "probe_isolation"],
                check=False,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            seen = json.loads(report.read_text(encoding="utf-8"))

            cache, temp, scratch, xdg = (
                Path(seen[key]) for key in ("cache", "temp", "scratch", "user_cache")
            )
            self.assertNotEqual(cache.resolve(), user_cache.resolve())
            self.assertNotEqual(xdg.resolve(), user_cache.resolve())
            self.assertNotEqual(temp.resolve(), user_temp.resolve())
            self.assertIn(temp.resolve(), scratch.resolve().parents)
            self.assertEqual(seen["env_temp"], [str(temp)] * 3)
            for path in (cache, temp, scratch, xdg):
                self.assertFalse(path.exists(), f"{path} survived the run")
            self.assertEqual(list(user_cache.iterdir()), [])
            self.assertEqual(list(user_temp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
