"""Debug-format fixtures through the production symbolization paths and the LLVM tools.

The checked-in binaries and their builds are in fixtures/debug/README.md. Tests that need the
Windows-only PDB discovery are marked individually; everything else runs on both operating
systems, because LLVM reads PE, PDB, ELF and DWARF on either.
"""

import csv
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
import uprof_context
import uprof_functions
import uprof_symbols

NT_ONLY = "production PDB discovery runs only on Windows (os.name == 'nt')"


def frames(records):
    return {int(record["Address"], 16): F.innermost(record) for record in records}


def readobj(*args):
    completed = F.run(F.READOBJ, *args)
    if completed.returncode != 0:
        raise AssertionError(f"llvm-readobj exited {completed.returncode}: {completed.stderr}")
    return completed.stdout


def pdbutil(*args):
    completed = F.run(F.PDBUTIL, *args)
    if completed.returncode != 0:
        raise AssertionError(f"llvm-pdbutil exited {completed.returncode}: {completed.stderr}")
    return completed.stdout


class FixtureTestCase(unittest.TestCase):
    def assert_names_line(self, frame, function, line, source="fixture_debug.c"):
        self.assertEqual(frame.get("FunctionName"), function)
        self.assertEqual(Path(frame.get("FileName") or "").name, source)
        self.assertEqual(frame.get("Line"), line)

    def assert_names_no_line(self, frame, function):
        self.assertEqual(frame.get("FunctionName"), function)
        self.assertFalse(frame.get("FileName"))
        self.assertEqual(frame.get("Line"), 0)

    def assert_dwarf_lines(self, records):
        resolved = frames(records)
        self.assert_names_line(resolved[F.ELF_HELPER], "fixture_helper", 2)
        self.assert_names_line(resolved[F.ELF_ENTRY], "fixture_entry", 6)
        self.assert_names_line(resolved[F.ELF_ENTRY_BODY], "fixture_entry", 7)

    def temporary_copy(self, image, name=None):
        root = tempfile.mkdtemp(prefix="uprof-debug-copy-")
        self.addCleanup(shutil.rmtree, root, True)
        target = Path(root) / (name or Path(image).name)
        shutil.copyfile(image, target)
        return target


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class PeCoffSymbolTableTest(FixtureTestCase):
    """Row 2: a PE with a COFF symbol table and no PDB."""

    def test_readobj_reports_the_symbol_table_and_both_function_names(self):
        output = readobj("--file-headers", "--sections", "--symbols",
                         "--coff-debug-directory", F.PE / "fixture_coff.exe")
        self.assertRegex(output, r"PointerToSymbolTable: 0x[1-9A-Fa-f]")
        self.assertIn("fixture_helper", output)
        self.assertIn("fixture_entry", output)
        self.assertNotIn(".debug_info", output)
        self.assertNotIn("PDBGUID", output)

    def test_image_debug_directory_records_no_pdb(self):
        name, guid, age = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_coff.exe"), F.READOBJ)
        self.assertEqual(name, "fixture_coff.pdb")
        self.assertIsNone(guid)
        self.assertIsNone(age)

    def test_symbolizer_names_both_starts_without_lines(self):
        resolved = frames(F.symbolizer_records(F.PE / "fixture_coff.exe",
                                               [F.PE_HELPER, F.PE_ENTRY]))
        self.assert_names_no_line(resolved[F.PE_HELPER], "fixture_helper")
        self.assert_names_no_line(resolved[F.PE_ENTRY], "fixture_entry")

    @unittest.skipUnless(os.name == "nt", NT_ONLY)
    def test_production_keeps_the_coff_names_and_reports_no_pdb(self):
        module, records, stream = F.production(F.PE / "fixture_coff.exe",
                                               [F.PE_HELPER, F.PE_ENTRY])
        self.assertIsNone(module.pdb)
        self.assertIn("PDB=none", stream)
        self.assertEqual(module.evidence.outcome, "readobj_failed")
        resolved = frames(records)
        self.assert_names_no_line(resolved[F.PE_HELPER], "fixture_helper")
        self.assert_names_no_line(resolved[F.PE_ENTRY], "fixture_entry")

    @unittest.skipUnless(os.name == "nt", NT_ONLY)
    def test_production_reports_no_debug_info_when_the_database_records_none(self):
        module, _, _ = F.production(F.PE / "fixture_coff.exe", [F.PE_HELPER],
                                    found_debug_info=False)
        self.assertEqual(module.evidence.outcome, "no_debug_info")


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class PeFullPdbTest(FixtureTestCase):
    """Row 3: PE CodeView with a full PDB (lines, publics and types)."""

    def test_image_debug_directory_names_the_pdb_guid_and_age(self):
        name, guid, age = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_full.exe"), F.READOBJ)
        self.assertEqual(F.embedded_basename(name or ""), "fixture_full.pdb")
        self.assertIsNotNone(guid)
        self.assertIsNotNone(age)

    def test_pdb_signature_matches_and_the_probe_accepts_it(self):
        _, image_guid, image_age = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_full.exe"), F.READOBJ)
        guid, age, error = uprof_symbols._pdb_signature(
            F.PDBUTIL, F.PE / "fixture_full.pdb")
        self.assertEqual((guid, age, error), (image_guid, str(image_age), None))
        probe = uprof_symbols._probe_pdb(F.PDBUTIL, F.PE / "fixture_full.pdb",
                                         image_guid, image_age)
        self.assertEqual(probe.status, "accepted")

    def test_probe_rejects_a_matching_guid_with_another_age(self):
        _, guid, age = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_full.exe"), F.READOBJ)
        probe = uprof_symbols._probe_pdb(F.PDBUTIL, F.PE / "fixture_full.pdb",
                                         guid, age + 1)
        self.assertEqual(probe.status, "mismatch")
        self.assertIn("GUID matches", probe.detail)

    def test_probe_trusts_a_guid_age_cache_key_when_the_pdb_age_differs(self):
        _, guid, age = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_full.exe"), F.READOBJ)
        key_age = age + 1  # image age for the key; the PDB keeps its own age
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / f"{guid}{key_age:X}"
            cache.mkdir()
            candidate = cache / "fixture_full.pdb"
            shutil.copyfile(F.PE / "fixture_full.pdb", candidate)
            probe = uprof_symbols._probe_pdb(F.PDBUTIL, candidate, guid, key_age)
        self.assertEqual(probe.status, "accepted")

    def test_publics_give_both_function_starts(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "publics.csv"
            starts, shared = uprof_symbols.public_symbols(
                out, F.PE / "fixture_full.pdb", F.PDBUTIL)
            with open(out, encoding="utf-8", newline="") as handle:
                rows = {int(row["rva"]): row["names"] for row in csv.DictReader(handle)}
        self.assertEqual((starts, shared), (2, 0))
        self.assertEqual(rows, {F.PE_HELPER: "fixture_helper",
                                F.PE_ENTRY: "fixture_entry"})

    def test_symbolizer_resolves_both_functions_and_the_body_line(self):
        resolved = frames(F.symbolizer_records(
            F.PE / "fixture_full.exe",
            [F.PE_HELPER, F.PE_ENTRY, F.PE_ENTRY_BODY],
            pdb=F.PE / "fixture_full.pdb"))
        self.assert_names_line(resolved[F.PE_HELPER], "fixture_helper", 2)
        self.assert_names_line(resolved[F.PE_ENTRY], "fixture_entry", 6)
        self.assert_names_line(resolved[F.PE_ENTRY_BODY], "fixture_entry", 7)

    @unittest.skipUnless(os.name == "nt", NT_ONLY)
    def test_production_accepts_the_adjacent_pdb_and_reports_folds(self):
        module, records, stream = F.production(
            F.PE / "fixture_full.exe",
            [F.PE_HELPER, F.PE_ENTRY, F.PE_ENTRY_BODY])
        self.assertEqual(Path(module.pdb).name, "fixture_full.pdb")
        self.assertIn("PDB_MATCH=yes", stream)
        self.assertIn("FOLDS: 0 shared starts of 2", stream)
        resolved = frames(records)
        self.assert_names_line(resolved[F.PE_HELPER], "fixture_helper", 2)
        self.assert_names_line(resolved[F.PE_ENTRY], "fixture_entry", 6)
        self.assert_names_line(resolved[F.PE_ENTRY_BODY], "fixture_entry", 7)

    @unittest.skipUnless(os.name == "nt", NT_ONLY)
    def test_production_searches_a_symbol_directory_for_the_pdb(self):
        exe = self.temporary_copy(F.PE / "fixture_full.exe")
        module, records, stream = F.production(exe, [F.PE_ENTRY],
                                               symbol_dirs=[F.PE])
        self.assertEqual(Path(module.pdb).name, "fixture_full.pdb")
        self.assertIn("PDB_MATCH=yes", stream)
        self.assert_names_line(frames(records)[F.PE_ENTRY], "fixture_entry", 6)

    @unittest.skipUnless(os.name == "nt", NT_ONLY)
    def test_production_accepts_a_guid_age_cache_key_directory(self):
        exe = self.temporary_copy(F.PE / "fixture_full.exe")
        _, guid, age = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_full.exe"), F.READOBJ)
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / f"{guid}{age:X}"
            cache.mkdir()
            shutil.copyfile(F.PE / "fixture_full.pdb", cache / "fixture_full.pdb")
            module, records, stream = F.production(exe, [F.PE_ENTRY],
                                                   symbol_dirs=[root])
        self.assertEqual(Path(module.pdb).name, "fixture_full.pdb")
        self.assertIn("PDB_MATCH=yes", stream)
        self.assert_names_line(frames(records)[F.PE_ENTRY], "fixture_entry", 6)


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class PeMismatchedPdbTest(FixtureTestCase):
    """Row 5: a PDB whose GUID differs from the image is rejected; its age happens to match."""

    def test_image_names_the_wrong_pdb_that_sits_beside_it(self):
        name, guid, age = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_mismatch.exe"), F.READOBJ)
        self.assertEqual(F.embedded_basename(name or ""), "fixture_other.pdb")
        self.assertIsNotNone(guid)
        self.assertIsNotNone(age)

    def test_probe_rejects_the_other_pdb_with_the_guid_in_the_detail(self):
        _, image_guid, image_age = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_mismatch.exe"), F.READOBJ)
        other_guid, _, _ = uprof_symbols._pdb_signature(
            F.PDBUTIL, F.PE / "fixture_other.pdb")
        self.assertNotEqual(other_guid, image_guid)
        probe = uprof_symbols._probe_pdb(
            F.PDBUTIL, F.PE / "fixture_other.pdb", image_guid, image_age)
        self.assertEqual(probe.status, "mismatch")
        self.assertEqual(probe.pdb_guid, other_guid)
        self.assertIn(other_guid, probe.detail)
        self.assertIn(image_guid, probe.detail)

    @unittest.skipUnless(os.name == "nt", NT_ONLY)
    def test_production_reports_no_debug_info_when_the_only_pdb_is_rejected(self):
        module, _, _ = F.production(F.PE / "fixture_mismatch.exe", [F.PE_HELPER],
                                    found_debug_info=False)
        self.assertEqual(module.evidence.outcome, "no_debug_info")

    @unittest.skipUnless(os.name == "nt", NT_ONLY)
    def test_production_reports_no_accepted_pdb(self):
        module, records, stream = F.production(F.PE / "fixture_mismatch.exe",
                                               [F.PE_ENTRY])
        self.assertIsNone(module.pdb)
        self.assertEqual(module.evidence.outcome, "no_pdb")
        self.assertIn("PDB=none", stream)
        _, image_guid, _ = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_mismatch.exe"), F.READOBJ)
        other_guid, _, _ = uprof_symbols._pdb_signature(
            F.PDBUTIL, F.PE / "fixture_other.pdb")
        probes = module.evidence.search.probes
        mismatch = [probe for probe in probes if probe.status == "mismatch"]
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0].pdb_guid, other_guid)
        self.assertIn(image_guid, mismatch[0].detail)
        self.assertFalse([probe for probe in probes if probe.status == "accepted"])


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class PePublicOnlyPdbTest(FixtureTestCase):
    """Row 4: a public-only PDB names the functions and carries no line information."""

    def test_public_pdb_has_publics_but_no_type_records(self):
        summary = pdbutil("dump", "--summary", F.PE / "fixture_public.pdb")
        self.assertIn("Has Publics: true", summary)
        self.assertIn("Has Types: true", summary)
        self.assertIn("Showing 0 records",
                      pdbutil("dump", "--types", F.PE / "fixture_public.pdb"))

    def test_publics_give_both_function_starts(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "publics.csv"
            starts, shared = uprof_symbols.public_symbols(
                out, F.PE / "fixture_public.pdb", F.PDBUTIL)
            with open(out, encoding="utf-8", newline="") as handle:
                rows = {int(row["rva"]): row["names"] for row in csv.DictReader(handle)}
        self.assertEqual((starts, shared), (2, 0))
        self.assertEqual(rows, {F.PE_HELPER: "fixture_helper",
                                F.PE_ENTRY: "fixture_entry"})

    def test_symbolizer_names_both_starts_without_lines(self):
        resolved = frames(F.symbolizer_records(
            F.PE / "fixture_public.exe", [F.PE_HELPER, F.PE_ENTRY],
            pdb=F.PE / "fixture_public.pdb"))
        self.assert_names_no_line(resolved[F.PE_HELPER], "fixture_helper")
        self.assert_names_no_line(resolved[F.PE_ENTRY], "fixture_entry")

    @unittest.skipUnless(os.name == "nt", NT_ONLY)
    def test_production_accepts_the_public_pdb(self):
        module, records, stream = F.production(
            F.PE / "fixture_public.exe", [F.PE_HELPER, F.PE_ENTRY])
        self.assertEqual(Path(module.pdb).name, "fixture_public.pdb")
        self.assertIn("PDB_MATCH=yes", stream)
        resolved = frames(records)
        self.assert_names_no_line(resolved[F.PE_HELPER], "fixture_helper")
        self.assert_names_no_line(resolved[F.PE_ENTRY], "fixture_entry")


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class PeDwarfTest(FixtureTestCase):
    """Row 6: a Windows PE carrying DWARF instead of CodeView."""

    def test_image_has_dwarf_sections_and_no_pdb_record(self):
        output = readobj("--file-headers", "--sections", "--coff-debug-directory",
                         F.PE / "fixture_pe_dwarf.exe")
        self.assertIn(".debug_info", output)
        self.assertIn(".debug_line", output)
        self.assertNotIn("PDBGUID", output)
        _, guid, _ = uprof_symbols._readobj_info(
            os.fspath(F.PE / "fixture_pe_dwarf.exe"), F.READOBJ)
        self.assertIsNone(guid)

    def test_symbolizer_resolves_file_and_line(self):
        resolved = frames(F.symbolizer_records(
            F.PE / "fixture_pe_dwarf.exe",
            [F.PE_HELPER, F.PE_ENTRY, F.PE_ENTRY_BODY]))
        self.assert_names_line(resolved[F.PE_HELPER], "fixture_helper", 2)
        self.assert_names_line(resolved[F.PE_ENTRY], "fixture_entry", 6)
        self.assert_names_line(resolved[F.PE_ENTRY_BODY], "fixture_entry", 7)

    def test_production_runs_the_symbolizer_without_a_pdb(self):
        module, records, stream = F.production(
            F.PE / "fixture_pe_dwarf.exe",
            [F.PE_HELPER, F.PE_ENTRY, F.PE_ENTRY_BODY])
        self.assertIsNone(module.pdb)
        # PDB discovery is Windows-only, so only Windows reports a PDB outcome in the stream.
        if os.name == "nt":
            self.assertIn("PDB=none", stream)
        resolved = frames(records)
        self.assert_names_line(resolved[F.PE_HELPER], "fixture_helper", 2)
        self.assert_names_line(resolved[F.PE_ENTRY], "fixture_entry", 6)
        self.assert_names_line(resolved[F.PE_ENTRY_BODY], "fixture_entry", 7)


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class ElfDwarfTest(FixtureTestCase):
    """Row 7: an ELF executable with DWARF."""

    def test_image_is_elf_with_dwarf_sections(self):
        output = readobj("--file-headers", "--sections", F.ELF / "fixture_dwarf")
        self.assertIn("Format: elf64-x86-64", output)
        self.assertIn("Type: SharedObject", output)
        self.assertIn(".debug_info", output)
        self.assertIn(".debug_line", output)

    def test_symbolizer_resolves_functions_file_and_line(self):
        self.assert_dwarf_lines(F.symbolizer_records(
            F.ELF / "fixture_dwarf",
            [F.ELF_HELPER, F.ELF_ENTRY, F.ELF_ENTRY_BODY]))

    def test_context_symbolizer_reads_the_same_dwarf(self):
        symbols = uprof_context.symbolize_rvas(
            str(F.ELF / "fixture_dwarf"),
            [F.ELF_HELPER, F.ELF_ENTRY, F.ELF_ENTRY_BODY])
        self.assertEqual(symbols[F.ELF_HELPER].function, "fixture_helper")
        self.assertEqual(symbols[F.ELF_HELPER].line, 2)
        self.assertEqual(symbols[F.ELF_ENTRY].function, "fixture_entry")
        self.assertEqual(symbols[F.ELF_ENTRY].line, 6)
        self.assertEqual(symbols[F.ELF_ENTRY_BODY].line, 7)

    def test_production_symbolizes_elf_dwarf_without_a_pdb(self):
        module, records, stream = F.production(
            F.ELF / "fixture_dwarf",
            [F.ELF_HELPER, F.ELF_ENTRY, F.ELF_ENTRY_BODY])
        self.assertIsNone(module.pdb)
        # PDB discovery is Windows-only, so only Windows reports a PDB outcome in the stream.
        if os.name == "nt":
            self.assertIn("PDB=none", stream)
        self.assert_dwarf_lines(records)

    def test_function_ranges_report_the_elf_image_as_not_pe(self):
        derivation = uprof_functions.derive_module_ranges(
            "fixture_dwarf", str(F.ELF / "fixture_dwarf"), readobj=str(F.READOBJ))
        self.assertEqual(derivation.ranges, [])
        self.assertTrue(derivation.reason.startswith("not_pe:"), derivation.reason)


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class ElfSharedObjectTest(FixtureTestCase):
    """Row 8: a shared object with DWARF and .dynsym; row 10's .dynsym-only variant."""

    def test_shared_object_exposes_dynsym_and_dwarf(self):
        output = readobj("--file-headers", "--sections", "--dyn-syms",
                         F.ELF / "fixture_shared.so")
        self.assertIn("Type: SharedObject", output)
        self.assertIn(".dynsym", output)
        self.assertIn(".debug_info", output)
        self.assertIn("fixture_helper", output)
        self.assertIn("fixture_entry", output)

    def test_symbolizer_resolves_shared_object_dwarf(self):
        resolved = frames(F.symbolizer_records(
            F.ELF / "fixture_shared.so",
            [F.ELF_SHARED_HELPER, F.ELF_SHARED_ENTRY, F.ELF_SHARED_BODY]))
        self.assert_names_line(resolved[F.ELF_SHARED_HELPER], "fixture_helper", 2)
        self.assert_names_line(resolved[F.ELF_SHARED_ENTRY], "fixture_entry", 6)
        self.assert_names_line(resolved[F.ELF_SHARED_BODY], "fixture_entry", 7)

    def test_dynsym_only_shared_object_gives_names_without_lines(self):
        output = readobj("--file-headers", "--sections", "--dyn-syms",
                         F.ELF / "fixture_dynsym.so")
        self.assertIn(".dynsym", output)
        self.assertNotIn(".debug_info", output)
        self.assertNotIn(".symtab ", output)
        resolved = frames(F.symbolizer_records(
            F.ELF / "fixture_dynsym.so", [F.ELF_SHARED_HELPER, F.ELF_SHARED_ENTRY]))
        self.assert_names_no_line(resolved[F.ELF_SHARED_HELPER], "fixture_helper")
        self.assert_names_no_line(resolved[F.ELF_SHARED_ENTRY], "fixture_entry")


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class ElfSeparateDebugTest(FixtureTestCase):
    """Row 9: stripped ELF images whose DWARF lives in a separate debug file."""

    def test_debuglink_binary_is_stripped_and_its_sidecar_keeps_the_dwarf(self):
        binary = readobj("--sections", F.ELF / "fixture_debuglink")
        self.assertIn(".gnu_debuglink", binary)
        self.assertNotIn(".debug_info", binary)
        debug_file = readobj("--sections", F.ELF / "fixture_debuglink.debug")
        self.assertIn(".debug_info", debug_file)
        self.assertIn(".debug_line", debug_file)

    def test_symbolizer_follows_the_adjacent_debuglink_sidecar(self):
        self.assert_dwarf_lines(F.symbolizer_records(
            F.ELF / "fixture_debuglink",
            [F.ELF_HELPER, F.ELF_ENTRY, F.ELF_ENTRY_BODY]))

    def test_buildid_names_the_separate_debug_file(self):
        build_id = (F.ELF / "build-id.txt").read_text(encoding="utf-8").strip()
        notes = readobj("--notes", F.ELF / "fixture_buildid")
        self.assertIn(f"Build ID: {build_id}", notes)
        debug_file = (F.ELF / "debug" / "buildid" / ".build-id" / build_id[:2]
                      / f"{build_id[2:]}.debug")
        self.assertTrue(debug_file.is_file(), debug_file)

    def test_symbolizer_uses_the_buildid_debug_root(self):
        root = F.ELF / "debug" / "buildid"
        without = frames(F.symbolizer_records(
            F.ELF / "fixture_buildid", [F.ELF_ENTRY]))
        self.assertFalse(without[F.ELF_ENTRY].get("Line"))
        with_root = frames(F.symbolizer_records(
            F.ELF / "fixture_buildid", [F.ELF_ENTRY], debug_dirs=[root]))
        self.assert_names_line(with_root[F.ELF_ENTRY], "fixture_entry", 6)

    def test_production_passes_the_buildid_debug_root_to_the_symbolizer(self):
        module, records, stream = F.production(
            F.ELF / "fixture_buildid", [F.ELF_ENTRY],
            symbol_dirs=[F.ELF / "debug" / "buildid"])
        self.assertIsNone(module.pdb)
        self.assert_names_line(frames(records)[F.ELF_ENTRY], "fixture_entry", 6)

    def test_production_uses_a_debuglink_sidecar_by_adjacency(self):
        module, records, stream = F.production(
            F.ELF / "fixture_debuglink",
            [F.ELF_HELPER, F.ELF_ENTRY, F.ELF_ENTRY_BODY])
        self.assertIsNone(module.pdb)
        self.assert_dwarf_lines(records)

    def test_production_finds_a_debuglink_under_a_fallback_root(self):
        binary = self.temporary_copy(F.ELF / "fixture_debuglink", "fixture_debuglink")
        with tempfile.TemporaryDirectory() as root:
            relative = F.relative_path_for_fallback(Path(binary).parent)
            target = Path(root) / relative / "fixture_debuglink.debug"
            target.parent.mkdir(parents=True)
            shutil.copyfile(F.ELF / "fixture_debuglink.debug", target)
            missing, missing_records, _ = F.production(binary, [F.ELF_ENTRY])
            module, records, _ = F.production(binary, [F.ELF_ENTRY],
                                              symbol_dirs=[root])
        self.assertFalse(frames(missing_records)[F.ELF_ENTRY].get("Line"))
        self.assertIsNone(module.pdb)
        self.assert_names_line(frames(records)[F.ELF_ENTRY], "fixture_entry", 6)

    def test_explicit_debug_files_contribute_their_directory(self):
        debug_file = F.ELF / "fixture_debuglink.debug"
        root = F.ELF / "debug" / "buildid"
        debug_dirs, fallback = uprof_symbols.symbolizer_search_paths(
            [debug_file, F.PE / "fixture_full.pdb"], [root])
        self.assertEqual(debug_dirs, [str(root), str(debug_file.parent)])
        self.assertEqual(fallback, debug_dirs)


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class ElfSymbolTableTest(FixtureTestCase):
    """Row 10: ELF with .symtab and no DWARF; names resolve, lines do not."""

    def test_binary_keeps_symtab_but_has_no_dwarf(self):
        output = readobj("--sections", "--symbols", F.ELF / "fixture_symtab")
        self.assertIn(".symtab", output)
        self.assertNotIn(".debug_info", output)
        self.assertIn("fixture_helper", output)
        self.assertIn("fixture_entry", output)

    def test_symbol_entries_report_function_sizes(self):
        output = readobj("--symbols", F.ELF / "fixture_symtab")
        for name in ("fixture_helper", "fixture_entry"):
            entry = output.split(f"Name: {name}", 1)[1].split("}", 1)[0]
            self.assertRegex(entry, r"Size: [1-9][0-9]*")

    def test_symbolizer_names_both_starts_without_lines(self):
        resolved = frames(F.symbolizer_records(
            F.ELF / "fixture_symtab", [F.ELF_HELPER, F.ELF_ENTRY]))
        self.assert_names_no_line(resolved[F.ELF_HELPER], "fixture_helper")
        self.assert_names_no_line(resolved[F.ELF_ENTRY], "fixture_entry")

    def test_production_keeps_the_names_without_lines(self):
        module, records, stream = F.production(
            F.ELF / "fixture_symtab", [F.ELF_HELPER, F.ELF_ENTRY])
        self.assertIsNone(module.pdb)
        resolved = frames(records)
        self.assert_names_no_line(resolved[F.ELF_HELPER], "fixture_helper")
        self.assert_names_no_line(resolved[F.ELF_ENTRY], "fixture_entry")


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class ElfNoSymbolsTest(FixtureTestCase):
    """Row 11: ELF with neither symbols nor DWARF; the frames stay empty."""

    def test_binary_has_no_named_symbols_or_debug_sections(self):
        output = readobj("--file-headers", "--sections", F.ELF / "fixture_nosym")
        self.assertIn("Format: elf64-x86-64", output)
        for name in (".symtab", ".debug_info", ".debug_line"):
            self.assertNotIn(f"Name: {name} ", output)
        symbols = readobj("--symbols", "--dyn-syms", F.ELF / "fixture_nosym")
        self.assertNotIn("fixture_helper", symbols)
        self.assertNotIn("fixture_entry", symbols)

    def test_eh_frame_reports_function_boundaries_without_symbols(self):
        output = readobj("--unwind", F.ELF / "fixture_nosym")
        self.assertIn("fde_count: 2", output)
        self.assertIn("initial_location: 0x1370", output)
        self.assertIn("initial_location: 0x1380", output)

    def test_symbolizer_returns_an_empty_frame(self):
        resolved = frames(F.symbolizer_records(F.ELF / "fixture_nosym", [F.ELF_HELPER]))
        self.assertFalse(resolved[F.ELF_HELPER].get("FunctionName"))
        self.assertFalse(resolved[F.ELF_HELPER].get("FileName"))
        self.assertEqual(resolved[F.ELF_HELPER].get("Line"), 0)

    def test_production_writes_the_empty_frame_without_failing(self):
        module, records, stream = F.production(F.ELF / "fixture_nosym", [F.ELF_HELPER])
        self.assertIsNone(module.pdb)
        self.assertEqual(len(records), 1)
        frame = F.innermost(records[0])
        self.assertFalse(frame.get("FunctionName"))
        self.assertFalse(frame.get("FileName"))
        self.assertEqual(frame.get("Line"), 0)


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class CompressedAndSplitDwarfTest(FixtureTestCase):
    """Row 12: compressed DWARF and split DWARF from the same source."""

    def test_compressed_sections_are_flagged(self):
        output = readobj("--sections", F.ELF / "fixture_compressed")
        self.assertIn(".debug_info", output)
        self.assertIn("SHF_COMPRESSED", output)

    def test_symbolizer_decompresses_and_resolves_lines(self):
        resolved = frames(F.symbolizer_records(
            F.ELF / "fixture_compressed",
            [F.ELF_COMPRESSED_HELPER, F.ELF_COMPRESSED_ENTRY, F.ELF_COMPRESSED_BODY]))
        self.assert_names_line(resolved[F.ELF_COMPRESSED_HELPER], "fixture_helper", 2)
        self.assert_names_line(resolved[F.ELF_COMPRESSED_ENTRY], "fixture_entry", 6)
        self.assert_names_line(resolved[F.ELF_COMPRESSED_BODY], "fixture_entry", 7)

    def test_split_dwarf_skeleton_resolves_lines(self):
        output = readobj("--sections", F.ELF / "fixture_split")
        self.assertIn(".debug_info", output)
        dwo = F.ELF / "fixture_split-fixture_debug.dwo"
        self.assertTrue(dwo.is_file())
        dwo_sections = readobj("--sections", dwo)
        self.assertIn(".debug_info.dwo", dwo_sections)
        self.assertIn(".debug_str.dwo", dwo_sections)
        resolved = frames(F.symbolizer_records(
            F.ELF / "fixture_split",
            [F.ELF_COMPRESSED_HELPER, F.ELF_COMPRESSED_ENTRY, F.ELF_COMPRESSED_BODY]))
        self.assert_names_line(resolved[F.ELF_COMPRESSED_HELPER], "fixture_helper", 2)
        self.assert_names_line(resolved[F.ELF_COMPRESSED_ENTRY], "fixture_entry", 6)
        self.assert_names_line(resolved[F.ELF_COMPRESSED_BODY], "fixture_entry", 7)

    def test_production_symbolizes_compressed_and_split_dwarf(self):
        for image in (F.ELF / "fixture_compressed", F.ELF / "fixture_split"):
            with self.subTest(image=image.name):
                module, records, _ = F.production(
                    image, [F.ELF_COMPRESSED_HELPER, F.ELF_COMPRESSED_ENTRY,
                            F.ELF_COMPRESSED_BODY])
                self.assertIsNone(module.pdb)
                resolved = frames(records)
                self.assert_names_line(resolved[F.ELF_COMPRESSED_HELPER],
                                       "fixture_helper", 2)
                self.assert_names_line(resolved[F.ELF_COMPRESSED_ENTRY],
                                       "fixture_entry", 6)
                self.assert_names_line(resolved[F.ELF_COMPRESSED_BODY],
                                       "fixture_entry", 7)
