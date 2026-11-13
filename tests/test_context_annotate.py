"""Symbolizer names/lines and IBS hardware labels for instruction context.

Run from the repository root: python tests/run_tests.py test_context_annotate
"""
# Spec: SPEC-88

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_context


SYMBOLS = """\
{"Address":"0x1010","ModuleName":"fixture.exe","Symbol":[{"Column":5,"FileName":"src\\\\main.cpp","FunctionName":"main","Line":12,"StartAddress":"0x140001000","StartFileName":"","StartLine":12}]}
{"Address":"0x1014","ModuleName":"fixture.exe","Symbol":[]}
{"Address":"0x1018","ModuleName":"fixture.exe","Error":{"Message":"no symbol"}}
{"Address":"0x101c","ModuleName":"fixture.exe","Symbol":[{"Column":0,"FileName":"","FunctionName":"fixture_entry","Line":0}]}
"""

IBS_RECORD = {
    "samples": 10,
    "DataSrc": {"0x3": 6, "0": 4},
    "CacheHitSt": {"0": 7, "1": 3},
    "IbsDcMiss": 2,
    "IbsOpMemWidth": {"4": 8, "3": 2},
    "IbsTagToRetCtr_samples": 9,
    "IbsTagToRetCtr_sum": 144,
    "IbsTagToRetCtr_max": 31,
}


class SymbolizerTest(unittest.TestCase):
    def test_frame_zero_supplies_function_file_and_line(self):
        symbols = uprof_context.parse_symbolizer_json(SYMBOLS)

        self.assertEqual(symbols[0x1010], uprof_context.SymbolInfo("main", r"src\main.cpp", 12))

    def test_empty_and_error_records_are_skipped(self):
        symbols = uprof_context.parse_symbolizer_json(SYMBOLS)

        self.assertNotIn(0x1014, symbols)
        self.assertNotIn(0x1018, symbols)

    def test_a_function_without_source_keeps_its_name_only(self):
        symbols = uprof_context.parse_symbolizer_json(SYMBOLS)

        self.assertEqual(symbols[0x101C], uprof_context.SymbolInfo("fixture_entry", None, None))

    def test_relative_rvas_are_passed_once_in_sorted_order(self):
        completed = subprocess.CompletedProcess(["llvm-symbolizer"], 0, SYMBOLS, "")
        with mock.patch.object(uprof_context, "run", return_value=completed) as invoked:
            symbols = uprof_context.symbolize_rvas(
                "fixture.exe", [0x1010, 0x1014, 0x1010], symbolizer="llvm-symbolizer.exe"
            )

        self.assertEqual(symbols[0x1010].function, "main")
        (command,) = invoked.call_args[0]
        self.assertIn("--relative-address", command)
        self.assertIn("--output-style=JSON", command)
        self.assertEqual(invoked.call_args[1]["input_text"], "0x1010\n0x1014\n")

    def test_no_addresses_do_not_invoke_the_symbolizer(self):
        with mock.patch.object(uprof_context, "run") as invoked:
            self.assertEqual(uprof_context.symbolize_rvas("fixture.exe", []), {})

        invoked.assert_not_called()

    def test_symbolizer_failure_is_an_omission(self):
        completed = subprocess.CompletedProcess(["llvm-symbolizer"], 1, "", "boom")
        with mock.patch.object(uprof_context, "run", return_value=completed):
            with self.assertRaises(uprof_context.ContextOmission) as caught:
                uprof_context.symbolize_rvas(
                    "fixture.exe", [0x1010], symbolizer="llvm-symbolizer.exe"
                )

        self.assertEqual(caught.exception.reason, "symbolizer_failed")
        self.assertIn("exited 1", caught.exception.detail)

    def test_the_json_error_is_the_symbolizer_omission_detail(self):
        text = json.dumps(
            {
                "Address": "0x1010",
                "Error": {"Message": "'C:\\x\\fixture.pdb': no such file or directory"},
            }
        )
        completed = subprocess.CompletedProcess(["llvm-symbolizer"], 1, text, "")
        with mock.patch.object(uprof_context, "run", return_value=completed):
            with self.assertRaises(uprof_context.ContextOmission) as caught:
                uprof_context.symbolize_rvas(
                    "fixture.exe", [0x1010], symbolizer="llvm-symbolizer.exe"
                )

        self.assertEqual(caught.exception.reason, "symbolizer_failed")
        self.assertIn("exited 1", caught.exception.detail)
        self.assertIn("no such file or directory", caught.exception.detail)

    def test_debug_file_directories_reach_the_symbolizer(self):
        completed = subprocess.CompletedProcess(["llvm-symbolizer"], 0, SYMBOLS, "")
        with mock.patch.object(uprof_context, "run", return_value=completed) as invoked:
            uprof_context.symbolize_rvas(
                "fixture.exe",
                [0x1010],
                symbolizer="llvm-symbolizer.exe",
                debug_dirs=["C:/debug", "C:/more"],
            )

        (command,) = invoked.call_args[0]
        self.assertIn("--debug-file-directory=C:/debug", command)
        self.assertIn("--debug-file-directory=C:/more", command)

    def test_fallback_debug_paths_reach_the_symbolizer(self):
        completed = subprocess.CompletedProcess(["llvm-symbolizer"], 0, SYMBOLS, "")
        with mock.patch.object(uprof_context, "run", return_value=completed) as invoked:
            uprof_context.symbolize_rvas(
                "fixture.exe",
                [0x1010],
                symbolizer="llvm-symbolizer.exe",
                fallback_paths=["C:/fallback"],
            )

        (command,) = invoked.call_args[0]
        self.assertIn("--fallback-debug-path=C:/fallback", command)

    def test_all_empty_symbol_records_are_a_wrong_build_pdb(self):
        text = (
            "\n".join(
                json.dumps(
                    {"Address": hex(0x1010 + offset), "Symbol": [{"FunctionName": "", "Line": 0}]}
                )
                for offset in range(2)
            )
            + "\n"
        )
        completed = subprocess.CompletedProcess(["llvm-symbolizer"], 0, text, "")
        with mock.patch.object(uprof_context, "run", return_value=completed):
            with self.assertRaises(uprof_context.ContextOmission) as caught:
                uprof_context.symbolize_rvas(
                    "fixture.exe",
                    [0x1010, 0x1011],
                    pdb="C:/cache/fixture.pdb",
                    symbolizer="llvm-symbolizer.exe",
                )

        self.assertEqual(caught.exception.reason, "symbolizer_failed")
        self.assertIn("fixture.pdb", caught.exception.detail)
        self.assertIn("another build", caught.exception.detail)


