"""The per-session cache keys on identity, rejects stale/transient runs and degrades safely."""

import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import duckdb

import uprof
import uprof_cache
import uprof_db
import uprof_query


RVAS = [("a.dll", "c:/a.dll", "0x10")]
DISK = [["c:/a.dll", True, 10, 20]]
MODULES = [["c:/a.dll", 10, 1, False, True]]
TOOLS = {"symbolizer": ["s", 1, 1], "pdbutil": ["p", 1, 1], "readobj": ["r", 1, 1]}
SEARCH = {"symbol_files": [], "symbol_dirs": [], "image_files": [], "image_dirs": [],
          "nt_symbol_path": "", "symbol_servers": []}


_STORAGE_ERROR = ("Trying to read a database file with storage version number 99, but we can "
                  "only read storage versions between 64 and 70.")


class _StorageMismatch:
    """A capture connection whose cache ATTACH fails as a newer DuckDB file would."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, *args):
        if sql.startswith("ATTACH"):
            raise duckdb.IOException(_STORAGE_ERROR)
        return self._connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class KeyTest(unittest.TestCase):
    def test_the_symbol_key_is_stable_and_covers_each_identity_input(self):
        base = uprof_cache.symbol_key(RVAS, MODULES, DISK, TOOLS, SEARCH)
        self.assertEqual(base, uprof_cache.symbol_key(RVAS, MODULES, DISK, TOOLS, SEARCH))
        checksum = [["c:/a.dll", 10, 2, False, True]]
        self.assertNotEqual(base, uprof_cache.symbol_key(RVAS, checksum, DISK, TOOLS, SEARCH))
        rvas = RVAS + [("a.dll", "c:/a.dll", "0x20")]
        self.assertNotEqual(base, uprof_cache.symbol_key(rvas, MODULES, DISK, TOOLS, SEARCH))
        tools = dict(TOOLS, readobj=["r2", 1, 1])
        self.assertNotEqual(base, uprof_cache.symbol_key(RVAS, MODULES, DISK, tools, SEARCH))
        search = dict(SEARCH, nt_symbol_path="srv*c:/symbols*https://example.invalid")
        self.assertNotEqual(base, uprof_cache.symbol_key(RVAS, MODULES, DISK, TOOLS, search))
        disk = [["c:/a.dll", True, 11, 20]]
        self.assertNotEqual(base, uprof_cache.symbol_key(RVAS, MODULES, disk, TOOLS, SEARCH))

    def test_the_range_key_is_bound_to_the_symbols_and_readobj(self):
        base = uprof_cache.ranges_key([["a.dll", "c:/a.dll"]], MODULES, DISK,
                                      TOOLS["readobj"], "symbols")
        self.assertNotEqual(base, uprof_cache.ranges_key(
            [["a.dll", "c:/a.dll"]], MODULES, DISK, TOOLS["readobj"], "other"))
        self.assertNotEqual(base, uprof_cache.ranges_key(
            [["a.dll", "c:/a.dll"]], MODULES, DISK, ["r2", 1, 1], "symbols"))

    def test_lock_errors_and_unfinished_results_are_distinguished(self):
        self.assertTrue(uprof_cache.SessionCache._lock_error(
            Exception("IO Error: Could not set lock on file: conflicting lock")))
        self.assertTrue(uprof_cache.SessionCache._lock_error(
            Exception("The process cannot access the file because it is being used by "
                      "another process")))
        self.assertTrue(uprof_cache.SessionCache._lock_error(
            Exception("Resource In Use Error: Unique file handle conflict: Cannot attach "
                      "b2 - the database file is already attached")))
        self.assertFalse(uprof_cache.SessionCache._lock_error(
            Exception("Resource In Use Error: connection has an unfinished result")))

    def test_the_search_identity_tracks_override_file_content(self):
        with tempfile.TemporaryDirectory(prefix="uprof-search-") as tmp:
            image = Path(tmp) / "a.dll"
            image.write_bytes(b"one")
            first = uprof_cache.search_identity(None, [str(image)], ["a.dll"])
            image.write_bytes(b"two-two")
            second = uprof_cache.search_identity(None, [str(image)], ["a.dll"])
        self.assertNotEqual(first["image_files"], second["image_files"])

    def test_the_search_identity_tracks_a_pdb_staged_into_a_srv_cache(self):
        with tempfile.TemporaryDirectory(prefix="uprof-srv-") as tmp:
            root = Path(tmp)
            staged = root / "a.pdb"
            staged.mkdir()
            with mock.patch.dict(os.environ, {
                    "_NT_SYMBOL_PATH": f"srv*{root}*https://example.invalid"}):
                first = uprof_cache.search_identity(None, None, [])
                (staged / "GUIDAGE").mkdir()
                second = uprof_cache.search_identity(None, None, [])
        self.assertNotEqual(first["srv_caches"], second["srv_caches"])

    def test_ambiguous_modules_are_detected(self):
        connection = duckdb.connect()
        connection.execute(
            "CREATE TEMP MACRO module_key(p) AS replace(lower(p), chr(92), '/')")
        connection.execute("CREATE TABLE Module(id INTEGER, path VARCHAR, size UBIGINT, "
                           "checksum UBIGINT, is32Bit BOOLEAN)")
        connection.execute("INSERT INTO Module VALUES (1, 'C:/a.dll', 10, 1, false), "
                           "(2, 'C:/a.dll', 10, 2, false), (3, 'C:/b.dll', 10, 1, false)")
        self.assertEqual(uprof_cache.ambiguous_modules(connection, ["c:/a.dll", "c:/b.dll"]),
                         ["c:/a.dll"])
        connection.close()


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-cache-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        duckdb.connect(str(self.session / "cpu.db")).close()
        self.env = mock.patch.dict(os.environ, {
            "UPROF_SESSION_CACHE_ROOT": str(self.root / "cache")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.symbols = self.root / "symbols.parquet"
        connection = duckdb.connect()
        connection.execute(
            "COPY (SELECT 'a.dll' AS module_name, '16' AS address, 'f' AS function, "
            "'f' AS function_short, 'f' AS containing_function, 'x.c' AS file, "
            "1 AS line) TO ? (FORMAT parquet)", [str(self.symbols)])
        connection.close()
        self.publics = self.root / "publics.csv"
        self.publics.write_text(
            "module_name,rva,name_count,names\na.dll,16,1,f\n", encoding="utf-8")
        self.pdbs = self.root / "pdbs.csv"
        self.pdbs.write_text("module_name,pdb\na.dll,c:/a.pdb\n", encoding="utf-8")
        self.pdb_file = self.root / "a.pdb"
        self.pdb_file.write_bytes(b"pdb-one")
        self.ranges = self.root / "functions.csv"
        self.ranges.write_text(
            "module_name,function_start,function_end,function_source,function_name\n"
            "a.dll,16,32,export,f\n", encoding="utf-8")

    def pdb_identity(self):
        stat = self.pdb_file.stat()
        return [{"path": str(self.pdb_file), "size": stat.st_size,
                 "mtime_ns": stat.st_mtime_ns}]

    def test_a_run_is_stored_and_replayed_by_identity(self):
        capture = duckdb.connect()
        stream = io.StringIO()
        writer = uprof_cache.SessionCache(capture, self.session, stream, timeout=2)
        self.assertTrue(writer.ensure_write())
        writer.store_symbols("k1", self.symbols, self.publics, self.pdbs,
                             {"a.dll": "cause"}, "symbol text\n", False, self.pdb_identity(),
                             [])
        writer.store_ranges("k1", self.ranges,
                            {"modules": 1, "ranges": 1, "absent_by_module": {},
                             "attempted": ["a.dll"]}, "range text\n")
        writer.close()
        capture.close()

        reader = duckdb.connect()
        replay = uprof_cache.SessionCache(reader, self.session, io.StringIO(), timeout=2)
        self.assertTrue(replay.lookup("symbol_runs", "k1"))
        self.assertTrue(replay.symbols_valid("k1"))
        self.assertTrue(replay.lookup("range_runs", "k1"))
        replay.create_symbol_views("k1")
        replay.create_range_view("k1")
        self.assertEqual(
            reader.execute("SELECT module_name, address, function FROM uprof_symbols_source")
            .fetchall(), [("a.dll", "16", "f")])
        self.assertEqual(
            reader.execute("SELECT module_name, CAST(rva AS BIGINT) FROM uprof_aliases_source")
            .fetchall(), [("a.dll", 16)])
        self.assertEqual(
            reader.execute("SELECT module_name, pdb FROM uprof_pdbs_source").fetchall(),
            [("a.dll", "c:/a.pdb")])
        self.assertEqual(
            reader.execute("SELECT module_name, CAST(function_start AS BIGINT), function_name "
                           "FROM uprof_functions_source").fetchall(), [("a.dll", 16, "f")])
        evidence, symbol_text = replay.symbol_meta("k1")
        self.assertEqual(evidence, {"a.dll": "cause"})
        self.assertEqual(symbol_text, "symbol text\n")
        info, range_text = replay.range_meta("k1")
        self.assertEqual(info["attempted"], ["a.dll"])
        self.assertEqual(range_text, "range text\n")
        self.assertFalse(replay.lookup("symbol_runs", "missing"))
        replay.close()
        reader.close()

    def test_a_changed_pdb_invalidates_the_run(self):
        capture = duckdb.connect()
        writer = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=2)
        self.assertTrue(writer.ensure_write())
        writer.store_symbols("k1", self.symbols, self.publics, self.pdbs, {}, "", False,
                             self.pdb_identity(), [])
        self.assertTrue(writer.symbols_valid("k1"))
        self.pdb_file.write_bytes(b"pdb-two-changed")
        self.assertFalse(writer.symbols_valid("k1"))
        writer.close()
        capture.close()

    def test_a_transient_run_is_not_served(self):
        capture = duckdb.connect()
        writer = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=2)
        self.assertTrue(writer.ensure_write())
        writer.store_symbols("k1", self.symbols, self.publics, self.pdbs, {}, "", True,
                             [], [])
        self.assertFalse(writer.symbols_valid("k1"))
        writer.close()
        capture.close()

    def test_a_second_writer_does_not_hang(self):
        capture = duckdb.connect()
        first = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=0.2)
        self.assertTrue(first.ensure_write())
        second_capture = duckdb.connect()
        second = uprof_cache.SessionCache(second_capture, self.session, io.StringIO(), timeout=0.2)
        self.assertFalse(second.ensure_write())
        first.close()
        second_capture.close()
        capture.close()

    def test_an_unwritable_root_degrades_instead_of_failing(self):
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with mock.patch.dict(os.environ, {"UPROF_SESSION_CACHE_ROOT": str(blocker / "cache")}):
            capture = duckdb.connect()
            cache = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=0.2)
            self.assertFalse(cache.ensure_write())
            self.assertTrue(cache.disabled)
            capture.close()

    def test_the_off_switch_disables_the_cache(self):
        with mock.patch.object(uprof_cache, "DISABLE", True):
            capture = duckdb.connect()
            cache = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=0.2)
            self.assertTrue(cache.disabled)
            self.assertFalse(cache.ensure_write())
            capture.close()

    def test_a_corrupt_database_is_moved_aside_and_recreated(self):
        path = uprof_cache.cache_db_path(self.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a duckdb database")
        capture = duckdb.connect()
        cache = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=0.2)
        self.assertFalse(cache.lookup("symbol_runs", "k1"))
        self.assertFalse(cache.disabled)
        self.assertTrue(cache.ensure_write())
        self.assertTrue(path.is_file())
        self.assertTrue(path.with_name(path.name + ".corrupt").is_file())
        capture.close()

    def test_a_missing_table_is_repaired(self):
        path = uprof_cache.cache_db_path(self.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(str(path))
        connection.execute("CREATE TABLE cache_meta(version INTEGER, session_path VARCHAR)")
        connection.execute("INSERT INTO cache_meta VALUES (2, ?)", [str(self.session)])
        connection.close()
        capture = duckdb.connect()
        cache = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=0.2)
        self.assertFalse(cache.lookup("symbol_runs", "k1"))
        self.assertFalse(cache.disabled)
        self.assertTrue(cache.ensure_write())
        capture.close()

    def test_the_size_cap_refuses_to_store(self):
        capture = duckdb.connect()
        stream = io.StringIO()
        cache = uprof_cache.SessionCache(capture, self.session, stream, timeout=2)
        self.assertTrue(cache.ensure_write())
        with mock.patch.object(uprof_cache, "MAX_BYTES", 1):
            cache.store_symbols("k1", self.symbols, self.publics, self.pdbs, {}, "", False,
                                [], [])
        self.assertEqual(
            capture.execute("SELECT count(*) FROM uprof_cache.symbol_runs").fetchone()[0], 0)
        self.assertIn("cap", stream.getvalue())
        capture.close()

    def test_orphan_cleanup_removes_gone_sessions(self):
        root = self.root / "orphans"
        root.mkdir()
        path = root / ("0" * 32 + ".duckdb")
        connection = duckdb.connect(str(path))
        connection.execute("CREATE TABLE cache_meta(version INTEGER, session_path VARCHAR)")
        connection.execute("INSERT INTO cache_meta VALUES (2, ?)",
                           [str(self.root / "gone-session")])
        connection.close()
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        with mock.patch.object(uprof_cache, "_CLEANED", False):
            uprof_cache.cleanup_orphans(root)
        self.assertFalse(path.exists())

    def _old(self, path):
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))

    def _cleanup(self, root):
        with mock.patch.object(uprof_cache, "_CLEANED", False):
            uprof_cache.cleanup_orphans(root)

    def test_orphan_cleanup_leaves_files_without_a_digest_name(self):
        root = self.root / "shared"
        root.mkdir()
        foreign = root / "user-data.duckdb"
        foreign.write_bytes(b"not ours")
        quarantined = root / "user-data.duckdb.corrupt"
        quarantined.write_bytes(b"not ours either")
        readable = root / "Analysis.duckdb"
        connection = duckdb.connect(str(readable))
        connection.execute("CREATE TABLE cache_meta(version INTEGER, session_path VARCHAR)")
        connection.execute("INSERT INTO cache_meta VALUES (3, ?)",
                           [str(self.root / "gone-session")])
        connection.close()
        for path in (foreign, quarantined, readable):
            self._old(path)
        self._cleanup(root)
        self.assertTrue(foreign.is_file())
        self.assertTrue(quarantined.is_file())
        self.assertTrue(readable.is_file())

    def test_orphan_cleanup_still_removes_an_old_unreadable_digest_file(self):
        root = self.root / "own"
        root.mkdir()
        path = root / ("2" * 32 + ".duckdb")
        path.write_bytes(b"truncated cache")
        self._old(path)
        self._cleanup(root)
        self.assertFalse(path.exists())

    def test_orphan_cleanup_skips_a_newer_schema_version(self):
        root = self.root / "newer"
        root.mkdir()
        path = root / ("3" * 32 + ".duckdb")
        connection = duckdb.connect(str(path))
        connection.execute("CREATE TABLE cache_meta(version INTEGER, session_path VARCHAR)")
        connection.execute("INSERT INTO cache_meta VALUES (?, ?)",
                           [uprof_cache.SCHEMA_VERSION + 1, str(self.root / "gone-session")])
        connection.close()
        self._cleanup(root)
        self.assertTrue(path.is_file())

    def test_orphan_cleanup_skips_another_duckdb_storage_version(self):
        root = self.root / "storage"
        root.mkdir()
        path = root / ("4" * 32 + ".duckdb")
        path.write_bytes(b"written by another duckdb")
        self._old(path)

        def newer(*_args, **_kwargs):
            raise duckdb.IOException(_STORAGE_ERROR)

        with mock.patch.object(duckdb, "connect", newer):
            self._cleanup(root)
        self.assertTrue(path.is_file())

    def test_another_duckdb_storage_version_is_skipped_not_moved_aside(self):
        path = uprof_cache.cache_db_path(self.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"written by another duckdb")
        capture = _StorageMismatch(duckdb.connect())
        stream = io.StringIO()
        cache = uprof_cache.SessionCache(capture, self.session, stream, timeout=0.2)
        self.assertFalse(cache.ensure_write())
        self.assertEqual(path.read_bytes(), b"written by another duckdb")
        self.assertFalse(path.with_name(path.name + ".corrupt").exists())
        self.assertIn("storage version", stream.getvalue())
        capture.close()

    def test_a_newer_schema_version_is_skipped_not_rebuilt(self):
        path = uprof_cache.cache_db_path(self.session)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(str(path))
        connection.execute("CREATE TABLE cache_meta(version INTEGER, session_path VARCHAR)")
        connection.execute("INSERT INTO cache_meta VALUES (?, ?)",
                           [uprof_cache.SCHEMA_VERSION + 1, str(self.session)])
        connection.execute("CREATE TABLE symbol_runs(run_key VARCHAR, future VARCHAR)")
        connection.execute("INSERT INTO symbol_runs VALUES ('k', 'kept')")
        connection.close()
        capture = duckdb.connect()
        cache = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=0.2)
        self.assertFalse(cache.ensure_write())
        capture.close()
        connection = duckdb.connect(str(path), read_only=True)
        self.assertEqual(connection.execute("SELECT future FROM symbol_runs").fetchall(),
                         [("kept",)])
        self.assertEqual(connection.execute("SELECT max(version) FROM cache_meta").fetchall(),
                         [(uprof_cache.SCHEMA_VERSION + 1,)])
        connection.close()

    def test_views_sql_can_run_without_the_tool_created_source_views(self):
        connection = duckdb.connect()
        connection.execute("CREATE TABLE Process(id UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE Module(id UINTEGER, path VARCHAR, "
                           "isSystemModule BOOLEAN)")
        connection.execute("CREATE TABLE Function(id UBIGINT, moduleId UINTEGER, name VARCHAR, "
                           "startOffset UBIGINT, size UBIGINT, srcfileId UINTEGER)")
        connection.execute("CREATE TABLE UnifiedSampleSeries(moduleId UINTEGER, "
                           "functionId UBIGINT, moduleRVA UBIGINT)")
        dump = self.root / "dump.csv"
        dump.write_text("DeltaTick,CPU_Number,TID,PID,Offset,ModuleName\n"
                        "1,0,1,100,4096,a.dll\n", encoding="utf-8")
        args = uprof.build_parser().parse_args(
            ["profile", "--session", str(self.session), "--by", "process"])
        variables = uprof_query._variables(args, None, 20, "0", "2147483647", None)
        variables.update({"uprof_ibs_dump": str(dump), "uprof_ibs_parquet": "",
                          "uprof_symbols": str(self.symbols),
                          "uprof_aliases": str(self.publics),
                          "uprof_pdbs": str(self.pdbs),
                          "uprof_functions": str(self.ranges)})
        uprof_db.set_variables(connection, variables)
        uprof_db.run_script(connection, uprof_query.TOOLS_DIR / "views.sql")
        self.assertEqual(connection.execute(
            "SELECT module_name, CAST(rva AS BIGINT), function FROM v_symbols").fetchall(),
            [("a.dll", 16, "f")])
        self.assertEqual(connection.execute(
            "SELECT module_name, name_count FROM v_public_starts").fetchall(), [("a.dll", 1)])
        self.assertEqual(connection.execute(
            "SELECT module_name, CAST(function_start AS BIGINT) FROM v_function_ranges")
            .fetchall(), [("a.dll", 16)])
        self.assertEqual(connection.execute("SELECT module_name, pdb FROM v_pdbs").fetchall(),
                         [("a.dll", "c:/a.pdb")])
        connection.close()

    def test_module_identities_use_the_columns_the_capture_has(self):
        connection = duckdb.connect()
        connection.execute(
            "CREATE TEMP MACRO module_key(p) AS replace(lower(p), chr(92), '/')")
        connection.execute("CREATE TABLE Module(id INTEGER, path VARCHAR)")
        connection.execute("INSERT INTO Module VALUES (1, 'C:/a.dll')")
        self.assertEqual(uprof_cache.module_identity_rows(connection, ["c:/a.dll"]),
                         [["C:/a.dll", None, None, None, None]])
        connection.execute("ALTER TABLE Module ADD COLUMN size UBIGINT")
        connection.execute("ALTER TABLE Module ADD COLUMN checksum UBIGINT")
        connection.execute("ALTER TABLE Module ADD COLUMN is32Bit BOOLEAN")
        connection.execute("ALTER TABLE Module ADD COLUMN foundDebugInfo BOOLEAN")
        connection.execute("UPDATE Module SET size = 10, checksum = 1, "
                           "is32Bit = false, foundDebugInfo = true")
        self.assertEqual(uprof_cache.module_identity_rows(connection, ["c:/a.dll"]),
                         [["C:/a.dll", 10, 1, False, True]])
        connection.close()


    def test_an_overwritten_probe_invalidates_the_run(self):
        probe = self.root / "candidate.pdb"
        probe.write_bytes(b"one")
        stat = probe.stat()
        identity = [{"path": str(probe), "exists": True, "size": stat.st_size,
                     "mtime_ns": stat.st_mtime_ns}]
        capture = duckdb.connect()
        cache = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=2)
        self.assertTrue(cache.ensure_write())
        cache.store_symbols("k1", self.symbols, self.publics, self.pdbs, {}, "", False,
                            [], identity)
        self.assertTrue(cache.symbols_valid("k1"))
        probe.write_bytes(b"two-replaced")
        self.assertFalse(cache.symbols_valid("k1"))
        capture.close()

    def test_a_probe_that_appears_invalidates_the_run(self):
        probe = self.root / "later.pdb"
        identity = [{"path": str(probe), "exists": False}]
        capture = duckdb.connect()
        cache = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=2)
        self.assertTrue(cache.ensure_write())
        cache.store_symbols("k1", self.symbols, self.publics, self.pdbs, {}, "", False,
                            [], identity)
        self.assertTrue(cache.symbols_valid("k1"))
        probe.write_bytes(b"appeared")
        self.assertFalse(cache.symbols_valid("k1"))
        capture.close()

    def test_version_identity_requires_the_capture_columns(self):
        connection = duckdb.connect()
        connection.execute(
            "CREATE TEMP MACRO module_key(p) AS replace(lower(p), chr(92), '/')")
        connection.execute("CREATE TABLE Module(id INTEGER, path VARCHAR)")
        self.assertFalse(uprof_cache.version_identity_verifiable(connection))
        for column, kind in (("size", "UBIGINT"), ("checksum", "UBIGINT"),
                             ("is32Bit", "BOOLEAN")):
            connection.execute(f"ALTER TABLE Module ADD COLUMN {column} {kind}")
        self.assertTrue(uprof_cache.version_identity_verifiable(connection))
        connection.close()

    def test_busy_is_memoized_and_does_not_disable(self):
        capture = duckdb.connect()
        first = uprof_cache.SessionCache(capture, self.session, io.StringIO(), timeout=0.2)
        self.assertTrue(first.ensure_write())
        second_capture = duckdb.connect()
        second = uprof_cache.SessionCache(second_capture, self.session, io.StringIO(), timeout=0.2)
        self.assertFalse(second.ensure_write())
        self.assertTrue(second.busy)
        started = time.monotonic()
        self.assertFalse(second.ensure_write())
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertFalse(second.disabled)
        first.close()
        second_capture.close()
        capture.close()

    def test_a_rewritten_cpu_db_removes_the_stale_sibling(self):
        root = self.root / "orphans2"
        root.mkdir()
        sibling = root / ("1" * 32 + ".duckdb")
        connection = duckdb.connect(str(sibling))
        connection.execute("CREATE TABLE cache_meta(version INTEGER, session_path VARCHAR)")
        connection.execute("INSERT INTO cache_meta VALUES (3, ?)", [str(self.session)])
        connection.close()
        with mock.patch.object(uprof_cache, "_CLEANED", False):
            uprof_cache.cleanup_orphans(root)
        self.assertFalse(sibling.exists())

    def test_env_float_rejects_non_finite_values(self):
        with mock.patch.dict(os.environ, {"UPROF_PROBE": "inf"}):
            self.assertEqual(uprof_cache._env_float("UPROF_PROBE", 5.0), 5.0)
        with mock.patch.dict(os.environ, {"UPROF_PROBE": "nan"}):
            self.assertEqual(uprof_cache._env_float("UPROF_PROBE", 5.0), 5.0)
        with mock.patch.dict(os.environ, {"UPROF_PROBE": "7"}):
            self.assertEqual(uprof_cache._env_float("UPROF_PROBE", 5.0), 7.0)


    def test_files_in_only_stats_matching_names(self):
        directory = self.root / "scan"
        directory.mkdir()
        (directory / "match.dll").write_bytes(b"1")
        (directory / "other.txt").write_bytes(b"2")
        entries = uprof_cache._files_in(directory, {"match.dll"})
        self.assertEqual([Path(entry[0]).name for entry in entries], ["match.dll"])


class _FailingCache:
    def __init__(self):
        self.disabled = []

    def symbol_meta(self, key):
        raise RuntimeError("read failed")

    def create_symbol_views(self, key):
        raise AssertionError("must not be reached")

    def range_meta(self, key):
        raise RuntimeError("read failed")

    def create_range_view(self, key):
        raise AssertionError("must not be reached")

    def disable(self, exc):
        self.disabled.append(exc)


class HitGuardTest(unittest.TestCase):
    def test_a_symbol_hit_read_failure_falls_back(self):
        cache = _FailingCache()
        ok, causes = uprof_query._apply_symbol_hit(cache, None, "k", {}, io.StringIO())
        self.assertFalse(ok)
        self.assertEqual(causes, {})
        self.assertTrue(cache.disabled)

    def test_a_range_hit_read_failure_falls_back(self):
        cache = _FailingCache()
        ok, info = uprof_query._apply_range_hit(cache, None, "k", {}, io.StringIO())
        self.assertFalse(ok)
        self.assertIsNone(info)
        self.assertTrue(cache.disabled)



class EndToEndHitTest(unittest.TestCase):
    """Two CLI runs on one synthesized session: store then hit, and a PDB touch misses."""

    def setUp(self):
        from test_function_region_profile import FIXTURE, Session
        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache_root = self.root / "cache"
        self.env = mock.patch.dict(os.environ, {
            "UPROF_SESSION_CACHE_ROOT": str(self.cache_root),
            "_NT_SYMBOL_PATH": f"srv*{self.root / 'srv'}*https://example.invalid"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # The replay comparison is exact, and Linux re-reads the current core frequency from
        # /proc/cpuinfo on every run, so pin it for both runs.
        self.frequency = mock.patch("uprof_collect._core_frequency",
                                    return_value=(4_700_000_000, "4700 MHz", "test"))
        self.frequency.start()
        self.addCleanup(self.frequency.stop)
        self.pdb = self.root / "fixture.pdb"
        self.pdb.write_bytes(b"fake pdb")
        session = Session(self.root, "e2e")
        session.module(1, FIXTURE).function(10, 1, "fixture_hot")
        session.sample(1, 10, 0x101B, 2)
        self.session = session.write()
        stat = FIXTURE.stat()
        connection = duckdb.connect(str(self.session / "cpu.db"))
        for column, kind in (("size", "UBIGINT"), ("checksum", "UBIGINT"),
                             ("is32Bit", "BOOLEAN"), ("foundDebugInfo", "BOOLEAN")):
            connection.execute(f"ALTER TABLE Module ADD COLUMN {column} {kind}")
        connection.execute("UPDATE Module SET size = ?, checksum = 7, is32Bit = false, "
                           "foundDebugInfo = true", [stat.st_size])
        connection.close()
        self.module_name = str(FIXTURE).replace("\\", "/").lower()
        self.calls = 0

    def fake_symbolize(self, modules, out_dir, **kwargs):
        self.calls += 1
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        symbols = out / "symbols.parquet"
        connection = duckdb.connect()
        connection.execute(
            "COPY (SELECT ? AS module_name, '0x101B' AS address, 'fixture_hot' AS function, "
            "'fixture_hot' AS function_short, 'fixture_hot' AS containing_function, "
            "'fixture.s' AS file, 27 AS line) TO ? (FORMAT parquet)",
            [self.module_name, str(symbols)])
        connection.close()
        publics = out / "symbols.publics.csv"
        publics.write_text("module_name,rva,name_count,names\n", encoding="utf-8")
        pdbs = out / "symbols.pdbs.csv"
        pdbs.write_text(f"module_name,pdb\n{self.module_name},{self.pdb}\n", encoding="utf-8")
        return symbols, publics, pdbs, []

    def run_cli(self):
        import contextlib
        import io
        import uprof
        import uprof_query
        args = uprof.build_parser().parse_args(
            ["profile", "--session", str(self.session), "--by", "line"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(uprof_query.uprof_symbols, "symbolize_modules",
                               side_effect=self.fake_symbolize), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            uprof_query.run_query_command(args)
        return out.getvalue(), err.getvalue()

    def test_second_run_hits_and_a_pdb_touch_rederives(self):
        first_out, first_err = self.run_cli()
        self.assertIn("ARTIFACTS=", first_err)
        second_out, second_err = self.run_cli()
        self.assertEqual(second_out, first_out)
        self.assertNotIn("ARTIFACTS=", second_err)
        self.assertEqual(self.calls, 1)
        self.pdb.write_bytes(b"replaced pdb content")
        third_out, third_err = self.run_cli()
        self.assertIn("ARTIFACTS=", third_err)
        self.assertEqual(self.calls, 2)



class EndToEndHitInsideSrvCacheTest(EndToEndHitTest):
    """The default layout: the session cache root, not yet created, inside the srv* cache."""

    def setUp(self):
        super().setUp()
        (self.root / "srv").mkdir()
        self.cache_root = self.root / "srv" / "uprof-session-cache"
        env = mock.patch.dict(os.environ, {"UPROF_SESSION_CACHE_ROOT": str(self.cache_root)})
        env.start()
        self.addCleanup(env.stop)


if __name__ == "__main__":
    unittest.main()
