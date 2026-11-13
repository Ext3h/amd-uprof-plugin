"""translate must start from a clean copy instead of appending to an earlier translation.

Run from the repository root: python tests/run_tests.py test_translate_out_dir
"""
# Spec: SPEC-25, SPEC-26

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import uprof_native
import uprof_util
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
        with (
            mock.patch.object(uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")),
            mock.patch.object(uprof_native, "run_watched", side_effect=self.fake_run),
        ):
            uprof_native.translate(self.session, self.out, force=force)

    def test_existing_cpu_db_is_refused_without_force(self):
        self.seed_previous_translation()

        with self.assertRaisesRegex(UprofError, "--force"):
            self.translate(force=False)

        self.assertEqual((self.out / "cpu.db").read_text(encoding="utf-8"), SAMPLE)

    def test_leftover_copied_inputs_are_refused_without_force(self):
        """A translate that stopped after the copy leaves inputs a new copy would merge into."""
        for index, name in enumerate(("session.uprof", *uprof_util.COPY_DIRS)):
            with self.subTest(name=name):
                self.out = Path(self.tmp.name) / f"out{index}"
                self.out.mkdir()
                leftover = self.out / name
                if name == "session.uprof":
                    leftover.write_bytes(b"other session")
                else:
                    leftover.mkdir()
                    (leftover / "old.prd").write_bytes(b"old")

                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(UprofError, "--force"):
                    self.translate(force=False)

                self.assertFalse((self.out / "cpu.db").exists(), "translate ran")
                if name == "session.uprof":
                    self.assertEqual(leftover.read_bytes(), b"other session")
                else:
                    self.assertEqual([path.name for path in leftover.iterdir()], ["old.prd"])

    def test_force_replaces_instead_of_appending(self):
        self.seed_previous_translation()

        for _ in range(2):
            self.translate(force=True)
            self.assertEqual((self.out / "cpu.db").read_text(encoding="utf-8"), SAMPLE)

        self.assertFalse((self.out / "callstack.db").exists())
        self.assertFalse((self.out / "cpu" / "old.prd").exists())
        self.assertTrue((self.out / "cpu" / "new.prd").exists())
        self.assertEqual((self.out / "unrelated.txt").read_text(encoding="utf-8"), "keep me")


def _short_path(path):
    """The 8.3 alias of path, or None when the volume keeps no short names."""
    if os.name != "nt":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        return None
    short = Path(buffer.value)
    return None if os.path.normcase(str(short)) == os.path.normcase(str(path)) else short


class AliasedOutDirTest(unittest.TestCase):
    """An --out-dir that reaches the session under another spelling must never touch it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.session = self.root / "capture_session"
        (self.session / "cpu").mkdir(parents=True)
        (self.session / "session.uprof").write_bytes(b"session")
        (self.session / "cpu" / "raw.prd").write_bytes(b"raw")

    def assert_refused_and_intact(self, out, force=True):
        def fake_run(command, **_kwargs):
            self.fail("translate ran AMDuProfCLI on an --out-dir that overlaps --session")

        with (
            mock.patch.object(uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")),
            mock.patch.object(uprof_native, "run_watched", side_effect=fake_run),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(UprofError, "--out-dir"),
        ):
            uprof_native.translate(self.session, out, force=force)
        self.assertEqual((self.session / "session.uprof").read_bytes(), b"session")
        self.assertEqual((self.session / "cpu" / "raw.prd").read_bytes(), b"raw")

    def test_junction_to_the_session_is_refused(self):
        if os.name != "nt":
            self.skipTest("host: junctions exist on Windows only")
        import _winapi

        link = self.root / "junction"
        _winapi.CreateJunction(str(self.session), str(link))
        self.assert_refused_and_intact(link)

    def test_symlink_to_the_session_is_refused(self):
        link = self.root / "symlink"
        try:
            link.symlink_to(self.session, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"host: cannot create a directory symlink here: {exc}")
        self.assert_refused_and_intact(link)

    def test_different_case_is_refused_on_a_case_insensitive_volume(self):
        upper = Path(str(self.session).upper())
        if str(upper) == str(self.session) or not upper.is_dir():
            self.skipTest("host: the volume is case-sensitive")
        self.assert_refused_and_intact(upper)

    def test_short_name_is_refused(self):
        short = _short_path(self.session)
        if short is None:
            self.skipTest("host: the volume keeps no 8.3 names")
        self.assert_refused_and_intact(short)

    def unc(self, path):
        """path spelled through the administrative share of its drive."""
        if os.name != "nt":
            self.skipTest("host: UNC paths exist on Windows only")
        drive, rest = os.path.splitdrive(str(path))
        share = Path(rf"\\localhost\{drive[0]}$" + "\\")
        if not share.is_dir():
            self.skipTest("host: the administrative share is not reachable")
        return Path(rf"\\localhost\{drive[0]}$" + rest)

    def nest_session_in_out(self):
        out = self.root / "out"
        nested = out / "cpu" / "capture_session"
        nested.parent.mkdir(parents=True)
        self.session.rename(nested)
        self.session = nested
        return out

    def test_unc_path_is_refused(self):
        self.assert_refused_and_intact(self.unc(self.session))

    def test_session_inside_a_copied_directory_of_out_dir_is_refused(self):
        self.assert_refused_and_intact(self.nest_session_in_out())

    def test_session_inside_a_unc_out_dir_is_refused(self):
        self.assert_refused_and_intact(self.unc(self.nest_session_in_out()))

    def test_out_dir_inside_the_session_is_refused(self):
        self.assert_refused_and_intact(self.session / "cpu" / "copy", force=False)

    def test_new_out_dir_inside_the_session_through_unc_is_refused(self):
        self.assert_refused_and_intact(self.unc(self.session) / "new" / "deeper", force=False)

    def test_a_sibling_directory_is_accepted(self):
        sibling = self.root / "capture_session_copy"
        sibling.mkdir()
        self.assertFalse(uprof_util.contains(sibling, self.session))
        self.assertFalse(uprof_util.contains(self.session, sibling))


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
        connection.execute("CREATE TABLE Module(id INTEGER, path VARCHAR, foundDebugInfo BOOLEAN)")
        connection.execute(
            "INSERT INTO Module VALUES (1, 'a', true), (2, 'b', false), (3, 'c', false)"
        )
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
            with (
                mock.patch.object(uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")),
                mock.patch.object(uprof_native, "run_watched", side_effect=fake_run),
                mock.patch.object(uprof_native, "symbolization_counts", return_value=(1166, 0, 0)),
                redirect_stdout(stream),
            ):
                uprof_native.translate(session, out)

        text = stream.getvalue()
        self.assertIn("SYMBOLIZATION: 0/1166 modules with debug info, 0 Function rows", text)
        self.assertIn("WARNING:", text)
        self.assertIn("not symbolization success", text)


if __name__ == "__main__":
    unittest.main()
