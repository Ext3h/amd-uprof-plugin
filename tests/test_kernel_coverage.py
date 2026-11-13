"""Kernel-coverage detection: an OS-mode request must not silently lose kernel samples.

Run from the repository root: python tests/run_tests.py test_kernel_coverage
"""
# Spec: SPEC-19, SPEC-20, SPEC-21, SPEC-22

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

import uprof_collect
import uprof_db
import uprof_dump
import uprof_query
import uprof_session

KERNEL_VA = 0xFFFFF800_00000000
USER_VA = 0x00007FF8_00000000


def pmc_meta(os_mode):
    encoded = 0x03000076 if os_mode else 0x01000076
    return {
        "collection": {
            "events": {"pmcEvents": {"pmcEventList": [{
                "hwId": 118, "interval": 4_700_000, "isSampling": True,
                "user": True, "os": os_mode, "encodedEventId": encoded,
            }]}},
            "target": {"systemWide": True},
        },
        "auxiliary": {},
    }


def ibs_meta(**fields):
    events = {"op": False, "fetch": False, "opInterval": 1_000_000, "fetchInterval": 500_000}
    events.update(fields)
    return {
        "collection": {"events": {"ibsEvents": events}, "target": {"systemWide": True}},
        "auxiliary": {"ibsOpSampling": bool(events.get("op"))},
    }


EVENT_COLUMN = "event#0x100000000f100"


def write_capture(root, *, dump_modules=None, modules=(), instances=None, series=(),
                  translate_command=None):
    """Write a minimal capture; return (cpu.db, session.uprof, IBS dump or None).

    modules are (id, path), instances are (id, processId, moduleId, loadAddress) and series are
    (moduleId, event_count) rows of UnifiedSampleSeries. An instances=None leaves the export
    without ModuleInstance.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    cpu_db = root / "cpu.db"
    connection = duckdb.connect(str(cpu_db))
    try:
        connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
        connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)")
        connection.execute(
            "CREATE TABLE UnifiedSampleSeries (processId UINTEGER, threadId UINTEGER, "
            "moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT, coreId UINTEGER, "
            f'"{EVENT_COLUMN}" DOUBLE)')
        if modules:
            connection.executemany("INSERT INTO Module VALUES (?, ?)", list(modules))
        if instances is not None:
            connection.execute("CREATE TABLE ModuleInstance (id UINTEGER, processId UINTEGER, "
                               "moduleId UINTEGER, loadAddress UBIGINT)")
            if instances:
                connection.executemany("INSERT INTO ModuleInstance VALUES (?, ?, ?, ?)",
                                       list(instances))
        if series:
            connection.executemany(
                "INSERT INTO UnifiedSampleSeries (processId, threadId, moduleId, functionId, "
                f'moduleRVA, coreId, "{EVENT_COLUMN}") VALUES (1, 1, ?, NULL, 0, 0, ?)',
                list(series))
    finally:
        connection.close()

    meta = {"collection": {"events": {}, "target": {"systemWide": True}}, "auxiliary": {}}
    if translate_command is not None:
        meta["translateCommand"] = translate_command
    meta_path = root / "session.uprof"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    dump = None
    if dump_modules is not None:
        dump = root / "IbsOpDump.csv"
        rows = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"]
        rows.extend(f"{index * 1000},0,{index},1,16,{module}"
                    for index, module in enumerate(dump_modules, start=1))
        dump.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return cpu_db, meta_path, dump


@contextlib.contextmanager
def capture_connection(capture, *, event_column=""):
    """Open a capture's cpu.db with views.sql bound, as the profile command does."""
    cpu_db, meta_path, dump = capture
    variables = {
        "uprof_image": "%", "uprof_pid": "", "uprof_top": "20",
        "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
        "uprof_ibs_dump": str(dump) if dump is not None else "",
        "uprof_event_column": event_column,
        "uprof_session_meta": str(meta_path),
        "uprof_tag_available": "false",
        "uprof_symbols": "",
        "uprof_aliases": "",
    }
    with uprof_db.open_query(cpu_db, [uprof_query.TOOLS_DIR / "views.sql"],
                             variables) as connection:
        yield connection


def run_capture_scan(capture, *, event_column=""):
    """Run the whole-capture scan against a capture through the real views.

    Returns (samples, contexts, kernel_covered), the same tuple the ACHIEVED check consumes.
    """
    with capture_connection(capture, event_column=event_column) as connection:
        return uprof_query._capture_scan(connection, True)


