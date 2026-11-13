"""The PDB path an image's CodeView entry records is a GUID-verified search candidate.

A module copied away from its build tree still names its PDB at the build path. That path is
probed like any other candidate, so its probe identity (absent, then present) also invalidates a
cached no-PDB result once the PDB appears there.

Run from the repository root: python tests/run_tests.py test_codeview_pdb_path
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_symbols
from run_tests import HOST_SKIP

GUID = "47E3DD0ED21F9EB64C4C44205044422E"
RECORDED = "C:/build/out/fixture.pdb"
READOBJ = os.linesep.join(
    [
        "  DebugEntry {",
        "    Type: CodeView (0x2)",
        "    PDBInfo {",
        "      PDBSignature: 0x53445352",
        "      PDBGUID: {47E3DD0E-D21F-9EB6-4C4C-44205044422E}",
        "      PDBAge: 1",
        f"      PDBFileName: {RECORDED}",
        "    }",
        "  }",
    ]
)


class RecordedPdbPathTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": ""})
        patch.start()
        self.addCleanup(patch.stop)

    def test_readobj_reports_the_recorded_path(self):
        completed = subprocess.CompletedProcess(["llvm-readobj"], 0, READOBJ, "")
        with mock.patch.object(uprof_symbols, "run", return_value=completed):
            info = uprof_symbols._readobj_info("C:/app/fixture.exe", Path("llvm-readobj.exe"))

        self.assertEqual(info, ("fixture.pdb", GUID, 1, RECORDED))

    def test_the_recorded_path_follows_the_module_directory(self):
        with tempfile.TemporaryDirectory(prefix="uprof-codeview-") as directory:
            recorded = Path(directory) / "build" / "fixture.pdb"
            candidates = uprof_symbols._candidate_pdbs(
                Path("C:/app/fixture.exe"), "fixture.pdb", GUID, 1, recorded_pdb=recorded
            )

        self.assertEqual(candidates[-1], recorded)

    def test_a_relative_recorded_path_is_no_candidate(self):
        candidates = uprof_symbols._candidate_pdbs(
            Path("C:/app/fixture.exe"), "fixture.pdb", GUID, 1, recorded_pdb=Path("fixture.pdb")
        )

        self.assertNotIn(Path("fixture.pdb"), candidates)

    def test_a_matching_pdb_at_the_recorded_path_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix="uprof-codeview-") as directory:
            recorded = Path(directory) / "build" / "fixture.pdb"
            recorded.parent.mkdir()
            recorded.write_bytes(b"pdb")

            def signature(_pdbutil, path):
                return (GUID, "1", None) if Path(path) == recorded else (None, None, "absent")

            with mock.patch.object(uprof_symbols, "_pdb_signature", side_effect=signature):
                search = uprof_symbols._choose_pdb(
                    Path("llvm-pdbutil.exe"),
                    Path(directory) / "app" / "fixture.exe",
                    "fixture.pdb",
                    GUID,
                    1,
                    recorded_pdb=recorded,
                )

        self.assertEqual(search.pdb, recorded)

    @unittest.skipUnless(os.name == "nt", HOST_SKIP + "the PDB search runs on Windows")
    def test_symbolize_module_searches_the_recorded_path(self):
        with tempfile.TemporaryDirectory(prefix="uprof-codeview-") as directory:
            root = Path(directory)
            module = root / "fixture.exe"
            module.write_bytes(b"MZ")
            rvas = root / "rvas.txt"
            rvas.write_text("0x1000" + os.linesep, encoding="utf-8")
            recorded = str(root / "build" / "fixture.pdb")
            patches = (
                mock.patch.object(
                    uprof_symbols, "require_symbolizer", return_value=Path("llvm-symbolizer.exe")
                ),
                mock.patch.object(
                    uprof_symbols,
                    "find_sibling",
                    side_effect=lambda _tool, name: Path(name + ".exe"),
                ),
                mock.patch.object(
                    uprof_symbols, "_readobj_info", return_value=("fixture.pdb", GUID, 1, recorded)
                ),
                mock.patch.object(uprof_symbols, "_choose_pdb", side_effect=RuntimeError("stop")),
            )
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3] as choose,
                self.assertRaises(RuntimeError),
            ):
                uprof_symbols.symbolize_module(
                    str(module), rvas, root / "out", module_name="fixture.exe"
                )

        self.assertEqual(choose.call_args.kwargs["recorded_pdb"], Path(recorded))


if __name__ == "__main__":
    unittest.main()
