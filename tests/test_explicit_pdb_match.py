"""A PDB is used only when its GUID and age verifiably match the image.

Without an image GUID (llvm-readobj missing or failed, or no debug directory) or without
llvm-pdbutil, no PDB can be verified. A PDB picked by name alone may come from another build
and give plausible but wrong names, so none is used: the module gets one no_pdb evidence line
with the cause and a runnable alternative, and PDB_MATCH is never printed.

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
class UnverifiablePdbTest(unittest.TestCase):
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

    def pdb(self, name, directory=None):
        path = (directory or self.pdbs) / name
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
            mock.patch.object(uprof_symbols, "_run_symbolizer", return_value=failed) as run,
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
                symbol_files=[explicit] if explicit else [],
            )
        passed = [call.args[2] for call in run.call_args_list]
        return result, stream.getvalue(), passed

    def evidence_line(self, result):
        line = io.StringIO()
        uprof_symbols._evidence_line(result.evidence, line)
        return line.getvalue()

    def assert_refused(self, result, output, passed):
        self.assertIsNone(result.pdb)
        self.assertEqual(result.evidence.outcome, "no_pdb")
        self.assertEqual([pdb for pdb in passed if pdb is not None], [])
        self.assertNotIn("PDB_MATCH=yes", output)
        self.assertIn("PDB=none", output)

    def test_the_modules_own_pdb_is_not_used_without_an_image_guid(self):
        result, output, passed = self.symbolize(
            self.pdb("plugin.pdb"), ("plugin.pdb", None, None, None)
        )

        self.assert_refused(result, output, passed)

    def test_another_modules_pdb_is_not_used_without_an_image_guid(self):
        result, output, passed = self.symbolize(
            self.pdb("app.pdb"), ("plugin.pdb", None, None, None)
        )

        self.assert_refused(result, output, passed)

    def test_without_an_image_guid_the_line_names_the_cause_and_the_image_alternative(self):
        explicit = self.pdb("plugin.pdb")
        result, _, _ = self.symbolize(explicit, ("plugin.pdb", None, None, None))
        text = self.evidence_line(result)

        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn(str(explicit), text)
        self.assertIn("records no PDB identity", text)
        self.assertIn("--image-path", text)

    def test_an_explicit_pdb_is_not_used_without_pdbutil(self):
        result, output, passed = self.symbolize(
            self.pdb("plugin.pdb"), ("plugin.pdb", "ABCD", 1, None), tools=("llvm-readobj",)
        )

        self.assert_refused(result, output, passed)

    def test_a_pdb_beside_the_module_is_not_used_without_pdbutil(self):
        beside = self.pdb("plugin.pdb", self.module.parent)
        result, output, passed = self.symbolize(
            None, ("plugin.pdb", "ABCD", 1, None), tools=("llvm-readobj",)
        )

        self.assert_refused(result, output, passed)
        self.assertIn(beside, [probe.path for probe in result.evidence.search.probes])

    def test_without_pdbutil_the_line_names_the_tool_to_install_where(self):
        result, _, _ = self.symbolize(
            self.pdb("plugin.pdb"), ("plugin.pdb", "ABCD", 1, None), tools=("llvm-readobj",)
        )
        text = self.evidence_line(result)

        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("llvm-pdbutil", text)
        self.assertIn("llvm-symbolizer.exe", text)
        self.assertNotIn("copy the PDB", text)


if __name__ == "__main__":
    unittest.main()