def capture_rows(capture, sql, *, event_column=""):
    """The columns and rows of one query over a capture's views."""
    with capture_connection(capture, event_column=event_column) as connection:
        relation = connection.sql(sql)
        return relation.columns, relation.fetchall()


class KernelImageTest(unittest.TestCase):
    """The kernel predicate runs in DuckDB over the module catalog and the dump basenames."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def capture(self, **fields):
        return write_capture(tempfile.mkdtemp(dir=self.root), **fields)

    def assert_covered(self, capture, **options):
        self.assertTrue(run_capture_scan(capture, **options)[2])

    def assert_warned(self, capture, **options):
        self.assertFalse(run_capture_scan(capture, **options)[2])

    def test_scan_counts_samples_and_contexts(self):
        samples, contexts, _ = run_capture_scan(self.capture(
            dump_modules=["a.dll", "a.dll", "b.dll"]))
        self.assertEqual(samples, 3)
        self.assertEqual(contexts, 1)

    def test_kernel_dll_is_recognized_by_its_load_address(self):
        self.assert_covered(self.capture(
            modules=[(1, "c:/windows/system32/ci.dll"), (2, "c:/windows/system32/ntdll.dll")],
            instances=[(1, 4, 1, KERNEL_VA), (2, 4, 2, USER_VA)],
            dump_modules=["c:\\windows\\system32\\ci.dll",
                          "c:\\windows\\system32\\ntdll.dll"]))

    def test_load_address_matches_across_path_case_and_separators(self):
        self.assert_covered(self.capture(
            modules=[(1, "C:\\Windows\\System32\\CI.DLL")],
            instances=[(1, 4, 1, KERNEL_VA)],
            dump_modules=["c:/windows/system32/ci.dll"]))

    def test_user_mode_module_with_a_low_load_address_warns(self):
        self.assert_warned(self.capture(
            modules=[(1, "c:/windows/system32/ntdll.dll")],
            instances=[(1, 4, 1, USER_VA)],
            dump_modules=["c:\\windows\\system32\\ntdll.dll"]))

    def test_windows_kernel_executable_is_recognized_without_a_catalog(self):
        self.assert_covered(self.capture(
            dump_modules=["c:\\windows\\system32\\ntoskrnl.exe"]))

    def test_kernel_driver_suffix_is_recognized_without_a_catalog(self):
        self.assert_covered(self.capture(
            dump_modules=["c:\\windows\\system32\\drivers\\ntfs.sys"]))
        self.assert_covered(self.capture(
            dump_modules=["/lib/modules/6.8.0/updates/amdgpu.ko"]))

    def test_linux_core_images_are_recognized_without_a_catalog(self):
        for image in ("vmlinux", "vmlinuz", "[kernel.kallsyms]"):
            with self.subTest(image=image):
                self.assert_covered(self.capture(
                    dump_modules=[image, "c:\\windows\\system32\\ntdll.dll"]))

    def test_marshal_dll_is_not_mistaken_for_hal_dll(self):
        self.assert_warned(self.capture(
            dump_modules=["c:/program files/mozilla firefox/marshal.dll"]))

    def test_pmc_kernel_module_counts_as_coverage(self):
        self.assert_covered(self.capture(
            modules=[(1, "c:/windows/system32/ci.dll")],
            instances=[(1, 4, 1, KERNEL_VA)],
            series=[(1, 5)]), event_column=EVENT_COLUMN)

    def test_pmc_user_module_warns(self):
        self.assert_warned(self.capture(
            modules=[(1, "c:/windows/system32/ntdll.dll")],
            instances=[(1, 4, 1, USER_VA)],
            series=[(1, 5)]), event_column=EVENT_COLUMN)


class OsRequestParsingTest(unittest.TestCase):
    """os_requested comes from the applied event, not from the request text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def derive(self, meta):
        (self.session / "session.uprof").write_text(
            json.dumps(meta), encoding="utf-8")
        return uprof_session.derive_sampler(self.session)

    def test_pmc_os_true_is_recorded(self):
        sampler = self.derive(pmc_meta(True))
        self.assertTrue(sampler.os_requested)
        self.assertEqual(sampler.os_mode, "os")

    def test_pmc_os_false_is_recorded(self):
        sampler = self.derive(pmc_meta(False))
        self.assertFalse(sampler.os_requested)
        self.assertIsNone(sampler.os_mode)

    def test_ibs_op_os_mode_is_recorded(self):
        sampler = self.derive(ibs_meta(op=True, opOs=True))
        self.assertTrue(sampler.os_requested)
        self.assertEqual(sampler.os_mode, "opOs")

    def test_ibs_fetch_os_mode_is_recorded(self):
        sampler = self.derive(ibs_meta(fetch=True, fetchOs=True))
        self.assertTrue(sampler.os_requested)
        self.assertEqual(sampler.os_mode, "fetchOs")

    def test_ibs_user_only_is_not_os_requested(self):
        sampler = self.derive(ibs_meta(op=True, opOs=False))
        self.assertFalse(sampler.os_requested)
        self.assertIsNone(sampler.os_mode)

    def test_ibs_op_and_fetch_take_the_os_bit_from_either_sampler(self):
        sampler = self.derive(ibs_meta(op=True, fetch=True, opOs=False, fetchOs=True))
        self.assertTrue(sampler.os_requested)
        self.assertEqual(sampler.os_mode, "fetchOs")

    def test_ibs_op_and_fetch_name_both_os_bits(self):
        sampler = self.derive(ibs_meta(op=True, fetch=True, opOs=True, fetchOs=True))
        self.assertEqual(sampler.os_mode, "opOs, fetchOs")

    def test_os_bit_of_a_disabled_sampler_is_ignored(self):
        sampler = self.derive(ibs_meta(op=True, fetch=False, opOs=False, fetchOs=True))
        self.assertFalse(sampler.os_requested)
        self.assertIsNone(sampler.os_mode)


