"""translate must start from a clean copy instead of appending to an earlier translation.

Run from the repository root: python tests/run_tests.py test_translate_out_dir
"""

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import uprof_native
from uprof_util import UprofError

SAMPLE = "sample\n"


class TranslateOutDirTest(unittest.TestCase):
    """The fake CLI appends to cpu.db when one survives, as AMDuProfCLI does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.session = root / "session"
        self.session.mkdir()
        (self.session / "session.uprof").write_bytes(b"session")
        (self.session / "cpu").mkdir()
        (self.session / "cpu" / "new.prd").write_bytes(b"new")
        self.out = root / "out"
        self.out.mkdir()
        (self.out / "unrelated.txt").write_text("keep me", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def seed_previous_translation(self):
        (self.out / "cpu.db").write_text(SAMPLE, encoding="utf-8")
        (self.out / "callstack.db").write_text(SAMPLE, encoding="utf-8")
        (self.out / "cpu").mkdir()
        (self.out / "cpu" / "old.prd").write_bytes(b"old")

    def fake_run(self, command, **_kwargs):
        with open(self.out / "cpu.db", "a", encoding="utf-8") as handle:
            handle.write(SAMPLE)
        return subprocess.CompletedProcess(command, 0, "", "")

    def translate(self, *, force):
        with mock.patch.object(uprof_native, "require_uprof",
                               return_value=Path("AMDuProfCLI")), \
                mock.patch.object(uprof_native, "run", side_effect=self.fake_run):
            uprof_native.translate(self.session, self.out, force=force)

    def test_existing_cpu_db_is_refused_without_force(self):
        self.seed_previous_translation()

        with self.assertRaisesRegex(UprofError, "--force"):
            self.translate(force=False)

        self.assertEqual((self.out / "cpu.db").read_text(encoding="utf-8"), SAMPLE)

    def test_force_replaces_instead_of_appending(self):
        self.seed_previous_translation()

        for _ in range(2):
            self.translate(force=True)
            self.assertEqual((self.out / "cpu.db").read_text(encoding="utf-8"), SAMPLE)

        self.assertFalse((self.out / "callstack.db").exists())
        self.assertFalse((self.out / "cpu" / "old.prd").exists())
        self.assertTrue((self.out / "cpu" / "new.prd").exists())
        self.assertEqual((self.out / "unrelated.txt").read_text(encoding="utf-8"), "keep me")


class SymbolizationCountsTest(unittest.TestCase):
    def setUp(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("DuckDB is required to inspect cpu.db")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "cpu.db"
        connection = duckdb.connect(str(self.db))
        connection.execute(
            "CREATE TABLE Module(id INTEGER, path VARCHAR, foundDebugInfo BOOLEAN)")
        connection.execute(
            "INSERT INTO Module VALUES (1, 'a', true), (2, 'b', false), (3, 'c', false)")
        connection.execute("CREATE TABLE Function(id INTEGER)")
        connection.execute("INSERT INTO Function VALUES (1), (2), (3), (4)")
        connection.close()

    def test_counts_report_modules_with_debug_info_and_function_rows(self):
        self.assertEqual(uprof_native.symbolization_counts(self.db), (3, 1, 4))

    def test_a_missing_found_debug_info_column_is_unknown(self):
        import duckdb

        connection = duckdb.connect(str(self.db))
        connection.execute("ALTER TABLE Module DROP COLUMN foundDebugInfo")
        connection.close()

        self.assertEqual(uprof_native.symbolization_counts(self.db), (3, None, 4))

    def test_an_unreadable_database_returns_none(self):
        self.db.write_text("not a database", encoding="utf-8")

        self.assertIsNone(uprof_native.symbolization_counts(self.db))


class TranslateSymbolizationWarningTest(unittest.TestCase):
    def test_a_silent_symbol_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            (session / "session.uprof").write_bytes(b"session")
            out = root / "out"

            def fake_run(command, **_kwargs):
                (out / "cpu.db").write_text("x", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "Translation finished\n", "")

            stream = io.StringIO()
            with mock.patch.object(uprof_native, "require_uprof",
                                   return_value=Path("AMDuProfCLI")), \
                    mock.patch.object(uprof_native, "run", side_effect=fake_run), \
                    mock.patch.object(uprof_native, "symbolization_counts",
                                      return_value=(1166, 0, 0)), \
                    redirect_stdout(stream):
                uprof_native.translate(session, out)

        text = stream.getvalue()
        self.assertIn("SYMBOLIZATION: 0/1166 modules with debug info, 0 Function rows", text)
        self.assertIn("WARNING:", text)
        self.assertIn("not symbolization success", text)


if __name__ == "__main__":
    unittest.main()
