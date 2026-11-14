"""Instruction context never reads a PDB that symbolization did not GUID-verify.

Without --pdb, llvm-symbolizer loads the PDB the image's CodeView entry names, or the one beside
the image, with no GUID check. When symbolization accepted no PDB for a module whose image
records one (another build's PDB beside it, no llvm-pdbutil, none found), the context omits
instead of showing another build's function and line names.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
import uprof_context
from test_context_fixture import SKIP, FakeConnection, _objdump_supports_flag


class _Connection(FakeConnection):
    """fixture_full.exe's samples; v_pdbs names no PDB, as when symbolization rejected it."""

    def execute(self, sql, parameters=None):
        if "FROM v_scoped" in " ".join(sql.split()):
            return _Rows([(F.PE_ENTRY_BODY, 4)])
        return super().execute(sql, parameters)


class _Rows:
    def __init__(self, rows):
        self._rows = rows
        self.description = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
@unittest.skipUnless(_objdump_supports_flag(), SKIP)
class UnverifiedPdbTest(unittest.TestCase):
    def test_another_builds_pdb_beside_the_image_is_never_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "fixture_full.exe"
            shutil.copyfile(F.PE / "fixture_full.exe", image)
            shutil.copyfile(F.PE / "fixture_other.pdb", Path(tmp) / "fixture_full.pdb")
            readobj = uprof_context.run([F.READOBJ, "--file-headers", str(image)]).stdout
            checksum = uprof_context.parse_image(readobj).build.checksum
            rows = [("fixture_full.exe", f"0x{F.PE_ENTRY_BODY:X}")]

            context = uprof_context.instruction_contexts(
                _Connection(str(image), checksum), ["module", "rva"], rows
            )[0]

        self.assertNotIn("/other/", str(context))
        self.assertEqual(context.get("reason"), "pdb_unverified")
        self.assertTrue(context["omitted"])
        self.assertTrue(context["build"]["verified"])


if __name__ == "__main__":
    unittest.main()
