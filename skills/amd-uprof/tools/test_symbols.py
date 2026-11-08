"""Public-symbol cache aggregation and PDB discovery guards.

Run from this directory: python -m unittest test_symbols -v
"""

import csv
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_symbols
from uprof_util import UprofError


class PublicSymbolsTest(unittest.TestCase):
    def setUp(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("DuckDB is required to aggregate public symbols")
        self.directory = tempfile.TemporaryDirectory(prefix="uprof-publics-")
        self.addCleanup(self.directory.cleanup)

    def aggregate(self, pairs_text):
        pairs = Path(self.directory.name) / "pairs.csv"
        out = Path(self.directory.name) / "publics.csv"
        pairs.write_text(pairs_text, encoding="utf-8", newline="\n")
        uprof_symbols._aggregate_publics(pairs, out)
        return list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))

    def test_a_single_name_start_keeps_its_name(self):
        rows = self.aggregate("rva,name\n4096,entry\n4112,a\n4112,b\n")

        self.assertEqual(rows[0]["names"], "entry")

    def test_a_shared_start_lists_every_name(self):
        rows = self.aggregate("rva,name\n4112,b\n4112,a\n")

        self.assertEqual(rows[0]["name_count"], "2")
        self.assertEqual(rows[0]["names"], "a | b")


