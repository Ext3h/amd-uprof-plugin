"""A module the capture maps to more than one image version is not attributed to one version.

IBS dump rows carry the module name, not Module.id. When one name has two versions, no sample can
be tied to either version's Function rows, so no row may carry a version's function name or call
itself unique.

Run from the repository root: python tests/run_tests.py test_ambiguous_module_attribution
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
NAMES = {"captured_v1", "captured_v2"}


@unittest.skipUnless(READOBJ, SKIP)
class AmbiguousModuleAttributionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def session(self, checksums):
        """Module ids 1 and 2 share FIXTURE's path; each has its own named function at the
        sampled RVA."""
        session = Session(self.root, "s" + "_".join(str(c) for c in checksums))
        session.module(1, FIXTURE, False).module(2, FIXTURE, False)
        session.function(10, 1, "captured_v1").function(11, 2, "captured_v2")
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
        return path

    def rows(self, path, by="function"):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(path),
                "--by",
                by,
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["rows"]

    def test_two_versions_name_no_function_and_say_why(self):
        rows = self.rows(self.session((0x1111, 0x2222)))
        self.assertFalse(NAMES & {row["function"] for row in rows}, rows)
        self.assertEqual([row["attribution"] for row in rows], ["unresolved"], rows)
        self.assertIn("more than one image version", rows[0]["function_reason"])
        self.assertEqual(rows[0]["samples"], 4)

    def test_one_version_in_two_rows_keeps_its_names(self):
        rows = self.rows(self.session((0x1111, 0x1111)))
        self.assertTrue(NAMES & {row["function"] for row in rows}, rows)


if __name__ == "__main__":
    unittest.main()
