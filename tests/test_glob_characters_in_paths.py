"""A capture path with glob characters reads its own files, never a sibling's.

DuckDB's read_csv, read_json and glob treat '[', '*' and '?' as pattern characters, and Windows
allows '[' and ']' in directory names. Beside capture[1], a sibling capture1 matches the pattern
capture[1], so every read of a capture file must match its path literally.
"""

import json
import tempfile
import unittest
from pathlib import Path

import duckdb
import uprof_context
import uprof_db
import uprof_dump
import uprof_util
from test_context_annotate import DUMP
from test_dump_parquet_cache import ROWS, TOOLS_DIR, VARIABLES, _dump, _info, _session


class LiteralPathTest(unittest.TestCase):
    def test_the_python_and_sql_twins_escape_alike(self):
        macro = next(
            line
            for line in (TOOLS_DIR / "views.sql").read_text(encoding="utf-8").splitlines()
            if line.startswith("CREATE OR REPLACE TEMP MACRO literal_path(")
        )
        connection = duckdb.connect()
        self.addCleanup(connection.close)
        connection.execute(macro)
        for path, expected in (
            (r"C:\captures\run", r"C:\captures\run"),
            (r"C:\capture[1]\d.csv", r"C:\capture[[]1]\d.csv"),
            ("/tmp/a*b", "/tmp/a[*]b"),
            ("/tmp/a?b", "/tmp/a[?]b"),
            ("/tmp/a]b", "/tmp/a]b"),
        ):
            with self.subTest(path=path):
                self.assertEqual(uprof_util.literal_path(path), expected)
                sql = connection.execute("SELECT literal_path(?)", [path]).fetchone()[0]
                self.assertEqual(sql, expected)


class _Siblings(unittest.TestCase):
    """capture[1] with the real data and capture1 beside it with other data."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="uprof-glob-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.capture = self.root / "capture[1]"
        self.sibling = self.root / "capture1"
        self.capture.mkdir()
        self.sibling.mkdir()


class SamplesParquetTest(_Siblings):
    def test_the_parquet_copy_reads_the_named_dump(self):
        dump = _dump(self.capture)
        _dump(self.sibling, rows=ROWS[:1])
        target = uprof_dump.samples_parquet(_info(dump, self.root), self.root)
        self.assertIsNotNone(target)
        rows = duckdb.connect().execute("SELECT count(*) FROM read_parquet(?)", [str(target)])
        self.assertEqual(rows.fetchone()[0], len(ROWS))


class ViewsTest(_Siblings):
    def _query(self, sql):
        dump = _dump(self.capture)
        _dump(self.sibling, rows=ROWS[:1])
        session = _session(self.capture, dump)
        sibling_meta = {"collection": {}, "translateCommand": "sibling"}
        (self.sibling / "session.uprof").write_text(json.dumps(sibling_meta), encoding="utf-8")
        variables = dict(VARIABLES)
        variables["uprof_session_meta"] = str(session / "session.uprof")
        variables["uprof_ibs_dump"] = str(dump)
        variables["uprof_ibs_parquet"] = ""
        with uprof_db.open_query(
            session / "cpu.db", [TOOLS_DIR / "views.sql"], variables
        ) as connection:
            return connection.execute(sql).fetchall()

    def test_the_dump_view_reads_the_named_dump(self):
        self.assertEqual(self._query("SELECT count(*) FROM v_dump"), [(len(ROWS),)])

    def test_the_capture_span_reads_the_named_session_file(self):
        self.assertEqual(self._query("SELECT translateCommand FROM v_capture_span"), [("",)])

    def test_a_missing_dump_is_not_found_although_a_sibling_matches(self):
        _dump(self.sibling)
        variables = dict(VARIABLES)
        variables["uprof_session_meta"] = ""
        variables["uprof_ibs_dump"] = str(self.capture / "IbsOpDump.csv")
        variables["uprof_ibs_parquet"] = ""
        session = _session(self.capture, self.capture / "IbsOpDump.csv")
        with (
            self.assertRaisesRegex(uprof_util.UprofError, "IBS ASCII dump not found"),
            uprof_db.open_query(session / "cpu.db", [TOOLS_DIR / "views.sql"], variables),
        ):
            pass


class IbsRowsTest(_Siblings):
    def test_instruction_context_reads_the_named_dump(self):
        dump = self.capture / "IbsOpDump.csv"
        dump.write_text(DUMP, encoding="utf-8", newline="\n")
        lines = DUMP.splitlines()
        (self.sibling / "IbsOpDump.csv").write_text(
            "\n".join(lines[:2]) + "\n", encoding="utf-8", newline="\n"
        )
        connection = duckdb.connect()
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE OR REPLACE TEMP MACRO module_key(p) AS replace(lower(p), chr(92), '/')"
        )
        connection.execute(
            "CREATE OR REPLACE TEMP MACRO image_matches(name, pattern) AS "
            "COALESCE(name, '') ILIKE pattern"
        )
        connection.execute("CREATE TABLE Process(id INTEGER, name VARCHAR)")
        connection.execute(
            "INSERT INTO Process VALUES (4524, 'c:\\capture\\a.exe'), (7132, 'c:\\capture\\b.exe')"
        )
        connection.execute("SET VARIABLE uprof_ibs_dump = ?", [str(dump)])
        connection.execute("SET VARIABLE time_start = 0")
        connection.execute("SET VARIABLE time_end = 1000")
        connection.execute("SET VARIABLE uprof_pid = ''")
        connection.execute("SET VARIABLE uprof_image = '%'")
        rows = uprof_context.ibs_rows(connection, "mod.dll", [0x100])
        self.assertEqual(rows[0x100]["samples"], 4)


if __name__ == "__main__":
    unittest.main()
