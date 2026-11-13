"""anonymize_capture keeps the named processes of a capture and nothing that names the host.

A capture recorded on someone's machine names the machine, the user and every running process.
trials/anonymize_capture.py deletes every other process with its rows, rewrites host and user
paths, empties HostInfo, reduces session.uprof to the keys the tools read, and writes a plain
dump for review that it scans for the local machine name, user name and profile path itself.

The input is the trial builder's system-wide session with host details planted in it; the local
machine and user names are patched to the planted ones.

Run from the repository root: python tests/run_tests.py test_anonymize_capture
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import debug_fixtures as F

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "trials"))
import anonymize_capture  # noqa: E402
import build_captures  # noqa: E402

HOST, USER = "BUILD-HOST-7", "alice"
PROFILE = f"C:\\Users\\{USER}\\work"


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class AnonymizeCaptureTest(unittest.TestCase):
    def setUp(self):
        import duckdb

        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-anonymize-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.session = build_captures.build(root / "c", ["systemwide"])["systemwide"]
        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE HostInfo (id INTEGER, hostName VARCHAR)")
            connection.execute("INSERT INTO HostInfo VALUES (1, ?)", [HOST])
            connection.execute("UPDATE Process SET name = ? || '\\' || name", [PROFILE])
            connection.execute(
                "UPDATE Module SET path = ? || '\\fixture_full.exe' WHERE id = 1", [PROFILE]
            )
        finally:
            connection.close()
        meta = json.loads((self.session / "session.uprof").read_text(encoding="utf-8"))
        meta["hpc"] = {"hostName": HOST}
        meta["collectCommand"] = f"AMDuProfCLI.exe collect -a -o {PROFILE}\\out"
        meta["collection"]["schedule"]["hostDurations"] = [{"host": HOST, "start": 1, "end": 2}]
        meta["collection"]["target"]["workingDir"] = PROFILE
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        self.out = root / "out"

    def run_main(self, *args, names=(HOST, USER)):
        stderr = io.StringIO()
        with (
            mock.patch.object(anonymize_capture, "host_names", return_value=list(names)),
            contextlib.redirect_stderr(stderr),
        ):
            code = anonymize_capture.main(
                [
                    str(self.session),
                    "--keep-process",
                    "storage_test.exe",
                    "--out",
                    str(self.out),
                    *args,
                ]
            )
        return code, stderr.getvalue()

    def query(self, sql):
        import duckdb

        connection = duckdb.connect(str(self.out / "cpu.db"), read_only=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_only_the_kept_process_and_its_rows_remain(self):
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            self.query("SELECT id, name FROM Process"),
            [(build_captures.PID, "c:\\capture\\storage_test.exe")],
        )
        self.assertEqual(
            self.query("SELECT DISTINCT processId FROM UnifiedSampleSeries"),
            [(build_captures.PID,)],
        )
        modules = {path for (path,) in self.query("SELECT path FROM Module")}
        self.assertNotIn("node.exe", " ".join(modules))
        self.assertIn("c:\\capture\\fixture_full.exe", modules)
        self.assertEqual(self.query("SELECT count(*) FROM HostInfo"), [(0,)])

    def test_session_meta_keeps_the_keys_the_tools_read_and_no_host_value(self):
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        text = (self.out / "session.uprof").read_text(encoding="utf-8")
        meta = json.loads(text)
        self.assertIn("ibsEvents", meta["collection"]["events"])
        self.assertTrue(meta["collection"]["target"]["systemWide"])
        self.assertEqual(meta["collection"]["schedule"]["start"], 1_000_000_000)
        self.assertNotIn(HOST, text)
        self.assertNotIn(USER, text)
        self.assertNotIn("hpc", meta)

    def test_the_review_dump_lists_every_table_and_the_distinct_strings(self):
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        review = (self.out / "review.txt").read_text(encoding="utf-8")
        for table in ("Process", "Module", "Function", "UnifiedSampleSeries", "HostInfo"):
            self.assertIn(f"== table {table}", review)
        self.assertIn("== distinct strings", review)
        self.assertIn("c:\\capture\\storage_test.exe", review)
        self.assertNotIn(HOST, review)
        self.assertNotIn(PROFILE, review)

    def test_a_host_string_left_in_the_dump_fails_the_run(self):
        # A name that is not a path is not rewritten, so the scan must catch it.
        code, stderr = self.run_main(names=(HOST, "fixture_entry"))
        self.assertNotEqual(code, 0)
        self.assertIn("fixture_entry", stderr)
        self.assertIn("review.txt", stderr)

    def test_a_cpu_db_without_the_expected_schema_is_refused(self):
        import duckdb

        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("ALTER TABLE UnifiedSampleSeries DROP COLUMN moduleId")
        finally:
            connection.close()
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("UnifiedSampleSeries.moduleId", stderr)
        self.assertFalse((self.out / "cpu.db").exists())

    def change_schema(self, *statements):
        import duckdb

        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            for statement in statements:
                connection.execute(statement)
        finally:
            connection.close()

    def test_keys_and_foreign_keys_in_the_capture_do_not_stop_the_filter(self):
        # A schema with a primary key on Process and a foreign key from its threads must not
        # make deleting another process's rows fail.
        self.change_schema(
            "CREATE TABLE keyed (id UINTEGER PRIMARY KEY, name VARCHAR)",
            "INSERT INTO keyed SELECT id, name FROM Process",
            "DROP TABLE Process",
            "ALTER TABLE keyed RENAME TO Process",
            "CREATE TABLE ProcessThread (id UBIGINT, processId UINTEGER REFERENCES Process(id), "
            "threadId UINTEGER, threadName VARCHAR)",
            "INSERT INTO ProcessThread SELECT DISTINCT threadId, processId, threadId, "
            "'C:\\Users\\alice\\t' FROM UnifiedSampleSeries",
        )
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            self.query("SELECT DISTINCT processId FROM ProcessThread"), [(build_captures.PID,)]
        )

    def test_a_duckdb_failure_ends_in_one_cause_line(self):
        self.change_schema(
            "CREATE TABLE Odd (processId VARCHAR)", "INSERT INTO Odd VALUES ('not a number')"
        )
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", stderr)
        lines = [line for line in stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, stderr)
        self.assertIn("Odd", lines[0])
        self.assertIn("DuckDB", lines[0])

    def test_an_unknown_process_is_refused(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = anonymize_capture.main(
                [str(self.session), "--keep-process", "absent.exe", "--out", str(self.out)]
            )
        self.assertEqual(code, 2)
        self.assertIn("absent.exe", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
