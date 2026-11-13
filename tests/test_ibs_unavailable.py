"""IBS unavailable reason diagnostic tests.

Run from repository root: python tests/run_tests.py test_ibs_unavailable
"""

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import uprof_native


class IbsUnavailableReasonTest(unittest.TestCase):
    def test_windows_cim_vbs_running(self):
        with mock.patch("uprof_native.IS_WINDOWS", True), \
             mock.patch("uprof_native._windows_vbs_state", return_value=(True, False)):
            reason = uprof_native.ibs_unavailable_reason()
            self.assertIn("VBS running", reason)
            self.assertIn("VirtualizationBasedSecurityStatus is 2", reason)

    def test_windows_cim_hypervisor_detected(self):
        with mock.patch("uprof_native.IS_WINDOWS", True), \
             mock.patch("uprof_native._windows_vbs_state", return_value=(False, True)):
            reason = uprof_native.ibs_unavailable_reason()
            self.assertIn("hypervisor detected", reason)
            self.assertIn("VirtualizationBasedSecurityStatus is 2", reason)

    def test_windows_cim_vbs_and_hypervisor_inactive(self):
        with mock.patch("uprof_native.IS_WINDOWS", True), \
             mock.patch("uprof_native._windows_vbs_state", return_value=(False, False)):
            reason = uprof_native.ibs_unavailable_reason()
            self.assertIn("no IBS counters are available", reason)
            self.assertIn("reports VirtualizationBasedSecurityStatus 0", reason)

    def test_windows_cim_failure_falls_back_to_systeminfo(self):
        systeminfo_out = (
            "Host Name: TEST\n"
            "Virtualization-based security: Running\n"
        )
        with mock.patch("uprof_native.IS_WINDOWS", True), \
             mock.patch("uprof_native._windows_vbs_state", return_value=None), \
             mock.patch("uprof_native.run", return_value=subprocess.CompletedProcess([], 0, systeminfo_out, "")):
            reason = uprof_native.ibs_unavailable_reason()
            self.assertIn("VBS running", reason)

    def test_linux_kvm_worded_as_possibility(self):
        with mock.patch("uprof_native.IS_WINDOWS", False), \
             mock.patch("builtins.open", side_effect=OSError), \
             mock.patch.object(Path, "glob", return_value=[Path("/sys/module/kvm_amd")]):
            reason = uprof_native.ibs_unavailable_reason()
            self.assertIn("KVM kernel module is loaded", reason)
            self.assertIn("may not be exposed to this host", reason)


if __name__ == "__main__":
    unittest.main()
