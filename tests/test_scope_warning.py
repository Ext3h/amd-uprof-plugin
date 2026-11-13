"""An empty `--image` scope is a warning on stderr, not only a field in the output file.

A JSON or `--out-file` run whose filter matched nothing otherwise looks successful. The warning
also names what `--image` selects when the pattern looks like a module file, so the next call is
the module or function scope rather than another empty rerun.

Run from the repository root: python tests/run_tests.py test_scope_warning
"""
# Spec: SPEC-82, SPEC-83, SPEC-84

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_function_region_profile import FIXTURE, Session

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
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


if __name__ == "__main__":
    unittest.main()
