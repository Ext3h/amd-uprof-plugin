"""A --symbol-path PDB that no module used is reported, not silently ignored.

Without the warning, an ignored PDB leaves every address '<no symbol>' and the coverage remedy
asks for the --symbol-path that was already passed.

Run from the repository root: python tests/run_tests.py test_unused_symbol_file
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_symbols


class UnusedSymbolFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-unused-symbol-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdb = self.root / "app.pdb"
        self.pdb.write_bytes(b"pdb")
        self.debug = self.root / "app.debug"
        self.debug.write_bytes(b"elf")

    def symbolize(self, used_pdb=None):
        outcome = uprof_symbols._ModuleSymbolization(
            "app.exe", "", pdb=str(used_pdb) if used_pdb else None
        )
        stream = io.StringIO()
        with (
            mock.patch.object(
                uprof_symbols, "require_symbolizer", return_value=Path("llvm-symbolizer")
            ),
            mock.patch.object(uprof_symbols, "_symbolize_tasks", return_value=[outcome]),
        ):
            uprof_symbols.symbolize_modules(
                [("app.exe", "C:/app/app.exe", ["0x1000"])],
                self.root / "out",
                stream=stream,
                symbol_files=[self.pdb, self.debug],
            )
        return [line for line in stream.getvalue().splitlines() if line.startswith("WARNING:")]

    def test_a_pdb_no_module_used_is_warned_about_in_one_line(self):
        warnings = self.symbolize()

        self.assertEqual(len(warnings), 1)
        self.assertIn(str(self.pdb), warnings[0])
        self.assertIn("used for no module", warnings[0])
        self.assertNotIn("<", warnings[0])

    def test_a_used_pdb_is_not_warned_about(self):
        self.assertEqual(self.symbolize(used_pdb=self.pdb), [])


if __name__ == "__main__":
    unittest.main()
