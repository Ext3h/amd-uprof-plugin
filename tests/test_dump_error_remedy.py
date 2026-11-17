"""A dump profile cannot read is refused with the skill's own call for the capture's dump kind (#403).

views.sql refuses a dump that is missing, lacks a column or holds no sample row. The remedy is a
fresh dump of the kind the capture records (IbsOpDump.csv or IbsFetchDump.csv), written by the
skill's translate into a new directory, which copies the session first and so leaves the source
unchanged. A raw AMDuProfCLI command would run on the original session.

Run from the repository root: python tests/run_tests.py test_dump_error_remedy
"""

import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
import uprof_db
import uprof_native
import uprof_query
import uprof_util

TOOLS = Path(__file__).resolve().parent.parent / "plugin" / "skills" / "amd-uprof" / "tools"
SESSION = Path(__file__).resolve().parent / "capture"
FRESH_FETCH = (
    r"^ALTERNATIVE: fresh dump: python \S+ translate --session \S+ --out-dir <NEW_TR_DIR> "
    r"--ascii ibsfetch-event-dump; python \S+ profile --session <NEW_TR_DIR> --by process$"
)


class ViewsTextTest(unittest.TestCase):
    def test_the_dump_refusals_carry_the_text_profile_recognizes_and_no_raw_cli(self):
        views = (TOOLS / "views.sql").read_text(encoding="utf-8")
        self.assertNotIn("AMDuProfCLI translate", views)
        self.assertNotIn("ibsop-event-dump", views)
        # Dump not found, a missing column and no sample rows.
        self.assertEqual(views.count(f"error('{uprof_query.DUMP_UNUSABLE}"), 3)


class FreshDumpTest(unittest.TestCase):
    def test_the_fresh_dump_alternative_names_the_capture_dump_kind(self):
        for kind in ("ibsfetch-event-dump", "ibsop-event-dump"):
            with self.subTest(kind=kind):
                error = uprof_native.failure(
                    "cause", ["fresh dump"], session=SESSION, dump_kind=kind
                )
                self.assertIn(f"--ascii {kind};", str(error))

    def test_without_a_known_kind_the_kind_is_a_placeholder(self):
        error = uprof_native.failure("cause", ["fresh dump"], session=SESSION)
        self.assertIn("--ascii <ibsop-event-dump|ibsfetch-event-dump>;", str(error))

    @unittest.skipIf(F.duckdb_skip(), F.duckdb_skip())
    def test_a_dump_that_disagrees_offers_a_fresh_dump_of_its_kind(self):
        class Connection:
            def sql(self, _text):
                import duckdb

                raise duckdb.InvalidInputException(
                    "Invalid Input Error: dump d.csv disagrees with session.uprof: the dump is "
                    "possibly incompatible with this session"
                )

        with self.assertRaises(uprof_util.LinesError) as raised:
            uprof_query._dump_sanity(
                Connection(), True, False, True, SESSION, dump_kind="ibsfetch-event-dump"
            )
        self.assertRegex(str(raised.exception).splitlines()[1], FRESH_FETCH)


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip())
class UnusableDumpTest(unittest.TestCase):
    def test_a_missing_fetch_dump_offers_a_fresh_fetch_dump(self):
        from test_dump_parquet_cache import VARIABLES, _session

        with tempfile.TemporaryDirectory(prefix="uprof-dump-remedy-") as tmp:
            capture = Path(tmp) / "capture"
            capture.mkdir()
            dump = capture / "IbsFetchDump.csv"
            session = _session(capture, dump)
            variables = dict(VARIABLES)
            variables["uprof_session_meta"] = ""
            variables["uprof_ibs_dump"] = str(dump)
            variables["uprof_ibs_parquet"] = ""
            with (
                self.assertRaises(uprof_util.UprofError) as raised,
                uprof_db.open_query(session / "cpu.db", [TOOLS / "views.sql"], variables),
            ):
                pass
            refusal = uprof_query.dump_refusal(raised.exception, session, "ibsfetch-event-dump")
        self.assertIsInstance(refusal, uprof_util.LinesError)
        lines = str(refusal).splitlines()
        self.assertIn("IBS ASCII dump not found", lines[0])
        self.assertNotIn("AMDuProfCLI", lines[0])
        self.assertRegex(lines[1], FRESH_FETCH)

    def test_another_error_is_not_a_dump_refusal(self):
        error = uprof_util.UprofError("cannot open cpu.db")
        self.assertIsNone(uprof_query.dump_refusal(error, SESSION, "ibsop-event-dump"))


if __name__ == "__main__":
    unittest.main()
