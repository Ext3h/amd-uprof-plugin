"""Public-symbol cache aggregation and PDB discovery guards.

Run from this directory: python -m unittest test_symbols -v
"""

import csv
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_symbols
from uprof_util import UprofError


class PublicSymbolsTest(unittest.TestCase):
    def setUp(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("DuckDB is required to aggregate public symbols")
        self.directory = tempfile.TemporaryDirectory(prefix="uprof-publics-")
        self.addCleanup(self.directory.cleanup)

    def aggregate(self, pairs_text):
        pairs = Path(self.directory.name) / "pairs.csv"
        out = Path(self.directory.name) / "publics.csv"
        pairs.write_text(pairs_text, encoding="utf-8", newline="\n")
        uprof_symbols._aggregate_publics(pairs, out)
        return list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))

    def test_a_single_name_start_keeps_its_name(self):
        rows = self.aggregate("rva,name\n4096,entry\n4112,a\n4112,b\n")

        self.assertEqual(rows[0]["names"], "entry")

    def test_a_shared_start_lists_every_name(self):
        rows = self.aggregate("rva,name\n4112,b\n4112,a\n")

        self.assertEqual(rows[0]["name_count"], "2")
        self.assertEqual(rows[0]["names"], "a | b")


class PdbProbeTest(unittest.TestCase):
    def test_the_readobj_probe_has_a_timeout(self):
        completed = subprocess.CompletedProcess(["llvm-readobj"], 0, "", "")

        with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
            uprof_symbols._readobj_info("fixture.exe", Path("llvm-readobj.exe"))

        self.assertEqual(invoked.call_args.kwargs["timeout"],
                         uprof_symbols.PDB_PROBE_TIMEOUT)

    def test_the_pdbutil_probe_has_a_timeout(self):
        completed = subprocess.CompletedProcess(["llvm-pdbutil"], 0,
                                                "GUID: ABCD\nAge: 1\n", "")

        with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
            uprof_symbols._pdb_signature(Path("llvm-pdbutil.exe"), Path("fixture.pdb"))

        self.assertEqual(invoked.call_args.kwargs["timeout"],
                         uprof_symbols.PDB_PROBE_TIMEOUT)

    def test_an_unreadable_symbol_cache_contributes_no_candidates(self):
        exe = Path("C:/app/fixture.exe")
        with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": r"srv*C:\cache*https://example"}):
            with mock.patch.object(Path, "rglob", side_effect=OSError("network path")):
                candidates = uprof_symbols._candidate_pdbs(exe, "fixture.pdb", "ABCD", 1)

        self.assertEqual(candidates, [Path("C:/app/fixture.pdb"),
                                      Path("C:/cache/fixture.pdb/ABCD1/fixture.pdb")])

    def test_a_failing_signature_probe_does_not_abort_discovery(self):
        with mock.patch.object(uprof_symbols.os, "name", "nt"), \
                mock.patch.object(uprof_symbols, "require_symbolizer",
                                  return_value=Path("llvm-symbolizer.exe")), \
                mock.patch.object(uprof_symbols, "find_sibling",
                                  return_value=Path("tool.exe")), \
                mock.patch.object(uprof_symbols, "_readobj_info",
                                  return_value=("fixture.pdb", "ABCD", 1)), \
                mock.patch.object(uprof_symbols, "_candidate_pdbs",
                                  return_value=[Path(__file__)]), \
                mock.patch.object(uprof_symbols, "_pdb_matches",
                                  side_effect=UprofError("timed out after 300s")):
            self.assertIsNone(uprof_symbols.find_pdb("fixture.exe"))


if __name__ == "__main__":
    unittest.main()
