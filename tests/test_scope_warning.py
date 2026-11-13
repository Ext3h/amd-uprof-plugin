"""An empty `--image` scope is a warning on stderr, not only a field in the output file.

A JSON or `--out-file` run whose filter matched nothing otherwise looks successful. The warning
also names what `--image` selects when the pattern looks like a module file, so the next call is
the module or function scope rather than another empty rerun.

Run from the repository root: python tests/run_tests.py test_scope_warning
"""

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from test_function_region_profile import FIXTURE, Session
import uprof_db
import uprof_dump
import uprof_query
import uprof_session
import uprof_util as util
MODULE_HINT = ("the pattern looks like a module name: --image scopes process image names, not "
               "modules; use --by module or --function ... --module")


class ScopeWarningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.empty_session()

    def empty_session(self):
        # The capture has samples; the filters below exclude all of them, so the scoped total is 0.
        session = Session(self.root, "empty")
        session.module(1, FIXTURE, False).function(10, 1, "")
        session.sample(1, 10, 0x1000, 1)
        return session.write()

    def run_profile(self, *extra):
        return subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile", "--session",
             str(self.session), "--by", "module", *extra],
            capture_output=True, text=True, encoding="utf-8")

    def test_the_empty_scope_warning_reaches_stderr_with_json_and_out_file(self):
        out_file = self.root / "profile.json"
        result = self.run_profile("--image", "ntdll.dll", "--json", "--out-file", str(out_file))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("WARNING: no samples in the selected scope", result.stderr)
        self.assertIn("available images in this capture: workload.exe", result.stderr)
        self.assertIn(MODULE_HINT, result.stderr)
        document = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertIn("no samples in the selected scope", document["WARNING"])
        self.assertIn(MODULE_HINT, document["WARNING"])

    def test_the_text_out_file_is_not_silent_either(self):
        out_file = self.root / "profile.md"
        result = self.run_profile("--image", "ntdll.dll", "--out-file", str(out_file))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: no samples in the selected scope", result.stderr)
        self.assertIn("WARNING: no samples in the selected scope",
                      out_file.read_text(encoding="utf-8"))

    def test_a_process_image_pattern_does_not_get_the_module_hint(self):
        result = self.run_profile("--image", "other.exe")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: no samples in the selected scope", result.stderr)
        self.assertIn("--image selects process base images", result.stderr)
        self.assertNotIn(MODULE_HINT, result.stderr)

    def test_a_non_image_scope_warning_also_reaches_stderr(self):
        result = self.run_profile("--pid", "999")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: no samples in the selected scope; the process or time filter "
                      "matched nothing", result.stderr)


class ErrorRemediesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_missing_cpu_db_prints_runnable_translate_call(self):
        empty_session = self.root / "empty"
        empty_session.mkdir()
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile", "--session",
             str(empty_session), "--by", "process"],
            capture_output=True, text=True, encoding="utf-8")

        self.assertEqual(result.returncode, 2)
        self.assertIn("cpu.db not found", result.stderr)
        self.assertIn("run python ", result.stderr)
        self.assertIn("translate --session ", result.stderr)
        self.assertIn("--out-dir ", result.stderr)
        self.assertIn(f"{empty_session}-translated", result.stderr)
        self.assertNotIn("<COPY_DIR>", result.stderr)

    def test_no_function_row_matches_offers_runnable_alternatives(self):
        session = Session(self.root, "s")
        session.module(1, FIXTURE, False).function(10, 1, "my_func")
        session.sample(1, 10, 0x1000, 5)
        sess_dir = session.write()

        sampler = uprof_session.derive_sampler(sess_dir)
        dump = uprof_dump.resolve_dump(sess_dir, sampler)
        ns = argparse.Namespace(session=str(sess_dir), pid=None, image=None, top="20",
                                time_filter=None, symbol_path=None, image_path=None,
                                json=False, out_file=None, by="line", function="my_func",
                                module="wrong_module", function_start=None)
        variables = uprof_query._variables(ns, dump, 20, "", "", sampler.event_column)

        with uprof_db.open_query(sess_dir / "cpu.db", [TOOLS_DIR / "views.sql"], variables) as connection:
            # 1. Casing mismatch on module
            args1 = argparse.Namespace(session=str(sess_dir), by="line", function="my_func",
                                       module=str(FIXTURE).upper(), function_start=None,
                                       pid=None, image=None, time_filter=None, symbol_path=None,
                                       image_path=None, top="20", json=False)
            with self.assertRaises(util.UprofError) as ctx1:
                uprof_query._function_scope(connection, args1)
            err1 = str(ctx1.exception)
            self.assertIn("no function row matches", err1)
            self.assertIn("runnable alternatives:", err1)
            self.assertIn("--function my_func", err1)
            self.assertIn(str(FIXTURE).replace("\\", "/").lower(), err1.lower())

            # 2. Unknown function in known module
            args2 = argparse.Namespace(session=str(sess_dir), by="line", function="unknown_func",
                                       module=str(FIXTURE).replace("\\", "/").lower(),
                                       function_start=None, pid=None, image=None,
                                       time_filter=None, symbol_path=None, image_path=None,
                                       top="20", json=False)
            with self.assertRaises(util.UprofError) as ctx2:
                uprof_query._function_scope(connection, args2)
            err2 = str(ctx2.exception)
            self.assertIn("no function row matches", err2)
            self.assertIn("runnable alternatives:", err2)
            self.assertIn("--function my_func", err2)

            # 3. Completely unknown function and module
            args3 = argparse.Namespace(session=str(sess_dir), by="line", function="unknown_func",
                                       module="unknown_module.dll", function_start=None,
                                       pid=None, image=None, time_filter=None, symbol_path=None,
                                       image_path=None, top="20", json=False)
            with self.assertRaises(util.UprofError) as ctx3:
                uprof_query._function_scope(connection, args3)
            err3 = str(ctx3.exception)
            self.assertIn("no function row matches", err3)
            self.assertNotIn("runnable alternatives:", err3)
            self.assertIn("copy the function, module and function_start cells", err3)


if __name__ == "__main__":
    unittest.main()