class CaptureModulesViewTest(unittest.TestCase):
    """The view's columns and row shape stay stable for both capture kinds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def capture(self, **fields):
        return write_capture(tempfile.mkdtemp(dir=self.root), **fields)

    def test_dump_capture_yields_one_row_per_sample(self):
        columns, rows = capture_rows(
            self.capture(dump_modules=["c:/app/a.dll", "c:/app/b.dll"]),
            "SELECT * FROM v_capture_modules")
        self.assertEqual(columns, ["module_name", "context", "samples", "kernel_image"])
        self.assertEqual(len(rows), 2)
        self.assertEqual([row[2] for row in rows], [1, 1])

    def test_pmc_capture_yields_the_event_count_per_row(self):
        columns, rows = capture_rows(
            self.capture(modules=[(1, "c:/app/a.dll")], series=[(1, 5)]),
            "SELECT * FROM v_capture_modules", event_column=EVENT_COLUMN)
        self.assertEqual(columns, ["module_name", "context", "samples", "kernel_image"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 5)


class KernelCoverageWarningTest(unittest.TestCase):
    """The warning exists only for an OS-mode request with no kernel-image sample."""

    def test_os_requested_without_kernel_samples_warns(self):
        warning = uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=True, os_mode="os"), False, 100)
        self.assertIsNotNone(warning)
        self.assertIn("kernel-mode", warning)
        self.assertIn("(os)", warning)
        self.assertNotIn("os=1", warning)
        self.assertIn("elevat", warning.lower())

    def test_warning_names_the_sampler_bit(self):
        warning = uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=True, os_mode="opOs"), False, 100)
        self.assertIn("(opOs)", warning)

    def test_empty_capture_is_reported_without_an_elevation_remedy(self):
        warning = uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=True, os_mode="fetchOs"), False, 0)
        self.assertIn("no samples in the capture", warning)
        self.assertNotIn("elevat", warning.lower())

    def test_unknown_capture_total_keeps_the_missing_kernel_wording(self):
        warning = uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=True, os_mode="os"), False)
        self.assertIn("no kernel-image samples", warning)

    def test_os_requested_with_kernel_samples_does_not_warn(self):
        self.assertIsNone(uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=True), True))

    def test_user_only_request_without_kernel_samples_does_not_warn(self):
        self.assertIsNone(uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=False), False))

    def test_user_only_request_with_kernel_samples_does_not_warn(self):
        self.assertIsNone(uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=False), True))


class KernelFieldGuardTest(unittest.TestCase):
    """The field is emitted only when the scan determined that no kernel sample exists."""

    def test_user_only_request_emits_nothing(self):
        self.assertEqual(uprof_query._kernel_coverage_field(
            uprof_session.Sampler(os_requested=False), False), [])

    def test_windowed_dump_emits_nothing(self):
        self.assertEqual(uprof_query._kernel_coverage_field(
            uprof_session.Sampler(os_requested=True), False, windowed=True), [])

    def test_undetermined_coverage_emits_nothing(self):
        self.assertEqual(uprof_query._kernel_coverage_field(
            uprof_session.Sampler(os_requested=True), None), [])

    def test_os_request_without_kernel_samples_emits_the_field(self):
        fields = uprof_query._kernel_coverage_field(
            uprof_session.Sampler(os_requested=True, os_mode="opOs"), False, False, 100)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0][0], "KERNEL_COVERAGE")
        self.assertIn("(opOs)", fields[0][1])

    def test_os_request_with_kernel_samples_emits_nothing(self):
        self.assertEqual(uprof_query._kernel_coverage_field(
            uprof_session.Sampler(os_requested=True), True), [])


class CollectOsModeTest(unittest.TestCase):
    """collect warns about kernel coverage for every event it can request."""

    def test_every_collect_event_requests_os_mode(self):
        for event in ("cycles", "ibs-op"):
            with self.subTest(event=event):
                self.assertTrue(uprof_collect._requests_os_mode(event))

    def test_unelevated_warning_names_kernel_samples_only_for_an_os_mode_request(self):
        self.assertIn("kernel-mode", uprof_collect._unelevated_warning(True))
        self.assertNotIn("kernel-mode", uprof_collect._unelevated_warning(False))

    def run_collect_after_ibs_fallback(self, elevated):
        """Run collect on an unelevated IBS request that falls back to a cycle capture."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "out"
        calls = []

        def fake_run_cli(_uprof, spec, _target_args, options):
            calls.append(spec)
            if len(calls) == 1:
                return subprocess.CompletedProcess(spec, 1, "", "ibs counters are not available")
            session = Path(options.out_dir)
            session.mkdir(parents=True, exist_ok=True)
            (session / "session.uprof").write_text(json.dumps({
                "collection": {
                    "events": {"pmcEvents": {"pmcEventList": [{
                        "hwId": 118, "interval": 4_700_000, "isSampling": True}]}},
                    "target": {"systemWide": True}},
                "auxiliary": {},
            }), encoding="utf-8")
            return subprocess.CompletedProcess(spec, 0, "", "")

        options = uprof_collect.CollectOptions(
            out_dir=str(out), seconds=1, event="ibs-op")
        stderr = io.StringIO()
        with mock.patch.object(uprof_collect, "is_admin", return_value=elevated), \
                mock.patch.object(uprof_collect, "find_uprof", return_value="AMDuProfCLI"), \
                mock.patch.object(uprof_collect, "require_uprof", return_value="AMDuProfCLI"), \
                mock.patch.object(uprof_collect, "ibs_unavailable_reason", return_value="test"), \
                mock.patch.object(uprof_collect, "_run_cli", side_effect=fake_run_cli), \
                mock.patch.object(uprof_collect, "_core_frequency",
                                  return_value=(4_700_000_000, "4700 MHz", "test")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(stderr):
            uprof_collect.collect(options)
        return stderr.getvalue()

    def test_unelevated_ibs_capture_warns_before_the_fallback(self):
        stderr = self.run_collect_after_ibs_fallback(elevated=False)
        self.assertIn(uprof_collect._KERNEL_COVERAGE_NOTE, stderr)
        self.assertIn(uprof_collect._FALLBACK_KERNEL_WARNING, stderr)

    def test_elevated_ibs_capture_does_not_warn(self):
        stderr = self.run_collect_after_ibs_fallback(elevated=True)
        self.assertNotIn(uprof_collect._KERNEL_COVERAGE_NOTE, stderr)
        self.assertNotIn(uprof_collect._FALLBACK_KERNEL_WARNING, stderr)


class WindowedDumpTest(unittest.TestCase):
    """A dump resolved through a recorded translate window cannot testify about the capture."""

    def session(self, translate_command):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = Path(tmp.name)
        (session / "session.uprof").write_text(
            json.dumps({"translateCommand": translate_command}), encoding="utf-8")
        return session

    def test_recorded_translate_filter_is_reported(self):
        session = self.session(
            "AMDuProfCLI.exe translate -i sess --ascii ibsop-event-dump --time-filter 10:12")
        self.assertEqual(uprof_dump.recorded_time_filter(session), "10:12")

    def test_no_recorded_translate_filter_is_none(self):
        self.assertIsNone(uprof_dump.recorded_time_filter(
            self.session("AMDuProfCLI.exe translate -i sess --ascii ibsop-event-dump")))


if __name__ == "__main__":
    unittest.main()
