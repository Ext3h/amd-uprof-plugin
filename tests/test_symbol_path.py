"""One _NT_SYMBOL_PATH reading shared by translate, PDB discovery and the session-cache key.

Run from the repository root: python tests/run_tests.py test_symbol_path
"""
# Spec: SPEC-45, SPEC-46, SPEC-47, SPEC-48

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_cache
import uprof_native
import uprof_symbols


class ParseTest(unittest.TestCase):
    def parse(self, value):
        return uprof_native.parse_nt_symbol_path(value)

    def test_padded_srv_elements_are_stripped(self):
        parsed = self.parse(r"srv* C:\sym *https://msdl.microsoft.com/download/symbols ")

        self.assertEqual(parsed.caches, (r"C:\sym",))
        self.assertEqual(parsed.upstreams, ("https://msdl.microsoft.com/download/symbols",))
        self.assertEqual(parsed.dirs, ())

    def test_a_url_only_srv_entry_names_no_local_cache(self):
        parsed = self.parse("srv*https://symbols.example/store")

        self.assertEqual(parsed.caches, ())
        self.assertEqual(parsed.upstreams, ("https://symbols.example/store",))

    def test_chained_stores_are_all_local_caches(self):
        parsed = self.parse(r"srv*C:\near*\\share\far*https://symbols.example/store")

        self.assertEqual(parsed.caches, (r"C:\near", r"\\share\far"))
        self.assertEqual(parsed.upstreams, ("https://symbols.example/store",))

    def test_a_share_as_the_upstream_store_is_never_a_local_root(self):
        parsed = self.parse(r"srv*C:\sym*\\storage\symbols\index;srv*\\storage\only")

        self.assertEqual(parsed.caches, (r"C:\sym",))
        self.assertEqual(parsed.upstreams, (r"\\storage\symbols\index", r"\\storage\only"))

    def test_symsrv_skips_the_dll_name(self):
        parsed = self.parse(r"symsrv*symsrv.dll*C:\sym*https://symbols.example/store")

        self.assertEqual(parsed.caches, (r"C:\sym",))
        self.assertEqual(parsed.upstreams, ("https://symbols.example/store",))
        self.assertEqual(parsed.servers, ())
        self.assertIsNone(parsed.download_cache)

    def test_cache_and_plain_directory_entries_are_kept(self):
        parsed = self.parse(r"cache*C:\cached; C:\plain ;;")

        self.assertEqual(parsed.caches, (r"C:\cached",))
        self.assertEqual(parsed.dirs, (r"C:\plain",))
        self.assertEqual(parsed.servers, ())

    def test_translate_gets_the_first_server_cache_and_the_server_entries(self):
        value = (r"srv*https://a.example;srv**https://b.example;srv* C:\sym *https://c.example;"
                 r"C:\plain")

        cache, servers = uprof_native.parse_symbol_path(value)

        self.assertEqual(cache, r"C:\sym")
        self.assertEqual(servers, ["srv**https://b.example",
                                   r"srv*C:\sym*https://c.example"])


class CallersAgreeTest(unittest.TestCase):
    """Discovery searches exactly the local roots the session-cache key fingerprints."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="uprof-nt-symbol-path-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cache = self.root / "cache"
        self.plain = self.root / "plain"
        self.cache.mkdir()
        self.plain.mkdir()
        value = f"srv* {self.cache} *https://symbols.example/store; {self.plain} "
        patcher = mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": value})
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidates(self):
        return uprof_symbols._candidate_pdbs(
            self.root / "app" / "app.exe", "app.pdb", "ABCD", 1)

    def test_discovery_reads_the_stripped_cache_root(self):
        self.assertIn(self.cache / "app.pdb" / "ABCD1" / "app.pdb", self.candidates())

    def test_discovery_reads_a_plain_directory_entry(self):
        self.assertIn(self.plain / "app.pdb", self.candidates())

    def test_discovery_reads_a_cache_entry_like_a_store(self):
        with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": f"cache*{self.cache}"}):
            candidates = self.candidates()

        self.assertIn(self.cache / "app.pdb" / "ABCD1" / "app.pdb", candidates)

    def test_the_cache_key_fingerprints_the_same_roots(self):
        fingerprinted = {entry[0]
                         for entry in uprof_cache.search_identity(None, None, [])["srv_caches"]}

        self.assertIn(str(self.cache), fingerprinted)
        self.assertIn(str(self.plain), fingerprinted)

    def test_the_cache_key_follows_the_roots_not_the_spelling(self):
        def identity(value):
            with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": value}):
                return uprof_cache.search_identity(None, None, [])

        base = identity(f"srv*{self.cache}*https://a.example;{self.plain}")
        respelled = identity(f" ;srv* {self.cache} *https://b.example;; {self.plain} ")
        moved = identity(f"srv*{self.cache}*https://a.example;{self.root}")

        self.assertEqual(base, respelled)
        self.assertNotEqual(base, moved)

    def test_the_unconsulted_url_is_reported_once(self):
        search = uprof_symbols._choose_pdb(
            Path("llvm-pdbutil.exe"), self.root / "app" / "app.exe", "app.pdb", "ABCD", 1)

        self.assertEqual(search.urls, ["https://symbols.example/store"])


if __name__ == "__main__":
    unittest.main()
