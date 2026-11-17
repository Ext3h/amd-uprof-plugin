"""The `--top` remainder tells the shown module's rest from other modules.

When every shown row belongs to one module, the remainder is two rows: the rest of that module
(module cell set) and everything in other modules (module cell empty). When the shown rows span
several modules, one remainder row says it holds all modules. Together they equal the old single
remainder.

Run from the repository root: python tests/run_tests.py test_remainder_by_module
"""
# Spec: SPEC-11

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_function_region_profile import READOBJ, SKIP, Session, module_key

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


@unittest.skipUnless(READOBJ, SKIP)
class RemainderByModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # No file at these paths: the uProf Function names are the only names.
        self.paths = [self.root / "absent" / f"{name}.dll" for name in ("a", "b", "c")]
        self.modules = [module_key(path) for path in self.paths]

    def session(self, name, samples):
        """samples: (module index, function name, rva, count) per function."""
        session = Session(self.root, name)
        for index, path in enumerate(self.paths, start=1):
            session.module(index, path, False)
        for function_id, (module, function, rva, count) in enumerate(samples, start=10):
            session.function(function_id, module, function)
            session.sample(module, function_id, rva, count)
        return session.write()

    def profile(self, session, by, *extra):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(session),
                "--by",
                by,
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def remainder(self, session, by, top, key):
        rows = json.loads(self.profile(session, by, "--top", str(top), "--json"))["rows"]
        return [
            (row[key], row["module"], row["samples"])
            for row in rows
            if str(row[key]).startswith("<other")
        ]

    def test_one_shown_module_splits_the_function_remainder(self):
        session = self.session(
            "functions-one", [(1, "fa", 0x1010, 3), (1, "fb", 0x2010, 2), (2, "fc", 0x1010, 1)]
        )

        self.assertEqual(
            self.remainder(session, "function", 1, "function"),
            [
                ("<other functions in this module>", self.modules[0], 2),
                ("<other functions in other modules>", None, 1),
            ],
        )
        text = self.profile(session, "function", "--top", "1")
        self.assertIn("<other functions in this module>", text)
        self.assertIn("<other functions in other modules>", text)

    def test_several_shown_modules_keep_one_function_remainder(self):
        session = self.session(
            "functions-several",
            [(1, "fa", 0x1010, 3), (2, "fc", 0x1010, 2), (1, "fb", 0x2010, 1)],
        )

        self.assertEqual(
            self.remainder(session, "function", 2, "function"),
            [("<other functions (all modules)>", None, 1)],
        )

    def test_an_empty_part_of_the_line_remainder_is_omitted(self):
        # Without symbols each module is one <no symbol> line row, so the shown module has no
        # rest and only the other modules remain.
        session = self.session("lines-one", [(1, "fa", 0x1010, 3), (2, "fc", 0x1010, 1)])

        self.assertEqual(
            self.remainder(session, "line", 1, "file"),
            [("<other lines in other modules>", None, 1)],
        )

    def test_several_shown_modules_keep_one_line_remainder(self):
        session = self.session(
            "lines-several", [(1, "fa", 0x1010, 3), (2, "fc", 0x1010, 2), (3, "fd", 0x1010, 1)]
        )

        self.assertEqual(
            self.remainder(session, "line", 2, "file"),
            [("<other lines (all modules)>", None, 1)],
        )


if __name__ == "__main__":
    unittest.main()
