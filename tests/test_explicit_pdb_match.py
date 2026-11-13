"""An explicit --symbol-path PDB used without GUID verification applies only to its own module.

Without an image GUID (llvm-readobj missing or failed, or no debug directory) or without
llvm-pdbutil, a PDB cannot be verified. It is then used only for the module whose PDB name it
carries; any other module rejects it with one evidence line instead of getting another binary's
names.

Run from the repository root: python tests/run_tests.py test_explicit_pdb_match
"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_symbols
from run_tests import HOST_SKIP


@unittest.skipUnless(os.name == "nt", HOST_SKIP + "the PDB search runs on Windows")
class ExplicitPdbMatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-explicit-pdb-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = self.root / "bin" / "plugin.dll"
        self.module.parent.mkdir()
        self.module.write_bytes(b"MZ")
        self.rvas = self.root / "rvas.txt"
        self.rvas.write_text("0x1000" + os.linesep, encoding="utf-8")
        self.pdbs = self.root / "pdbs"
        self.pdbs.mkdir()
        patch = mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": ""})
        patch.start()
        self.addCleanup(patch.stop)

    def pdb(self, name):
        path = self.pdbs / name
        path.write_bytes(b"pdb")
        return path

    def symbolize(self, explicit, readobj_info, tools=("llvm-readobj", "llvm-pdbutil")):
        stream = io.StringIO()
        failed = uprof_symbols.SymbolizerRun(1, [], "")
        with (
            mock.patch.object(
                uprof_symbols, "require_symbolizer", return_value=Path("llvm-symbolizer.exe")
            ),
            mock.patch.object(
                uprof_symbols,
                "find_sibling",
                side_effect=lambda _tool, name: Path(name + ".exe") if name in tools else None,
            ),
            mock.patch.object(uprof_symbols, "_readobj_info", return_value=readobj_info),
            mock.patch.object(uprof_symbols, "_run_symbolizer", return_value=failed),
            mock.patch.object(
                uprof_symbols, "_write_parquet", return_value=self.root / "symbols.parquet"
            ),
            mock.patch.object(uprof_symbols, "_publics", return_value=None),
        ):
            result = uprof_symbols.symbolize_module(
                str(self.module),
                self.rvas,
                self.root / "out",
                stream=stream,
                module_name="plugin.dll",
                symbol_files=[explicit],
            )
        return result, stream.getvalue()

    def test_another_modules_pdb_is_not_used_without_an_image_guid(self):
        result, _ = self.symbolize(self.pdb("app.pdb"), ("plugin.pdb", None, None, None))

        self.assertIsNone(result.pdb)
        self.assertEqual(result.evidence.outcome, "pdb_unmatched")

    def test_the_rejection_is_one_line_with_a_concrete_remedy(self):
        result, _ = self.symbolize(self.pdb("app.pdb"), ("plugin.pdb", None, None, None))
        line = io.StringIO()
        uprof_symbols._evidence_line(result.evidence, line)
        text = line.getvalue()

        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn("<", text)
        self.assertIn("app.pdb", text)
        self.assertIn("plugin.pdb", text)

    def test_the_modules_own_pdb_is_used_without_an_image_guid(self):
        own = self.pdb("plugin.pdb")
        result, _ = self.symbolize(own, ("plugin.pdb", None, None, None))

        self.assertEqual(result.pdb, own)

    def test_another_modules_pdb_is_not_used_without_pdbutil(self):
        result, _ = self.symbolize(
            self.pdb("app.pdb"), ("plugin.pdb", "ABCD", 1, None), tools=("llvm-readobj",)
        )

        self.assertIsNone(result.pdb)
        rejected = [
            probe for probe in result.evidence.search.probes if probe.path.name == "app.pdb"
        ]
        self.assertEqual([probe.status for probe in rejected], ["mismatch"])


if __name__ == "__main__":
    unittest.main()
