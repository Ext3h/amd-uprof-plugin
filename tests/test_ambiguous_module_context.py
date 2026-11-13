"""The instruction context omits a module the capture maps to more than one image version.

Picking one of the module's rows would verify, or reject, the build on disk against whichever
version sorted first, so the verdict would be chance.

Run from the repository root: python tests/run_tests.py test_ambiguous_module_context
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb
from test_function_region_profile import FIXTURE, READOBJ, SKIP, Session

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
REASON = "ambiguous_module_version"


@unittest.skipUnless(READOBJ, SKIP)
class AmbiguousModuleContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def contexts(self, checksums):
        """Module ids 1 and 2 share FIXTURE's path and differ only in the recorded checksum."""
        session = Session(self.root, "s" + "_".join(str(c) for c in checksums))
        session.module(1, FIXTURE, False).module(2, FIXTURE, False)
        session.function(10, 1, "").function(11, 2, "")
        session.sample(1, 10, 0x101B, 3).sample(2, 11, 0x101B, 1)
        path = session.write()
        connection = duckdb.connect(str(path / "cpu.db"))
        try:
            connection.execute("ALTER TABLE Module ADD COLUMN checksum BIGINT")
            connection.execute("ALTER TABLE Module ADD COLUMN size UBIGINT")
            connection.execute("ALTER TABLE Module ADD COLUMN is32Bit BOOLEAN")
            for module_id, checksum in zip((1, 2), checksums):
                connection.execute(
                    "UPDATE Module SET checksum = ?, size = 4096, is32Bit = false WHERE id = ?",
                    [checksum, module_id],
                )
        finally:
            connection.close()
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(path),
                "--by",
                "instruction",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)["rows"]
        return [row["context"] for row in rows if row.get("rva") is not None]

    def test_two_versions_omit_the_context_with_a_stable_reason(self):
        contexts = self.contexts((0x1111, 0x2222))
        self.assertTrue(contexts)
        for context in contexts:
            self.assertEqual(context.get("reason"), REASON, context)
            self.assertTrue(context.get("omitted"), context)
            self.assertNotIn("build", context)

    def test_one_version_in_two_rows_is_not_ambiguous(self):
        for context in self.contexts((0x1111, 0x1111)):
            self.assertNotEqual(context.get("reason"), REASON, context)


if __name__ == "__main__":
    unittest.main()
