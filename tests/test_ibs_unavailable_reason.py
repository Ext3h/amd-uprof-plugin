"""The IBS-unavailable cause is detected from numeric system state, not from localized text.

On Windows, `systeminfo` prints its VBS and hypervisor lines in the display language (and in
the OEM code page), so a German host with VBS running got the generic text. Win32_DeviceGuard
VirtualizationBasedSecurityStatus and Win32_ComputerSystem HypervisorPresent are numbers in
every language. A loaded Linux KVM module does not prove the cause, so it is named as a
possibility.
Run from the repository root: python tests/run_tests.py test_ibs_unavailable_reason
"""
# Spec: SPEC-23

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import uprof_native
from uprof_util import UprofError


def windows_reason(stdout, returncode=0, raises=False):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if raises:
            raise UprofError("powershell timed out after 60s")
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    with mock.patch.object(uprof_native, "IS_WINDOWS", True), \
            mock.patch.object(uprof_native, "_system_directory",
                              return_value=Path("C:/Windows/System32")), \
            mock.patch.object(uprof_native, "run", side_effect=fake_run):
        reason = uprof_native.ibs_unavailable_reason()
    return reason, calls


class WindowsCauseTest(unittest.TestCase):
    def test_running_vbs_is_named_from_the_device_guard_status(self):
        reason, calls = windows_reason("2 1\r\n")
        self.assertIn("VBS is running", reason)
        self.assertIn("VirtualizationBasedSecurityStatus 2", reason)
        self.assertIn("remedy:", reason)
        self.assertNotIn("\n", reason)
        self.assertFalse(any("systeminfo" in " ".join(map(str, call)) for call in calls))

    def test_the_query_reads_both_cim_classes_without_a_profile(self):
        _, calls = windows_reason("2 1")
        command = " ".join(map(str, calls[0]))
        for part in ("powershell", "-NoProfile", "-NonInteractive", "Win32_DeviceGuard",
                     "VirtualizationBasedSecurityStatus", "Win32_ComputerSystem",
                     "HypervisorPresent"):
            self.assertIn(part, command)

    def test_the_remedy_does_not_repeat_the_cycle_fallback(self):
        # The reason is printed on the SAMPLER line after collect already chose cycle sampling.
        for stdout in ("2 1", "0 1", "0 0", ""):
            with self.subTest(stdout=stdout):
                reason, _ = windows_reason(stdout)
                self.assertNotIn("--event cycles", reason)

    def test_vbs_enabled_but_not_running_is_not_named(self):
        reason, _ = windows_reason("1 0")
        self.assertNotIn("VBS is running", reason)
        self.assertIn("no hypervisor or VBS is active", reason)
        reason, _ = windows_reason("1 1")
        self.assertIn("hypervisor is present", reason)

    def test_a_hypervisor_without_vbs_is_named(self):
        reason, _ = windows_reason("0 1")
        self.assertIn("hypervisor is present", reason)
        self.assertNotIn("VBS is running", reason)

    def test_neither_names_no_virtualization_cause(self):
        reason, _ = windows_reason("0 0")
        self.assertNotIn("VBS is running", reason)
        self.assertNotIn("hypervisor is present", reason)
        self.assertIn("no hypervisor or VBS is active", reason)

    def test_an_unreadable_state_says_so(self):
        for case in ({"stdout": "", "returncode": 1}, {"stdout": "", "raises": True},
                     {"stdout": "Zugriff verweigert"}):
            with self.subTest(case=case):
                reason, _ = windows_reason(**case)
                self.assertIn("could not be read", reason)


class LinuxCauseTest(unittest.TestCase):
    def test_a_loaded_kvm_module_is_a_possibility_not_the_cause(self):
        with mock.patch.object(uprof_native, "IS_WINDOWS", False), \
                mock.patch("builtins.open", mock.mock_open(read_data="flags : fpu sse2\n")), \
                mock.patch.object(Path, "glob", return_value=iter([Path("/sys/module/kvm")])):
            reason = uprof_native.ibs_unavailable_reason()
        self.assertIn("KVM", reason)
        self.assertIn("may", reason)
        self.assertNotIn("are not exposed to this host", reason)


if __name__ == "__main__":
    unittest.main()
