"""Public-symbol cache aggregation and PDB discovery guards.

Run from the repository root: python tests/run_tests.py test_symbols
"""

import csv
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import uprof_symbols
from uprof_util import UprofError


class PublicSymbolsTest(unittest.TestCase):
    def setUp(self):
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

    def test_all_null_names_do_not_append_a_truncation_suffix(self):
        for count in (5, 6):
            rows = self.aggregate("rva,name\n" + "4096,\n" * count)

            self.assertEqual(rows[0]["name_count"], str(count))
            self.assertEqual(rows[0]["names"], "")


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

    def test_an_explicit_symbol_directory_is_searched_before_the_module_dir(self):
        with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": ""}):
            candidates = uprof_symbols._candidate_pdbs(
                Path("C:/app/fixture.exe"), "fixture.pdb", "ABCD", 1,
                symbol_dirs=[r"C:\symbols"])

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
    def test_an_explicit_symbol_file_is_probed_before_any_search_root(self):
        with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": ""}):
            candidates = uprof_symbols._candidate_pdbs(
                Path("C:/app/fixture.exe"), "fixture.pdb", "ABCD", 1,
                symbol_dirs=[r"C:\symbols"],
                symbol_files=[Path(r"C:\choice\other.pdb")])

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


class SourcePathProbeTest(unittest.TestCase):
    """--symbol-path and --image-path each accept a file or a directory, probed by the tool."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="uprof-paths-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdb = self.root / "app.pdb"
        self.pdb.write_bytes(b"pdb")
        self.debug = self.root / "app.debug"
        self.debug.write_bytes(b"debug")
        self.symbols = self.root / "symbols"
        self.symbols.mkdir()
        self.image = self.root / "app.exe"
        self.image.write_bytes(b"MZ")
        self.images = self.root / "images"
        self.images.mkdir()
        (self.images / "app.exe").write_bytes(b"MZ")

    def test_a_symbol_file_and_a_directory_are_told_apart(self):
        paths = uprof_symbols.probe_paths([str(self.pdb), str(self.symbols)])

        self.assertEqual(paths.symbol_files, (self.pdb,))
        self.assertEqual(paths.symbol_dirs, (self.symbols,))
        self.assertEqual(paths.image_files, ())
        self.assertEqual(paths.image_dirs, ())

    def test_an_image_file_and_a_directory_are_told_apart(self):
        paths = uprof_symbols.probe_paths(
            image_paths=[str(self.image), str(self.images)])

        self.assertEqual(paths.image_files, (self.image,))
        self.assertEqual(paths.image_dirs, (self.images,))
        self.assertEqual(paths.symbol_files, ())

    def test_a_missing_symbol_path_is_an_error_naming_the_option(self):
        with self.assertRaisesRegex(UprofError, "--symbol-path"):
            uprof_symbols.probe_paths([str(self.root / "missing")])

    def test_a_missing_image_path_is_an_error_naming_the_option(self):
        with self.assertRaisesRegex(UprofError, "--image-path"):
            uprof_symbols.probe_paths(image_paths=[str(self.root / "missing")])

    def test_a_non_pdb_symbol_file_is_a_debug_search_root(self):
        paths = uprof_symbols.probe_paths([str(self.debug)])

        self.assertEqual(paths.symbol_files, (self.debug,))
        self.assertEqual(paths.debug_dirs, (self.root,))


class ImageResolutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="uprof-images-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "app.exe"
        self.image.write_bytes(b"MZ")
        self.images = self.root / "images"
        self.images.mkdir()
        self.found = self.images / "app.exe"
        self.found.write_bytes(b"MZ")

    def test_an_explicit_image_file_wins_over_a_directory(self):
        image, source = uprof_symbols.resolve_image(
            "app.exe", "C:/capture/app.exe", image_files=(self.image,),
            image_dirs=(self.images,))

        self.assertEqual(image, self.image)
        self.assertIn("explicit", source)

    def test_an_image_directory_finds_the_module_basename(self):
        image, source = uprof_symbols.resolve_image(
            "app.exe", "C:/capture/app.exe", image_dirs=(self.images,))

        self.assertEqual(image, self.found)
        self.assertIn(str(self.images), source)

    def test_the_captured_path_is_used_without_an_image_path(self):
        image, source = uprof_symbols.resolve_image("app.exe", str(self.image))

        self.assertEqual(image, self.image)
        self.assertIsNone(source)

    def test_an_image_directory_resolves_a_module_the_capture_cannot_supply(self):
        image, _source = uprof_symbols.resolve_image(
            "app.exe", "C:/capture/app.exe", image_dirs=(self.images,))

        self.assertEqual(image, self.found)

    def test_an_unmatched_image_path_resolves_nothing(self):
        image, source = uprof_symbols.resolve_image(
            "other.exe", "C:/capture/other.exe", image_dirs=(self.images,))

        self.assertIsNone(image)
        self.assertIsNone(source)


class ProfilePathOptionsTest(unittest.TestCase):
    def test_profile_accepts_repeatable_neutral_paths(self):
        import uprof

        args = uprof.build_parser().parse_args([
            "profile", "--session", "s", "--by", "line",
            "--symbol-path", "one", "--symbol-path", "two",
            "--image-path", "three", "--image-path", "four"])

        self.assertEqual(args.symbol_path, ["one", "two"])
        self.assertEqual(args.image_path, ["three", "four"])

    def test_profile_rejects_the_old_names(self):
        import uprof

        for option in ("--pdb-file", "--pdb-dir", "--debug-file-dir",
                       "--fallback-debug-path", "--image-dir"):
            with self.subTest(option=option):
                with self.assertRaises(SystemExit):
                    uprof.build_parser().parse_args(
                        ["profile", "--session", "s", "--by", "line", option, "x"])

    def test_the_process_image_filter_stays_separate_from_image_path(self):
        import uprof

        args = uprof.build_parser().parse_args([
            "profile", "--session", "s", "--by", "line",
            "--image", "%app%", "--image-path", "images"])

        self.assertEqual(args.image, "%app%")
        self.assertEqual(args.image_path, ["images"])

    def test_translate_accepts_repeatable_symbol_paths(self):
        import uprof

        args = uprof.build_parser().parse_args([
            "translate", "--session", "s", "--out-dir", "o",
            "--symbol-path", "one", "--symbol-path", "two"])

        self.assertEqual(args.symbol_path, ["one", "two"])

    def test_translate_rejects_the_pdb_directory_name(self):
        import uprof

        with self.assertRaises(SystemExit):
            uprof.build_parser().parse_args(
                ["translate", "--session", "s", "--out-dir", "o", "--pdb-dir", "x"])


class TranslateSymbolPathTest(unittest.TestCase):
    def test_each_symbol_path_is_forwarded(self):
        import uprof_native

        command = uprof_native.translate_command(
            "uprof", "session", symbol_paths=["one", "two"])

        self.assertEqual(command.count("--symbol-path"), 2)
        self.assertIn("one", command)
        self.assertIn("two", command)


class ImagePathSymbolizationTest(unittest.TestCase):
    def test_the_resolved_image_reaches_the_symbolizer(self):
        with tempfile.TemporaryDirectory(prefix="uprof-image-plumb-") as directory:
            root = Path(directory)
            images = root / "images"
            images.mkdir()
            image = images / "app.exe"
            image.write_bytes(b"MZ")
            evidence = uprof_symbols.ModuleEvidence("app.exe", str(image), outcome="resolved")
            result = uprof_symbols.SymbolizedModule(
                root / "symbols.parquet", None, None, evidence)
            with mock.patch.object(uprof_symbols, "symbolize_module",
                                   return_value=result) as symbolized, \
                    mock.patch.object(uprof_symbols, "require_symbolizer",
                                      return_value=Path("llvm-symbolizer.exe")), \
                    mock.patch.object(uprof_symbols, "_combine_artifacts",
                                      return_value=(root / "symbols.parquet", None)):
                uprof_symbols.symbolize_modules(
                    [("app.exe", "C:/capture/app.exe", ["0x101B"])], root,
                    image_dirs=(images,))
            called_with = symbolized.call_args.args[0]

        self.assertEqual(called_with, str(image))


class PublicsMemoTest(unittest.TestCase):
    def test_the_same_pdb_is_enumerated_once_per_run(self):
        with tempfile.TemporaryDirectory(prefix="uprof-memo-") as tmp:
            root = Path(tmp)
            pdb = root / "x.pdb"
            pdb.write_bytes(b"pdb")
            pdbutil = root / "llvm-pdbutil.exe"
            pdbutil.write_bytes(b"")
            stdout = ("SECTION HEADER #1\n  1000 virtual address\nPublic Symbols\n"
                      "1 | S_PUB32 [size = 8] `foo`\n"
                      "        flags = function, addr = 0001:10\n")
            completed = subprocess.CompletedProcess(["llvm-pdbutil"], 0, stdout, "")
            uprof_symbols._PUBLICS_CACHE.reset()
            self.addCleanup(uprof_symbols._PUBLICS_CACHE.reset)
            with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
                first = uprof_symbols.public_symbols(root / "one.csv", pdb, pdbutil)
                calls = invoked.call_count
                second = uprof_symbols.public_symbols(root / "two.csv", pdb, pdbutil)

            self.assertEqual(first, second)
            self.assertEqual(invoked.call_count, calls)

    def test_a_failed_copy_re_enumerates_instead_of_failing(self):
        with tempfile.TemporaryDirectory(prefix="uprof-memo-copy-") as tmp:
            root = Path(tmp)
            pdb = root / "x.pdb"
            pdb.write_bytes(b"pdb")
            pdbutil = root / "llvm-pdbutil.exe"
            pdbutil.write_bytes(b"")
            stdout = ("SECTION HEADER #1\n  1000 virtual address\nPublic Symbols\n"
                      "1 | S_PUB32 [size = 8] `foo`\n"
                      "        flags = function, addr = 0001:10\n")
            completed = subprocess.CompletedProcess(["llvm-pdbutil"], 0, stdout, "")
            uprof_symbols._PUBLICS_CACHE.reset()
            self.addCleanup(uprof_symbols._PUBLICS_CACHE.reset)
            with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
                first = uprof_symbols.public_symbols(root / "one.csv", pdb, pdbutil)
                calls = invoked.call_count
                with mock.patch.object(uprof_symbols.shutil, "copyfile",
                                       side_effect=OSError("gone")):
                    second = uprof_symbols.public_symbols(root / "two.csv", pdb, pdbutil)

            self.assertEqual(first, second)
            self.assertGreater(invoked.call_count, calls)


class ParallelSymbolizationTest(unittest.TestCase):
    """Modules symbolize concurrently, but stream text and artifacts keep module order."""

    def test_concurrent_modules_keep_output_and_artifact_order(self):
        cpu_count = os.cpu_count() or 1
        if cpu_count < 2:
            self.skipTest("a thread pool needs at least two CPUs to run modules together")
        count = min(4, cpu_count)
        with tempfile.TemporaryDirectory(prefix="uprof-parallel-") as directory:
            root = Path(directory)
            names = [f"module{index}.exe" for index in range(count)]
            modules = []
            for name in names:
                path = root / name
                path.write_bytes(b"MZ")
                modules.append((name, str(path), ["0x1000"]))
            barrier = threading.Barrier(count, timeout=5)
            threads: set[int] = set()

            def symbolize(exe, rva_file, out_dir, stream, module_name=None, **_kwargs):
                threads.add(threading.get_ident())
                barrier.wait()
                print(f"SYMBOLIZED: {module_name}", file=stream)
                evidence = uprof_symbols.ModuleEvidence(module_name, exe, outcome="resolved")
                return uprof_symbols.SymbolizedModule(
                    Path(out_dir) / "symbols.parquet", None, None, evidence)

            stream = io.StringIO()
            with mock.patch.object(uprof_symbols, "require_symbolizer",
                                   return_value=Path("llvm-symbolizer.exe")), \
                    mock.patch.object(uprof_symbols, "symbolize_module",
                                      side_effect=symbolize), \
                    mock.patch.object(uprof_symbols, "_combine_artifacts",
                                      return_value=(root / "symbols.parquet", None)) as combine:
                uprof_symbols.symbolize_modules(modules, root / "out", stream=stream)

        expected: list[str] = []
        for name in names:
            expected.append(f"MODULE: {name}: 1 addresses in {root / name}")
            expected.append(f"SYMBOLIZED: {name}")
        self.assertEqual(stream.getvalue().splitlines(), expected)
        # Reaching the barrier proves every call was in flight at once; one worker cannot.
        self.assertGreater(len(threads), 1)
        parquet_inputs = combine.call_args.args[0]
        self.assertEqual([name for name, _path in parquet_inputs], names)


class SymbolizeTasksErrorReplayTest(unittest.TestCase):
    def test_an_error_stops_replay_before_later_modules(self):
        def worker(index, name, path, rvas, out_path, debug_info, symbol_files,
                   symbol_dirs, image_files, image_dirs):
            if name == "b.exe":
                return uprof_symbols._ModuleSymbolization(
                    name, "MODULE: b.exe\nFAILURE DETAIL\n", error=RuntimeError("boom"))
            return uprof_symbols._ModuleSymbolization(name, f"MODULE: {name}\n")

        modules = [("a.exe", "", ["0x1"]), ("b.exe", "", ["0x1"]), ("c.exe", "", ["0x1"])]
        stream = io.StringIO()
        with mock.patch.object(uprof_symbols, "_symbolize_worker", side_effect=worker):
            with self.assertRaises(RuntimeError):
                uprof_symbols._symbolize_tasks(modules, Path("out"), stream, None,
                                               (), (), (), ())

        output = stream.getvalue()
        self.assertIn("MODULE: a.exe", output)
        self.assertIn("FAILURE DETAIL", output)
        self.assertNotIn("MODULE: c.exe", output)


class PublicsCacheResetTest(unittest.TestCase):
    def setUp(self):
        uprof_symbols._PUBLICS_CACHE.reset()
        self.addCleanup(uprof_symbols._PUBLICS_CACHE.reset)

    def _seed(self):
        cache = uprof_symbols._PUBLICS_CACHE
        scratch = cache.pairs_path()
        cache.remember(Path("x.pdb"), Path("out.csv"), (1, 2))
        return scratch

    def _assert_cleared(self):
        cache = uprof_symbols._PUBLICS_CACHE
        self.assertEqual(cache._results, {})
        self.assertEqual(cache._outputs, {})
        self.assertEqual(cache._counter, 0)
        self.assertIsNone(cache._directory)

    def test_a_successful_symbolization_clears_the_cache(self):
        cache = uprof_symbols._PUBLICS_CACHE
        scratch = self._seed()

        def tasks(*_args, **_kwargs):
            self.assertEqual(cache._results, {})
            cache.pairs_path()
            cache.remember(Path("y.pdb"), Path("out2.csv"), (3, 4))
            return []

        with tempfile.TemporaryDirectory(prefix="uprof-reset-ok-") as tmp, \
                mock.patch.object(uprof_symbols, "require_symbolizer",
                                  return_value=Path("llvm-symbolizer.exe")), \
                mock.patch.object(uprof_symbols, "_symbolize_tasks", side_effect=tasks):
            uprof_symbols.symbolize_modules([], Path(tmp), stream=io.StringIO())

        self._assert_cleared()
        self.assertFalse(scratch.parent.exists())

    def test_a_failed_symbolization_clears_the_cache_and_the_next_run_reenumerates(self):
        cache = uprof_symbols._PUBLICS_CACHE
        scratch = self._seed()

        def tasks(*_args, **_kwargs):
            self.assertEqual(cache._results, {})
            cache.pairs_path()
            cache.remember(Path("y.pdb"), Path("out2.csv"), (3, 4))
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory(prefix="uprof-reset-fail-") as tmp:
            with mock.patch.object(uprof_symbols, "require_symbolizer",
                                   return_value=Path("llvm-symbolizer.exe")), \
                    mock.patch.object(uprof_symbols, "_symbolize_tasks", side_effect=tasks):
                with self.assertRaises(RuntimeError):
                    uprof_symbols.symbolize_modules([], Path(tmp), stream=io.StringIO())
            self._assert_cleared()
            self.assertFalse(scratch.parent.exists())

            root = Path(tmp)
            pdb = root / "x.pdb"
            pdb.write_bytes(b"pdb")
            pdbutil = root / "llvm-pdbutil.exe"
            pdbutil.write_bytes(b"")
            stdout = ("SECTION HEADER #1\n  1000 virtual address\nPublic Symbols\n"
                      "1 | S_PUB32 [size = 8] `foo`\n"
                      "        flags = function, addr = 0001:10\n")
            completed = subprocess.CompletedProcess(["llvm-pdbutil"], 0, stdout, "")
            with mock.patch.object(uprof_symbols, "run", return_value=completed) as invoked:
                uprof_symbols.public_symbols(root / "again.csv", pdb, pdbutil)
            self.assertGreater(invoked.call_count, 0)


class PublicSymbolsInvocationTest(unittest.TestCase):
    SECTIONS = "SECTION HEADER #1\n  1000 virtual address\n"
    PUBLICS = ("Public Symbols\n"
               "================================================================\n"
               "1 | S_PUB32 [size = 8] `foo`\n"
               "        flags = function, addr = 0001:10\n")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="uprof-publics-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.pdb = root / "x.pdb"
        self.pdb.write_bytes(b"pdb")
        self.pdbutil = root / "llvm-pdbutil.exe"
        self.pdbutil.write_bytes(b"")
        self.out = root / "publics.csv"
        self.stream = io.StringIO()
        uprof_symbols._PUBLICS_CACHE.reset()
        self.addCleanup(uprof_symbols._PUBLICS_CACHE.reset)

    def invoke(self, stdout, returncode=0):
        completed = subprocess.CompletedProcess(["llvm-pdbutil"], returncode, stdout, "boom")
        with mock.patch.object(uprof_symbols, "find_sibling", return_value=self.pdbutil), \
                mock.patch.object(uprof_symbols, "run", return_value=completed):
            return uprof_symbols._publics(self.pdb, self.out, Path("llvm-symbolizer.exe"),
                                          self.stream)

    def test_a_failed_invocation_is_unavailable_and_warns(self):
        self.assertIsNone(self.invoke(self.SECTIONS, returncode=1))
        self.assertIn("enumeration failed", self.stream.getvalue())

    def test_an_empty_or_markerless_invocation_is_unavailable(self):
        for stdout in ("", self.SECTIONS):
            self.stream.seek(0)
            self.stream.truncate()
            self.assertIsNone(self.invoke(stdout))
            self.assertIn("enumeration failed", self.stream.getvalue())

    def test_a_publics_only_invocation_has_no_sections(self):
        self.assertIsNone(self.invoke(self.PUBLICS))
        self.assertIn("enumeration failed", self.stream.getvalue())

    def test_a_nonzero_exit_with_a_valid_marker_is_unavailable(self):
        self.assertIsNone(self.invoke(self.SECTIONS + self.PUBLICS, returncode=1))
        self.assertIn("enumeration failed", self.stream.getvalue())

    def test_a_complete_invocation_aggregates(self):
        self.assertIsNotNone(self.invoke(self.SECTIONS + self.PUBLICS))
        self.assertIn("FOLDS: 0 shared starts of 1", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
