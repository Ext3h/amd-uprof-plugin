"""Symbolizer names/lines and IBS hardware labels for instruction context.

Run from this directory: python -m unittest test_context_annotate -v
"""

import subprocess
import unittest
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
                "fixture.exe", [0x1010, 0x1014, 0x1010],
                symbolizer="llvm-symbolizer.exe")

        self.assertEqual(symbols[0x1010].function, "main")
        command, = invoked.call_args[0]
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
                uprof_context.symbolize_rvas("fixture.exe", [0x1010],
                                             symbolizer="llvm-symbolizer.exe")

        self.assertEqual(caught.exception.reason, "symbolizer_failed")


class IbsTest(unittest.TestCase):
    def test_present_fields_follow_the_whitelist_order(self):
        fields = uprof_context.present_ibs_fields(
            {"IbsTagToRetCtr", "DataSrc", "NotWhitelisted", "IbsDcMiss"})

        self.assertEqual(fields, ("DataSrc", "IbsDcMiss", "IbsTagToRetCtr"))

    def test_a_pmc_capture_has_no_ibs_fields(self):
        self.assertEqual(uprof_context.present_ibs_fields(set()), ())

    def test_absent_fields_are_omitted(self):
        summary = uprof_context.ibs_summary(
            IBS_RECORD, ("DataSrc", "IbsDcMiss", "IbsTagToRetCtr"))

        self.assertEqual(summary["DataSrc"], {"0": 4, "0x3": 6})
        self.assertEqual(summary["IbsDcMiss"], {"set": 2, "of": 10})
        self.assertEqual(summary["IbsTagToRetCtr"],
                         {"samples": 9, "sum": 144, "max": 31})
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

    def test_latency_is_clipped_like_pct_cycles(self):
        expressions = " ".join(uprof_context.ibs_expression("IbsTagToRetCtr"))

        self.assertIn("LEAST(TRY_CAST(\"IbsTagToRetCtr\" AS BIGINT), 131070)", expressions)

    def test_no_dump_means_no_ibs_rows(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = (None,)

        self.assertEqual(uprof_context.ibs_rows(connection, "fixture.exe", [0x1010]), {})
        connection.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
