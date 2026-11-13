"""A capture's cpu.db is data: SQL objects stored in it never run.

A DuckDB file can carry macros, views and generated columns. A macro named like a builtin (for
example lower()) would shadow the builtin in every later query and could read local files into
the output. The capture is attached read-only and refused when it defines any such object, and
no connection may install or load a DuckDB extension on its own.

Run from the repository root: python tests/run_tests.py test_hostile_capture_db
"""

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import uprof_db
import uprof_native
from uprof_util import UprofError

import debug_fixtures as F

TOOLS = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
SECRET = "LOCAL-FILE-CONTENT-7f3a"


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip())
class HostileCaptureDbTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="uprof-hostile-db-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.secret = self.root / "secret.txt"
        self.secret.write_text(SECRET, encoding="utf-8")
        self.session = self.root / "session"
        self.session.mkdir()
        (self.session / "session.uprof").write_text(
            '{"collection": {"target": {"systemWide": true}}, "translateCommand": ""}',
            encoding="utf-8",
        )
        self.cpu_db = self.session / "cpu.db"

    def capture(self, *statements):
        import duckdb

        connection = duckdb.connect(str(self.cpu_db))
        try:
            connection.execute("CREATE TABLE Module (id INTEGER, path VARCHAR)")
            connection.execute("INSERT INTO Module VALUES (1, 'C:/App/App.exe')")
            connection.execute("CREATE TABLE Function (id INTEGER, name VARCHAR)")
            for statement in statements:
                connection.execute(statement.replace("SECRET_PATH", self.secret.as_posix()))
        finally:
            connection.close()

    def refused(self, *statements):
        self.capture(*statements)
        with self.assertRaises(UprofError) as caught:
            with uprof_db.connect(self.cpu_db) as connection:
                connection.execute("SELECT lower('X')").fetchall()
        message = str(caught.exception)
        self.assertNotIn(SECRET, message)
        self.assertIn(str(self.cpu_db), message)
        return message

    def test_a_macro_shadowing_a_builtin_is_refused(self):
        message = self.refused(
            "CREATE MACRO lower(x) AS (SELECT content FROM read_text('SECRET_PATH'))"
        )
        self.assertIn("lower", message)

    def test_a_view_is_refused(self):
        message = self.refused(
            "CREATE VIEW Process AS SELECT content AS name FROM read_text('SECRET_PATH')"
        )
        self.assertIn("Process", message)

    def test_a_generated_column_is_refused(self):
        message = self.refused(
            "CREATE TABLE Process (id INTEGER, name VARCHAR AS (CAST(id AS VARCHAR) || 'x'))"
        )
        self.assertIn("Process.name", message)

    def test_the_refusal_names_a_runnable_alternative(self):
        message = self.refused("CREATE MACRO lower(x) AS x")
        self.assertEqual(len(message.splitlines()), 2)
        self.assertTrue(message.splitlines()[1].startswith("ALTERNATIVE: "))
        self.assertIn("translate --session", message)

    def test_a_plain_capture_reads_its_tables_with_builtin_functions(self):
        self.capture()
        with uprof_db.connect(self.cpu_db) as connection:
            self.assertEqual(connection.execute("SELECT lower('X')").fetchall(), [("x",)])
            self.assertEqual(
                connection.execute("SELECT path FROM Module").fetchall(), [("C:/App/App.exe",)]
            )

    def test_the_capture_cannot_be_written(self):
        self.capture()
        with uprof_db.connect(self.cpu_db) as connection:
            with self.assertRaises(Exception):
                connection.execute("INSERT INTO Module VALUES (2, 'x')")

    def test_extensions_are_neither_installed_nor_loaded_on_their_own(self):
        self.capture()
        with uprof_db.connect(self.cpu_db) as connection:
            settings = connection.execute(
                "SELECT current_setting('autoinstall_known_extensions'), "
                "current_setting('autoload_known_extensions')"
            ).fetchone()
        self.assertEqual(settings, (False, False))
        with uprof_db.memory_connection() as connection:
            settings = connection.execute(
                "SELECT current_setting('autoinstall_known_extensions'), "
                "current_setting('autoload_known_extensions')"
            ).fetchone()
        self.assertEqual(settings, (False, False))

    def test_translate_counts_do_not_run_capture_sql(self):
        self.capture("CREATE MACRO lower(x) AS (SELECT content FROM read_text('SECRET_PATH'))")
        self.assertIsNone(uprof_native.symbolization_counts(self.cpu_db))

    def test_profile_output_carries_no_local_file_content(self):
        self.capture("CREATE MACRO lower(x) AS (SELECT content FROM read_text('SECRET_PATH'))")
        completed = subprocess.run(
            [sys.executable, str(TOOLS / "uprof.py"), "schema", "--session", str(self.session)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn(SECRET, completed.stdout + completed.stderr)
        self.assertIn("lower", completed.stderr)


class ConnectionSitesTest(unittest.TestCase):
    def test_every_tool_connection_goes_through_uprof_db(self):
        calls = []
        for path in sorted(TOOLS.glob("*.py")):
            if path.name == "uprof_db.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls += [
                f"{path.name}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "duckdb"
            ]
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