class PdbProbeTest(unittest.TestCase):
    def test_the_readobj_probe_has_a_timeout(self):
        completed = subprocess.CompletedProcess(["llvm-readobj"], 0, "", "")

        with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
            uprof_symbols._readobj_info("fixture.exe", Path("llvm-readobj.exe"))

        self.assertEqual(invoked.call_args.kwargs["timeout"],
                         uprof_symbols.PDB_PROBE_TIMEOUT)

    def test_the_pdbutil_probe_has_a_timeout(self):
        completed = subprocess.CompletedProcess(["llvm-pdbutil"], 0,
                                                "GUID: ABCD\nAge: 1\n", "")

        with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
            uprof_symbols._pdb_signature(Path("llvm-pdbutil.exe"), Path("fixture.pdb"))

        self.assertEqual(invoked.call_args.kwargs["timeout"],
                         uprof_symbols.PDB_PROBE_TIMEOUT)

    def test_a_probe_error_carries_the_pdbutil_exit_and_stderr(self):
        completed = subprocess.CompletedProcess(["llvm-pdbutil"], 1, "", "Access is denied.")

        with mock.patch.object(uprof_symbols, "run", return_value=completed):
            guid, age, error = uprof_symbols._pdb_signature(
                Path("llvm-pdbutil.exe"), Path("fixture.pdb"))

        self.assertIsNone(guid)
        self.assertIsNone(age)
        self.assertIn("llvm-pdbutil exited 1", error)
        self.assertIn("Access is denied", error)

    def test_an_unreadable_symbol_cache_contributes_no_candidates(self):
        exe = Path("C:/app/fixture.exe")
        with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": r"srv*C:\cache*https://example"}):
            with mock.patch.object(Path, "rglob", side_effect=OSError("network path")):
                candidates = uprof_symbols._candidate_pdbs(exe, "fixture.pdb", "ABCD", 1)

        self.assertEqual(candidates, [Path("C:/app/fixture.pdb"),
                                      Path("C:/cache/fixture.pdb/ABCD1/fixture.pdb")])

    def test_a_failing_signature_probe_rejects_only_that_candidate(self):
        with mock.patch.object(uprof_symbols, "_candidate_pdbs",
                               return_value=[Path(__file__)]), \
                mock.patch.object(uprof_symbols, "_pdb_signature",
                                  side_effect=UprofError("timed out after 300s")):
            search = uprof_symbols._choose_pdb(
                Path("llvm-pdbutil.exe"), Path("C:/app/fixture.exe"), "fixture.pdb", "ABCD", 1)

        self.assertIsNone(search.pdb)
        self.assertEqual(search.probes[0].status, "probe_failed")
        self.assertIn("timed out after 300s", search.probes[0].detail)

    def test_a_missing_candidate_is_not_found_not_a_guid_mismatch(self):
        with tempfile.TemporaryDirectory(prefix="uprof-probe-") as directory:
            missing = Path(directory) / "amdvlk64.pdb"
            with mock.patch.object(uprof_symbols, "_candidate_pdbs", return_value=[missing]):
                search = uprof_symbols._choose_pdb(
                    Path("llvm-pdbutil.exe"), Path("C:/app/amdvlk64.dll"), "amdvlk64.pdb",
                    "ABCD", 1)

        self.assertIsNone(search.pdb)
        self.assertEqual(search.probes[0].status, "not_found")
        self.assertEqual(search.probes[0].detail, "not found")

    def test_symbolizer_json_errors_are_collected_as_evidence(self):
        text = json.dumps({"Address": "0x10",
                           "Error": {"Message": "'C:\\x\\amdvlk64.pdb': no such file or "
                                                "directory"},
                           "ModuleName": "C:\\app\\amdvlk64.dll"})

        errors = uprof_symbols._json_errors(text)

        self.assertEqual(errors, [("0x10", "'C:\\x\\amdvlk64.pdb': no such file or directory")])

    def test_an_explicit_pdb_dir_is_searched_before_the_module_dir(self):
        with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": ""}):
            candidates = uprof_symbols._candidate_pdbs(
                Path("C:/app/fixture.exe"), "fixture.pdb", "ABCD", 1, [r"C:\symbols"])

        self.assertEqual(candidates, [Path(r"C:\symbols\fixture.pdb"),
                                      Path(r"C:\symbols\ABCD1\fixture.pdb"),
                                      Path("C:/app/fixture.pdb")])

    def test_debug_file_directories_reach_the_symbolizer(self):
        with tempfile.TemporaryDirectory(prefix="uprof-debug-dirs-") as directory:
            root = Path(directory)
            rvas = root / "rvas.txt"
            rvas.write_text("0x1000\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(["llvm-symbolizer"], 0, "", "")
            with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
                uprof_symbols._run_symbolizer(
                    Path("llvm-symbolizer.exe"), "fixture.exe", None, rvas,
                    root / "out.jsonl", debug_dirs=[str(root / "one"), str(root / "two")])

        command = invoked.call_args.args[0]
        self.assertIn(f"--debug-file-directory={root / 'one'}", command)
        self.assertIn(f"--debug-file-directory={root / 'two'}", command)

    def test_the_evidence_line_names_locations_and_unconsulted_urls(self):
        evidence = uprof_symbols.ModuleEvidence(
            "amdvlk64.dll", "C:/app/amdvlk64.dll", image_guid="ABCD", image_age=1,
            search=uprof_symbols.PdbSearch(
                probes=[uprof_symbols.PdbProbe(Path("C:/app/amdvlk64.pdb"), "not_found",
                                               detail="not found")],
                urls=["https://msdl.microsoft.com/download/symbols"]),
            outcome="no_pdb")
        stream = io.StringIO()

        uprof_symbols._evidence_line(evidence, stream)

        text = stream.getvalue()
        self.assertIn(str(Path("C:/app/amdvlk64.pdb")) + " [not found]", text)
        self.assertIn("https://msdl.microsoft.com/download/symbols", text)
        self.assertIn("not consulted", text)
        self.assertIn("debug info recorded: unknown", text)
        self.assertIn("finding, not a blocker", text)


class PdbManifestTest(unittest.TestCase):
    def symbolize(self, pdb):
        with tempfile.TemporaryDirectory(prefix="uprof-pdb-manifest-") as directory:
            root = Path(directory)
            module = root / "fixture.exe"
            module.write_bytes(b"MZ")
            out = root / "out"
            result = uprof_symbols.SymbolizedModule(
                root / "symbols.parquet", None, pdb,
                uprof_symbols.ModuleEvidence(
                    "fixture.exe", str(module), pdb=str(pdb) if pdb else None,
                    outcome="resolved" if pdb else "no_pdb"))
            with mock.patch.object(uprof_symbols, "require_symbolizer",
                                   return_value=Path("llvm-symbolizer.exe")):
                with mock.patch.object(
                        uprof_symbols, "symbolize_module", return_value=result):
                    with mock.patch.object(
                            uprof_symbols, "_combine_artifacts",
                            return_value=(root / "symbols.parquet", None)):
                        symbols, publics, pdbs, evidence = uprof_symbols.symbolize_modules(
                            [("fixture.exe", str(module), ["0x101B"])], out)
                        content = (Path(pdbs).read_text(encoding="utf-8")
                                   if pdbs else None)
                        return symbols, publics, content, evidence

    def test_the_pdb_used_for_symbolization_is_recorded(self):
        pdb = Path("C:/cache/fixture.pdb")

        symbols, publics, manifest, evidence = self.symbolize(pdb)

        rows = list(csv.DictReader(io.StringIO(manifest)))
        self.assertEqual(rows, [{"module_name": "fixture.exe", "pdb": str(pdb)}])
        self.assertEqual(evidence[0].outcome, "resolved")

    def test_no_pdb_means_no_manifest_and_one_evidence_item(self):
        symbols, publics, manifest, evidence = self.symbolize(None)

        self.assertIsNone(manifest)
        self.assertEqual(evidence[0].outcome, "no_pdb")


class NoPdbEvidenceTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "the PDB search runs on Windows")
    def test_the_no_pdb_branch_keeps_the_symbolizer_error(self):
        with tempfile.TemporaryDirectory(prefix="uprof-no-pdb-") as directory:
            root = Path(directory)
            module = root / "fixture.exe"
            module.write_bytes(b"MZ")
            rvas = root / "rvas.txt"
            rvas.write_text("0x1000\n", encoding="utf-8")
            search = uprof_symbols.PdbSearch(
                probes=[uprof_symbols.PdbProbe(root / "fixture.pdb", "not_found")])
            failed = uprof_symbols.SymbolizerRun(
                1, [("0x1000", "'C:\\app\\fixture.pdb': no such file or directory")], "")
            with mock.patch.object(uprof_symbols, "require_symbolizer",
                                   return_value=Path("llvm-symbolizer.exe")), \
                    mock.patch.object(uprof_symbols, "find_sibling",
                                      side_effect=lambda _tool, name: Path(name + ".exe")), \
                    mock.patch.object(uprof_symbols, "_readobj_info",
                                      return_value=("fixture.pdb", "ABCD", 1)), \
                    mock.patch.object(uprof_symbols, "_choose_pdb",
                                      return_value=search), \
                    mock.patch.object(uprof_symbols, "_run_symbolizer",
                                      return_value=failed) as symbolizer, \
                    mock.patch.object(uprof_symbols, "_write_parquet",
                                      return_value=root / "symbols.parquet"):
                result = uprof_symbols.symbolize_module(
                    str(module), rvas, root / "out", module_name="fixture.exe")

        self.assertIsNone(result.pdb)
        self.assertEqual(result.evidence.outcome, "no_pdb")
        self.assertIs(result.evidence.symbolizer, failed)
        self.assertIsNone(symbolizer.call_args.args[2])


