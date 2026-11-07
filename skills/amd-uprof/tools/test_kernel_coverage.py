"""Kernel-coverage detection: an OS-mode request must not silently lose kernel samples.

Run from this directory: python -m unittest test_kernel_coverage -v
"""

import json
import tempfile
import unittest
from pathlib import Path

import uprof_query
import uprof_session


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


class KernelImageTest(unittest.TestCase):
    """The predicate is basename-based; a path suffix is not enough."""

    def test_windows_kernel_images_are_recognized(self):
        for path in (
            "c:/windows/system32/ntoskrnl.exe",
            "c:/windows/system32/ntkrnlmp.exe",
            "c:/windows/system32/ntkrnlpa.exe",
            "c:/windows/system32/ntkrpamp.exe",
            "c:/windows/system32/hal.dll",
            "c:/windows/system32/drivers/ntfs.sys",
            "C:\\Windows\\System32\\drivers\\amdkmpfd.sys",
        ):
            with self.subTest(path=path):
                self.assertTrue(uprof_session.is_kernel_image(path))

    def test_linux_kernel_modules_are_recognized(self):
        self.assertTrue(uprof_session.is_kernel_image(
            "/lib/modules/6.8.0/updates/amdgpu.ko"))

    def test_user_mode_images_are_not_kernel_images(self):
        for path in (
            "c:/windows/system32/ntdll.dll",
            "c:/windows/system32/kernelbase.dll",
            "c:/program files/mozilla firefox/marshal.dll",
            "c:/program files/app/hal.dll.bak",
            "c:/windows/system32/drivers.sys/ntfs",
        ):
            with self.subTest(path=path):
                self.assertFalse(uprof_session.is_kernel_image(path))


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
        self.assertTrue(self.derive(pmc_meta(True)).os_requested)

    def test_pmc_os_false_is_recorded(self):
        self.assertFalse(self.derive(pmc_meta(False)).os_requested)

    def test_ibs_op_os_mode_is_recorded(self):
        self.assertTrue(self.derive(ibs_meta(op=True, opOs=True)).os_requested)

    def test_ibs_fetch_os_mode_is_recorded(self):
        self.assertTrue(self.derive(ibs_meta(fetch=True, fetchOs=True)).os_requested)

    def test_ibs_user_only_is_not_os_requested(self):
        self.assertFalse(self.derive(ibs_meta(op=True, opOs=False)).os_requested)


class KernelCoverageWarningTest(unittest.TestCase):
    """The warning exists only for an OS-mode request with no kernel-image sample."""

    def test_os_requested_without_kernel_samples_warns(self):
        warning = uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=True), 0)
        self.assertIsNotNone(warning)
        self.assertIn("kernel-mode", warning)
        self.assertIn("elevat", warning.lower())

    def test_os_requested_with_kernel_samples_does_not_warn(self):
        self.assertIsNone(uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=True), 12))

    def test_user_only_request_without_kernel_samples_does_not_warn(self):
        self.assertIsNone(uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=False), 0))

    def test_user_only_request_with_kernel_samples_does_not_warn(self):
        self.assertIsNone(uprof_session.kernel_coverage_warning(
            uprof_session.Sampler(os_requested=False), 12))


class _Relation:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    """Stands in for the query connection; returns the capture's modules and sample counts."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def sql(self, query):
        self.queries.append(query)
        return _Relation(self.rows)


class KernelCoverageFieldTest(unittest.TestCase):
    """End to end over the v_capture_modules rows the query would return."""

    def fields(self, sampler, rows):
        return uprof_query._kernel_coverage_field(_Connection(rows), sampler)

    def test_os_request_with_only_user_samples_emits_the_field(self):
        fields = self.fields(uprof_session.Sampler(os_requested=True), [
            ("c:/windows/system32/ntdll.dll", 100),
            ("c:/windows/system32/ntoskrnl.exe", 0),
        ])
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0][0], "KERNEL_COVERAGE")
        self.assertIn("elevat", fields[0][1].lower())

    def test_os_request_with_a_kernel_sample_emits_nothing(self):
        self.assertEqual(self.fields(uprof_session.Sampler(os_requested=True), [
            ("c:/windows/system32/ntdll.dll", 100),
            ("c:/windows/system32/ntoskrnl.exe", 7),
        ]), [])

    def test_user_only_request_does_not_query_or_warn(self):
        connection = _Connection([("c:/windows/system32/ntdll.dll", 100)])
        self.assertEqual(
            uprof_query._kernel_coverage_field(
                connection, uprof_session.Sampler(os_requested=False)), [])
        self.assertEqual(connection.queries, [])

    def test_marshal_dll_is_not_mistaken_for_hal_dll(self):
        fields = self.fields(uprof_session.Sampler(os_requested=True), [
            ("c:/program files/mozilla firefox/marshal.dll", 100),
        ])
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0][0], "KERNEL_COVERAGE")

    def test_linux_kernel_module_sample_counts_as_coverage(self):
        self.assertEqual(self.fields(uprof_session.Sampler(os_requested=True), [
            ("/lib/modules/6.8.0/updates/amdgpu.ko", 4),
        ]), [])


if __name__ == "__main__":
    unittest.main()
