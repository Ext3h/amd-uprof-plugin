"""The Parquet samples cache: exact values, reuse, invalidation and views.sql wiring."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

import uprof_db
import uprof_dump

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
HEADER = "DeltaTick,CPU_Number,TID,PID,Offset,ModuleName,IbsTagToRetCtr"
ROWS = [
    "1,0,7,100,16,c:\\windows\\system32\\ntdll.dll,5",
    "2,0,7,100,32,c:\\workload.exe,70000",
    "3,1,8,100,48,,not-a-number",
    "4,1,8,100,64,c:\\workload.exe,12",
]
VARIABLES = {
    "uprof_image": "%", "uprof_pid": "", "uprof_top": "20",
    "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
    "uprof_event_column": "", "uprof_tag_available": "false",
    "uprof_symbols": "", "uprof_aliases": "",
}


def _dump(directory: Path, rows=None, header=HEADER) -> Path:
    path = directory / "IbsOpDump.csv"
    path.write_text("\n".join([header, *(rows or ROWS)]) + "\n", encoding="utf-8", newline="\n")
    return path


def _info(dump: Path, cache: Path) -> uprof_dump.DumpInfo:
    return uprof_dump.DumpInfo(dump, "beside", cache_dir=cache)


def _session(directory: Path, dump: Path) -> Path:
    meta = {"collection": {"target": {"systemWide": True}}, "translateCommand": ""}
    (directory / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
    connection = duckdb.connect(str(directory / "cpu.db"))
    try:
        connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
        connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                           "functionId UBIGINT, moduleRVA UBIGINT)")
    finally:
        connection.close()
    return directory


class SamplesParquetTest(unittest.TestCase):
    def test_the_copy_carries_exactly_the_text_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            dump = _dump(directory)
            target = uprof_dump.samples_parquet(_info(dump, directory), directory)
            self.assertIsNotNone(target)
            self.assertTrue(target.is_file())
            meta = json.loads(Path(str(target) + ".json").read_text(encoding="utf-8"))
            self.assertEqual(meta["rows"], len(ROWS))
            self.assertEqual(meta["columns"], list(uprof_dump.SAMPLE_COLUMNS))
            self.assertEqual(meta["format"], uprof_dump._SAMPLES_FORMAT)
            connection = duckdb.connect()
            try:
                columns = ", ".join(f'"{name}"' for name in uprof_dump.SAMPLE_COLUMNS)
                copied = connection.execute(
                    f"SELECT {columns} FROM read_parquet(?) ORDER BY \"DeltaTick\"",
                    [str(target)]).fetchall()
                original = connection.execute(
                    f"SELECT {columns} FROM read_csv(?, header = true, all_varchar = true) "
                    "ORDER BY \"DeltaTick\"", [str(dump)]).fetchall()
            finally:
                connection.close()
            self.assertEqual(copied, original)

    def test_a_matching_dump_reuses_the_copy_and_a_changed_one_rebuilds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            dump = _dump(directory)
            dump_info = _info(dump, directory)
            first = uprof_dump.samples_parquet(dump_info, directory)
            stamp = first.stat().st_mtime_ns
            self.assertEqual(uprof_dump.samples_parquet(dump_info, directory), first)
            self.assertEqual(first.stat().st_mtime_ns, stamp)
            _dump(directory, rows=["9,0,7,100,80,c:\\workload.exe,3"])
            rebuilt = uprof_dump.samples_parquet(dump_info, directory)
            self.assertEqual(rebuilt, first)
            with duckdb.connect() as connection:
                rows = connection.execute(
                    "SELECT count(*), max(CAST(\"Offset\" AS BIGINT)) FROM read_parquet(?)",
                    [str(first)]).fetchall()
            self.assertEqual(rows, [(1, 80)])

    def test_a_dump_without_the_tag_column_falls_back_to_the_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            dump = _dump(directory, header="DeltaTick,CPU_Number,TID,PID,Offset,ModuleName")
            self.assertIsNone(uprof_dump.samples_parquet(_info(dump, directory), directory))

    def test_an_unsupported_dump_is_recorded_and_not_rehashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            dump = _dump(directory, header="DeltaTick,CPU_Number,TID,PID,Offset,ModuleName")
            info = _info(dump, directory)
            target, _meta = uprof_dump._samples_paths(dump, directory)
            marker = uprof_dump._unsupported_path(target)
            calls = 0
            real = uprof_dump._sha256_file

            def counting(path):
                nonlocal calls
                calls += 1
                return real(path)

            with mock.patch.object(uprof_dump, "_sha256_file", side_effect=counting):
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    self.assertIsNone(uprof_dump.samples_parquet(info, directory))
                first = calls
                self.assertTrue(marker.is_file())
                self.assertIn("WARNING", error.getvalue())
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertIsNone(uprof_dump.samples_parquet(info, directory))
                self.assertEqual(calls, first)

            dump.write_text("DeltaTick,CPU_Number,TID,PID,Offset,ModuleName\n"
                            "9,0,7,100,80,m.dll\n", encoding="utf-8", newline="\n")
            with mock.patch.object(uprof_dump, "_sha256_file", side_effect=counting):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertIsNone(uprof_dump.samples_parquet(info, directory))
            self.assertGreater(calls, first)

    def test_a_build_interrupted_leaves_no_staging_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            dump = _dump(directory)
            target, meta = uprof_dump._samples_paths(dump, directory)
            unsupported = uprof_dump._unsupported_path(target)
            key = uprof_dump._file_key(dump)
            self.assertIsNotNone(key)

            class FakeConnection:
                def execute(self, *_args, **_kwargs):
                    raise KeyboardInterrupt

                def close(self):
                    pass

            class FakeDuckDB:
                Error = Exception

                @staticmethod
                def connect(*_args, **_kwargs):
                    return FakeConnection()

            with mock.patch.dict(sys.modules, {"duckdb": FakeDuckDB}):
                with self.assertRaises(KeyboardInterrupt):
                    uprof_dump._build_samples(dump, target, meta, unsupported, directory,
                                              "sha", key)

            self.assertEqual(list(directory.glob("*.parquet.tmp")), [])


class ViewsSourceTest(unittest.TestCase):
    def _v_dump(self, parquet) -> list[tuple]:
        variables = dict(VARIABLES)
        variables["uprof_session_meta"] = str(self.session / "session.uprof")
        variables["uprof_ibs_dump"] = str(self.dump)
        variables["uprof_ibs_parquet"] = "" if parquet is None else str(parquet)
        with uprof_db.open_query(self.session / "cpu.db", [TOOLS_DIR / "views.sql"],
                                 variables) as connection:
            return connection.execute(
                "SELECT * FROM v_dump ORDER BY delta_tick, tag_cycles NULLS LAST").fetchall()

    def test_the_parquet_copy_produces_the_same_rows_as_the_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.dump = _dump(directory)
            session = directory / "session"
            session.mkdir()
            self.session = _session(session, self.dump)
            parquet = uprof_dump.samples_parquet(_info(self.dump, directory), directory)
            self.assertIsNotNone(parquet)
            from_parquet = self._v_dump(parquet)
            from_csv = self._v_dump(None)
            self.assertEqual(from_parquet, from_csv)
            self.assertEqual(len(from_csv), len(ROWS))


class SamplesMetadataTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="uprof-samples-meta-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.dump = _dump(self.root)
        self.info = _info(self.dump, self.root)
        self.target = uprof_dump.samples_parquet(self.info, self.root)
        self.assertIsNotNone(self.target)
        self.meta_path = Path(str(self.target) + ".json")

    def meta(self):
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def write_meta(self, **changes):
        self.meta_path.write_text(json.dumps({**self.meta(), **changes}), encoding="utf-8")

    def test_a_stale_format_is_rebuilt(self):
        stamp = self.target.stat().st_mtime_ns
        self.write_meta(format=0)

        rebuilt = uprof_dump.samples_parquet(self.info, self.root)

        self.assertEqual(rebuilt, self.target)
        self.assertEqual(self.meta()["format"], uprof_dump._SAMPLES_FORMAT)
        self.assertNotEqual(self.target.stat().st_mtime_ns, stamp)

    def test_stale_columns_are_rebuilt(self):
        self.write_meta(columns=["wrong"])

        uprof_dump.samples_parquet(self.info, self.root)

        self.assertEqual(self.meta()["columns"], list(uprof_dump.SAMPLE_COLUMNS))

    def test_a_matching_sha_updates_a_stale_source(self):
        stamp = self.target.stat().st_mtime_ns
        self.write_meta(source="elsewhere")

        reused = uprof_dump.samples_parquet(self.info, self.root)

        self.assertEqual(reused, self.target)
        self.assertEqual(self.meta()["source"], uprof_dump._normalized_source(self.dump))
        self.assertEqual(self.target.stat().st_mtime_ns, stamp)

    def test_a_truncated_copy_is_rebuilt(self):
        self.target.write_bytes(b"")

        rebuilt = uprof_dump.samples_parquet(self.info, self.root)

        self.assertEqual(rebuilt, self.target)
        self.assertGreater(self.target.stat().st_size, 0)
        with duckdb.connect() as connection:
            rows = connection.execute("SELECT count(*) FROM read_parquet(?)",
                                      [str(self.target)]).fetchone()[0]
        self.assertEqual(rows, len(ROWS))

    def test_a_corrupt_same_size_copy_is_rebuilt(self):
        corrupted = b"\0" * self.target.stat().st_size
        self.target.write_bytes(corrupted)

        rebuilt = uprof_dump.samples_parquet(self.info, self.root)

        self.assertEqual(rebuilt, self.target)
        self.assertNotEqual(self.target.read_bytes(), corrupted)

    def test_a_missing_copy_is_rebuilt(self):
        self.target.unlink()

        rebuilt = uprof_dump.samples_parquet(self.info, self.root)

        self.assertEqual(rebuilt, self.target)
        self.assertTrue(self.target.is_file())


if __name__ == "__main__":
    unittest.main()