class ExplicitPdbTest(unittest.TestCase):
    def test_an_explicit_pdb_file_is_probed_before_any_search_root(self):
        with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": ""}):
            candidates = uprof_symbols._candidate_pdbs(
                Path("C:/app/fixture.exe"), "fixture.pdb", "ABCD", 1,
                [r"C:\symbols"], r"C:\choice\other.pdb")

        self.assertEqual(candidates[0], Path(r"C:\choice\other.pdb"))
        self.assertEqual(candidates[1], Path(r"C:\symbols\fixture.pdb"))

    def test_fallback_debug_paths_reach_the_symbolizer(self):
        with tempfile.TemporaryDirectory(prefix="uprof-fallback-") as directory:
            root = Path(directory)
            rvas = root / "rvas.txt"
            rvas.write_text("0x1000\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(["llvm-symbolizer"], 0, "", "")
            with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
                uprof_symbols._run_symbolizer(
                    Path("llvm-symbolizer.exe"), "fixture.exe", None, rvas,
                    root / "out.jsonl",
                    fallback_paths=[str(root / "one"), str(root / "two")])

        command = invoked.call_args.args[0]
        self.assertIn(f"--fallback-debug-path={root / 'one'}", command)
        self.assertIn(f"--fallback-debug-path={root / 'two'}", command)

    def test_all_empty_symbol_records_are_a_wrong_build_pdb(self):
        text = "\n".join(json.dumps(
            {"Address": hex(0x1000 + offset),
             "Symbol": [{"FunctionName": "", "Line": 0}]}) for offset in range(2)) + "\n"
        completed = subprocess.CompletedProcess(["llvm-symbolizer"], 0, text, "")
        with tempfile.TemporaryDirectory(prefix="uprof-wrong-build-") as directory:
            root = Path(directory)
            rvas = root / "rvas.txt"
            rvas.write_text("0x1000\n0x1001\n", encoding="utf-8")
            with mock.patch.object(uprof_symbols, "run", return_value=completed):
                result = uprof_symbols._run_symbolizer(
                    Path("llvm-symbolizer.exe"), "fixture.exe",
                    root / "fixture.pdb", rvas, root / "out.jsonl")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.empty_records, 2)
        self.assertIn("fixture.pdb", result.errors[0][1])
        self.assertIn("another build", result.errors[0][1])


class ProfilePathOptionsTest(unittest.TestCase):
    def test_profile_accepts_explicit_symbol_paths(self):
        import uprof

        args = uprof.build_parser().parse_args([
            "profile", "--session", "s", "--by", "line",
            "--pdb-file", "chosen.pdb",
            "--pdb-dir", "one", "--pdb-dir", "two",
            "--debug-file-dir", "debug",
            "--fallback-debug-path", "fallback"])

        self.assertEqual(args.pdb_file, "chosen.pdb")
        self.assertEqual(args.pdb_dir, ["one", "two"])
        self.assertEqual(args.debug_file_dir, ["debug"])
        self.assertEqual(args.fallback_debug_path, ["fallback"])

    def test_profile_has_no_image_search_directory(self):
        import uprof

        with self.assertRaises(SystemExit):
            uprof.build_parser().parse_args(
                ["profile", "--session", "s", "--by", "line", "--image-dir", "images"])


if __name__ == "__main__":
    unittest.main()
