"""profile searches the symbol stores translate stages PDBs into, in the store layout.

translate stages a PDB as <cache>/<pdb>/<GUID><age>/<pdb> under its --symbol-cache-dir (recorded
in session.uprof) or the default symbol cache; profile must find it there and under every
--symbol-path directory, and the session cache key must notice it being staged.

Run from the repository root: python tests/run_tests.py test_symbol_stores
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_cache
import uprof_symbols

GUID = "ABCD"
AGE = 1


class SymbolStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-stores-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.default_cache = self.root / "Symbols"
        self.custom_cache = self.root / "my cache"
        self.session = self.root / "session"
        self.session.mkdir()
        patches = [
            mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": ""}),
            mock.patch.object(uprof_symbols, "symbol_cache_dir",
                              return_value=self.default_cache),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def record_translate(self, cache):
        command = (f"AMDuProfCLI.exe translate -i {self.session} --symbol-cache-dir {cache} "
                   "--symbol-server srv*C:\\symbols\\ms*https://msdl.microsoft.com/download/symbols")
        (self.session / "session.uprof").write_text(
            json.dumps({"translateCommand": command}), encoding="utf-8")

    def stage(self, store):
        pdb = store / "ntdll.pdb" / f"{GUID}{AGE:X}" / "ntdll.pdb"
        pdb.parent.mkdir(parents=True, exist_ok=True)
        pdb.write_bytes(b"pdb")
        return pdb

    def candidates(self, symbol_dirs=()):
        return uprof_symbols._candidate_pdbs(
            Path("C:/Windows/System32/ntdll.dll"), "ntdll.pdb", GUID, AGE,
            symbol_dirs=symbol_dirs,
            symbol_stores=uprof_symbols.symbol_stores(self.session))

    def test_the_recorded_translate_symbol_cache_is_searched(self):
        self.custom_cache.mkdir()
        self.record_translate(self.custom_cache)
        self.assertIn(self.stage(self.custom_cache), self.candidates())

    def test_the_default_symbol_cache_is_searched(self):
        self.default_cache.mkdir()
        self.assertIn(self.stage(self.default_cache), self.candidates())

    def test_a_symbol_path_directory_is_searched_in_the_store_layout(self):
        store = self.root / "store"
        self.assertIn(self.stage(store), self.candidates(symbol_dirs=[store]))

    def test_a_missing_store_adds_no_candidate(self):
        self.record_translate(self.custom_cache)
        self.assertEqual(uprof_symbols.symbol_stores(self.session), [])

    def test_staging_into_a_store_changes_the_search_identity(self):
        # An earlier build's PDB already created the <pdb> directory, so only its child changes.
        (self.default_cache / "ntdll.pdb").mkdir(parents=True)
        stores = uprof_symbols.symbol_stores(self.session)
        with mock.patch.dict(os.environ, {"UPROF_SESSION_CACHE_ROOT": str(self.root / "c")}):
            before = uprof_cache.search_identity(None, None, ["ntdll.dll"], stores)
            self.stage(self.default_cache)
            after = uprof_cache.search_identity(None, None, ["ntdll.dll"], stores)
        self.assertNotEqual(before, after)

    def test_staging_under_a_symbol_path_changes_the_search_identity(self):
        store = self.root / "store"
        (store / "ntdll.pdb").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"UPROF_SESSION_CACHE_ROOT": str(self.root / "c")}):
            before = uprof_cache.search_identity([str(store)], None, ["ntdll.dll"])
            self.stage(store)
            after = uprof_cache.search_identity([str(store)], None, ["ntdll.dll"])
        self.assertNotEqual(before, after)



class NoPdbRemedyTest(unittest.TestCase):
    def test_the_remedy_names_concrete_steps_without_placeholders(self):
        searched = Path("C:/Symbols/ntdll.pdb/ABCD1/ntdll.pdb")
        evidence = uprof_symbols.ModuleEvidence(
            "c:/windows/system32/ntdll.dll", "C:/Windows/System32/ntdll.dll",
            image_guid=GUID, image_age=AGE, outcome="no_pdb",
            search=uprof_symbols.PdbSearch(probes=[uprof_symbols.PdbProbe(
                searched, "not_found", detail="not found")]))
        stream = io.StringIO()
        uprof_symbols._evidence_line(evidence, stream)
        line = stream.getvalue()

        self.assertEqual(len(line.splitlines()), 1)
        self.assertNotIn("<", line)
        self.assertIn(f"GUID {GUID} age {AGE}", line)
        self.assertIn("one of the searched paths", line)


if __name__ == "__main__":
    unittest.main()
