"""External tools are never started from the current directory.

`collect` runs elevated. On Windows, CreateProcess with a bare name and shutil.which both look in
the current directory before PATH, so a planted powershell.exe, AMDuProfCLI.exe or
llvm-symbolizer.exe in a checked-out project or an extracted capture folder would run with
administrator rights. PowerShell is started by its absolute path below the OS system directory,
and the PATH lookup skips the current directory and relative PATH entries.
Run from the repository root: python tests/run_tests.py test_tool_lookup
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_native

EXE = ".exe" if os.name == "nt" else ""


class LookupTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="uprof-lookup-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cwd = self.root / "project"
        self.bin = self.root / "bin"
        self.cwd.mkdir()
        self.bin.mkdir()
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)

    def plant(self, directory, name):
        path = directory / (name + EXE)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def which(self, name, path_entries):
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(path_entries)}):
            return uprof_native._which(name)

    def test_a_tool_in_the_current_directory_is_not_found(self):
        self.plant(self.cwd, "AMDuProfCLI")
        self.assertIsNone(self.which("AMDuProfCLI", [str(self.bin)]))

    def test_dot_empty_relative_and_cwd_path_entries_are_skipped(self):
        self.plant(self.cwd, "llvm-symbolizer")
        for entries in (["."], [""], [str(self.cwd)]):
            with self.subTest(entries=entries):
                self.assertIsNone(self.which("llvm-symbolizer", entries))
        # A relative entry resolves against the current directory, wherever that is.
        os.chdir(self.root)
        self.assertIsNone(self.which("llvm-symbolizer", ["project"]))

    def test_a_tool_on_an_absolute_path_entry_is_found(self):
        self.plant(self.cwd, "AMDuProfCLI")
        real = self.plant(self.bin, "AMDuProfCLI")
        found = self.which("AMDuProfCLI", [str(self.bin)])
        self.assertIsNotNone(found)
        # resolve() on both sides: a temp root may be spelled with an 8.3 short name.
        self.assertEqual(found.resolve(), real.resolve())


@unittest.skipUnless(os.name == "nt", "host: PowerShell is started only on Windows")
class PowerShellPathTest(unittest.TestCase):
    def test_powershell_starts_by_absolute_path_from_the_system_directory(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "0 0", "")

        with mock.patch.object(uprof_native, "IS_WINDOWS", True), \
                mock.patch.object(uprof_native, "_system_directory",
                                  return_value=Path("C:/Windows/System32")), \
                mock.patch.dict(os.environ, {"SystemRoot": "C:/planted"}), \
                mock.patch.object(uprof_native, "run", side_effect=fake_run):
            uprof_native.ibs_unavailable_reason()
        self.assertEqual(Path(calls[0][0]),
                         Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))

    def test_the_system_directory_comes_from_the_os(self):
        with mock.patch.dict(os.environ, {"SystemRoot": "C:/planted", "windir": "C:/planted"}):
            directory = uprof_native._system_directory()
        self.assertTrue((directory / "kernel32.dll").is_file(), directory)
        self.assertNotIn("planted", str(directory))


if __name__ == "__main__":
    unittest.main()
