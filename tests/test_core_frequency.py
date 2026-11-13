"""Core frequency detection tests for Windows registry and Linux cpufreq / procfs.

Run from repository root: python tests/run_tests.py test_core_frequency
"""

import tempfile
import unittest
from pathlib import Path

import uprof_collect
from uprof_util import UprofError


class CoreFrequencyLinuxTest(unittest.TestCase):
    def test_base_frequency_is_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base_frequency"
            base.write_text("3800000\n", encoding="ascii")
            max_freq = Path(tmp) / "cpuinfo_max_freq"
            max_freq.write_text("4700000\n", encoding="ascii")
            cpuinfo = Path(tmp) / "cpuinfo"
            cpuinfo.write_text("cpu MHz : 2200.000\n", encoding="utf-8")

            hz, display, source = uprof_collect._linux_core_frequency(
                cpufreq_paths=(base, max_freq), cpuinfo_path=cpuinfo)

            self.assertEqual(hz, 3_800_000_000)
            self.assertEqual(display, "3800 MHz")
            self.assertEqual(source, str(base))

    def test_cpuinfo_max_freq_is_used_when_base_frequency_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "nonexistent_base_frequency"
            max_freq = Path(tmp) / "cpuinfo_max_freq"
            max_freq.write_text("4700000\n", encoding="ascii")
            cpuinfo = Path(tmp) / "cpuinfo"
            cpuinfo.write_text("cpu MHz : 2200.000\n", encoding="utf-8")

            hz, display, source = uprof_collect._linux_core_frequency(
                cpufreq_paths=(base, max_freq), cpuinfo_path=cpuinfo)

            self.assertEqual(hz, 4_700_000_000)
            self.assertEqual(display, "4700 MHz")
            self.assertEqual(source, f"{max_freq} (boost maximum)")

    def test_proc_cpuinfo_fallback_labels_current_frequency(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base_frequency"
            max_freq = Path(tmp) / "cpuinfo_max_freq"
            cpuinfo = Path(tmp) / "cpuinfo"
            cpuinfo.write_text(
                "processor\t: 0\n"
                "model name\t: AMD Ryzen 9 7950X\n"
                "cpu MHz\t\t: 2842.125\n",
                encoding="utf-8")

            hz, display, source = uprof_collect._linux_core_frequency(
                cpufreq_paths=(base, max_freq), cpuinfo_path=cpuinfo)

            self.assertEqual(hz, 2_842_125_000)
            self.assertEqual(display, "2842.125 MHz")
            self.assertEqual(source, f"{cpuinfo} cpu MHz (current frequency)")

    def test_all_sources_missing_raises_uprof_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base_frequency"
            max_freq = Path(tmp) / "cpuinfo_max_freq"
            cpuinfo = Path(tmp) / "cpuinfo"
            cpuinfo.write_text("processor : 0\n", encoding="utf-8")

            with self.assertRaises(UprofError) as context:
                uprof_collect._linux_core_frequency(
                    cpufreq_paths=(base, max_freq), cpuinfo_path=cpuinfo)

            self.assertIn("cannot determine the core frequency", str(context.exception))
            self.assertIn("cpuinfo_max_freq", str(context.exception))


class CoreFrequencyWindowsTest(unittest.TestCase):
    def test_windows_frequency_returns_valid_tuple(self):
        if not uprof_collect.IS_WINDOWS:
            self.skipTest("host: Windows-only test")
        hz, display, source = uprof_collect._windows_core_frequency()
        self.assertGreater(hz, 0)
        self.assertTrue(display.endswith(" MHz"))
        self.assertIn("CentralProcessor", source)


if __name__ == "__main__":
    unittest.main()