class IbsTest(unittest.TestCase):
    def test_present_fields_follow_the_whitelist_order(self):
        fields = uprof_context.present_ibs_fields(
            {"IbsTagToRetCtr", "DataSrc", "NotWhitelisted", "IbsDcMiss"}
        )

        self.assertEqual(fields, ("DataSrc", "IbsDcMiss", "IbsTagToRetCtr"))

    def test_a_pmc_capture_has_no_ibs_fields(self):
        self.assertEqual(uprof_context.present_ibs_fields(set()), ())

    def test_absent_fields_are_omitted(self):
        summary = uprof_context.ibs_summary(IBS_RECORD, ("DataSrc", "IbsDcMiss", "IbsTagToRetCtr"))

        self.assertEqual(summary["DataSrc"], {"0": 4, "0x3": 6})
        self.assertEqual(summary["IbsDcMiss"], {"set": 2, "of": 10})
        self.assertEqual(summary["IbsTagToRetCtr"], {"samples": 9, "sum": 144, "max": 31})
        self.assertNotIn("CacheHitSt", summary)
        self.assertNotIn("IbsOpBrnMisp", summary)

    def test_zero_flags_are_kept_as_evidence(self):
        record = {"samples": 4, "IbsDcLockedOp": 0}

        summary = uprof_context.ibs_summary(record, ("IbsDcLockedOp",))

        self.assertEqual(summary["IbsDcLockedOp"], {"set": 0, "of": 4})

    def test_distribution_codes_are_sorted_numerically(self):
        record = {"samples": 3, "DataSrc": {"0x10": 1, "0x3": 1, "0x1": 1}}

        summary = uprof_context.ibs_summary(record, ("DataSrc",))

        self.assertEqual(list(summary["DataSrc"]), ["0x1", "0x3", "0x10"])

    def test_latency_without_values_is_omitted(self):
        record = {"samples": 2, "IbsTagToRetCtr_samples": 0}

        summary = uprof_context.ibs_summary(record, ("IbsTagToRetCtr",))

        self.assertNotIn("IbsTagToRetCtr", summary)

    def test_distribution_uses_histogram(self):
        expressions = " ".join(uprof_context.ibs_expression("DataSrc"))

        self.assertIn('histogram("DataSrc")', expressions)

    def test_flag_counts_only_nonzero_values(self):
        expressions = " ".join(uprof_context.ibs_expression("IbsDcMiss"))

        self.assertIn('TRY_CAST("IbsDcMiss" AS BIGINT) <> 0', expressions)

    def test_tag_to_retire_is_clipped_at_the_16_bit_maximum(self):
        expressions = " ".join(uprof_context.ibs_expression("IbsTagToRetCtr"))

        self.assertIn('LEAST(TRY_CAST("IbsTagToRetCtr" AS BIGINT), 65535)', expressions)

    def test_no_dump_means_no_ibs_rows(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = (None,)

        self.assertEqual(uprof_context.ibs_rows(connection, "fixture.exe", [0x1010]), {})
        connection.execute.assert_called_once()


DUMP = """\
DeltaTick,CPU_Number,TID,PID,Offset,ModuleName,DataSrc
100,0,1,4524,256,mod.dll,0
100,0,2,7132,256,mod.dll,0
100,0,3,7132,256,mod.dll,0
100,0,4,7132,256,mod.dll,0
5000,0,1,4524,256,mod.dll,0
"""


class IbsScopeTest(unittest.TestCase):
    """The per-RVA IBS aggregate must count the same rows as v_scoped."""

    def setUp(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("DuckDB is required for the scope query")
        self.directory = tempfile.TemporaryDirectory(prefix="uprof-ibs-scope-")
        self.addCleanup(self.directory.cleanup)
        self.dump = Path(self.directory.name) / "IbsOpDump.csv"
        self.dump.write_text(DUMP, encoding="utf-8", newline="\n")
        self.connection = duckdb.connect()
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "CREATE OR REPLACE TEMP MACRO module_key(p) AS replace(lower(p), chr(92), '/')"
        )
        self.connection.execute(
            "CREATE OR REPLACE TEMP MACRO image_matches(name, pattern) AS "
            "COALESCE(name, '') ILIKE pattern "
            "OR regexp_replace(COALESCE(name, ''), '^.*[\\\\/]', '') ILIKE pattern"
        )
        self.connection.execute("CREATE TABLE Process(id INTEGER, name VARCHAR)")
        self.connection.execute(
            "INSERT INTO Process VALUES (4524, 'c:\\capture\\a.exe'), (7132, 'c:\\capture\\b.exe')"
        )
        self.connection.execute("SET VARIABLE uprof_ibs_dump = ?", [str(self.dump)])
        self.set_scope(time_start=0, time_end=1000, pid="", image="%")

    def set_scope(self, time_start, time_end, pid, image):
        self.connection.execute(f"SET VARIABLE time_start = {int(time_start)}")
        self.connection.execute(f"SET VARIABLE time_end = {int(time_end)}")
        self.connection.execute("SET VARIABLE uprof_pid = ?", [pid])
        self.connection.execute("SET VARIABLE uprof_image = ?", [image])

    def samples(self):
        return uprof_context.ibs_rows(self.connection, "mod.dll", [0x100])[0x100]["samples"]

    def test_time_filter_selects_the_window(self):
        self.assertEqual(self.samples(), 4)
        self.set_scope(time_start=1000, time_end=6000, pid="", image="%")
        self.assertEqual(self.samples(), 1)

    def test_pid_filter_selects_one_process(self):
        self.set_scope(time_start=0, time_end=6000, pid="4524", image="%")
        self.assertEqual(self.samples(), 2)
        self.set_scope(time_start=0, time_end=6000, pid="7132", image="%")
        self.assertEqual(self.samples(), 3)

    def test_image_filter_matches_the_process_row(self):
        self.set_scope(time_start=0, time_end=6000, pid="", image="a.exe")
        self.assertEqual(self.samples(), 2)

    def test_filters_combine(self):
        self.set_scope(time_start=0, time_end=1000, pid="7132", image="b.exe")
        self.assertEqual(self.samples(), 3)


class ModuleRowTest(unittest.TestCase):
    """Path and checksum must come from the same Module row."""

    def setUp(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest("DuckDB is required for the Module query")
        self.connection = duckdb.connect()
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "CREATE OR REPLACE TEMP MACRO module_key(p) AS replace(lower(p), chr(92), '/')"
        )
        self.connection.execute(
            "CREATE OR REPLACE TEMP MACRO image_matches(name, pattern) AS "
            "COALESCE(name, '') ILIKE pattern "
            "OR regexp_replace(COALESCE(name, ''), '^.*[\\\\/]', '') ILIKE pattern"
        )
        self.connection.execute("CREATE TABLE Module(path VARCHAR, checksum BIGINT, size UBIGINT)")
        self.connection.execute(
            "INSERT INTO Module VALUES ('C:\\app\\fixture.exe', 11, 8192), "
            "('C:/app/fixture.exe', 22, 16384)"
        )

    def test_the_checksum_and_size_belong_to_the_choice_of_path(self):
        row = uprof_context._module_row(self.connection, "c:/app/fixture.exe", True, True)

        self.assertEqual(row, ("C:\\app\\fixture.exe", 11, 8192))

    def test_missing_identity_columns_yield_nulls(self):
        row = uprof_context._module_row(self.connection, "c:/app/fixture.exe", False, False)

        self.assertEqual(row, ("C:\\app\\fixture.exe", None, None))


if __name__ == "__main__":
    unittest.main()
