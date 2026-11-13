"""Anchor selection: nearest function start from every PE source, section fallback, cap.

Run from the repository root: python tests/run_tests.py test_context_anchor
"""
# Spec: SPEC-89

import unittest

import uprof_context


FILE_HEADERS = """\
File: fixture.exe
Format: COFF-x86-64
Arch: x86_64
AddressSize: 64bit
ImageFileHeader {
  Machine: IMAGE_FILE_MACHINE_AMD64 (0x8664)
  TimeDateStamp: 2026-11-07 17:21:58 (0x6AEF5E36)
}
ImageOptionalHeader {
  Magic: 0x20B
  ImageBase: 0x140000000
  SizeOfImage: 8192
  CheckSum: 0x1234ABCD
}
"""

UNWIND = """\
UnwindInformation [
  RuntimeFunction {
    StartAddress: (0x140002010)
    EndAddress: (0x140002036)
    UnwindInfoAddress: (0x140003000)
  }
  RuntimeFunction {
    StartAddress: (0x140001010)
    EndAddress: (0x140001036)
    UnwindInfoAddress: (0x140003040)
    UnwindInfo {
      Chained {
        StartAddress: (0x140001000)
        EndAddress: (0x140001010)
      }
    }
  }
]
"""

EXPORTS = """\
Export {
  Ordinal: 1
  Name: fixture_entry
  RVA: 0x1000
}
Export {
  Ordinal: 2
  Name:
  RVA: 0x2010
}
"""

SECTIONS = """\
Sections [
  Section {
    Number: 1
    Name: .text (2E 74 65 78 74 00 00 00)
    VirtualSize: 0x26
    VirtualAddress: 0x1000
    RawDataSize: 512
    Characteristics [ (0x60000020)
      IMAGE_SCN_CNT_CODE (0x20)
      IMAGE_SCN_MEM_EXECUTE (0x20000000)
      IMAGE_SCN_MEM_READ (0x40000000)
    ]
  }
  Section {
    Number: 2
    Name: .rdata (2E 72 64 61 74 61 00 00 00)
    VirtualSize: 0x1000
    VirtualAddress: 0x3000
    RawDataSize: 512
    Characteristics [ (0x40000040)
      IMAGE_SCN_CNT_INITIALIZED_DATA (0x40)
      IMAGE_SCN_MEM_READ (0x40000000)
    ]
  }
]
"""


class FileHeaderTest(unittest.TestCase):
    def test_parses_image_base_stamp_checksum_and_magic(self):
        build = uprof_context.parse_image(FILE_HEADERS).build

        self.assertEqual(build.image_base, 0x140000000)
        self.assertEqual(build.time_date_stamp, 0x6AEF5E36)
        self.assertEqual(build.checksum, 0x1234ABCD)
        self.assertEqual(build.magic, 0x20B)
        self.assertEqual(build.slot_width, 8)

    def test_pe32_slots_are_four_bytes(self):
        text = FILE_HEADERS.replace("Magic: 0x20B", "Magic: 0x10B")

        self.assertEqual(uprof_context.parse_image(text).build.slot_width, 4)


class SectionsTest(unittest.TestCase):
    def test_code_section_is_selected_by_characteristics(self):
        sections = uprof_context.parse_image(FILE_HEADERS + "\n" + SECTIONS).sections

        code = uprof_context.find_code_section(sections, 0x1010)
        self.assertEqual((code.start, code.end), (0x1000, 0x1026))
        self.assertIsNone(uprof_context.find_code_section(sections, 0x3010))

    def test_a_section_without_a_virtual_size_uses_the_decimal_raw_size(self):
        text = (FILE_HEADERS + "\n" + SECTIONS).replace("VirtualSize: 0x26", "VirtualSize: 0x0")

        code = uprof_context.find_code_section(uprof_context.parse_image(text).sections, 0x1010)

        self.assertEqual((code.start, code.end), (0x1000, 0x1200))


class WindingAndExportParseTest(unittest.TestCase):
    def test_unwind_starts_are_module_relative_and_deduplicated(self):
        starts = uprof_context.parse_image(FILE_HEADERS + "\n" + UNWIND).unwind_starts

        self.assertEqual([rva for rva, _ in starts], [0x1000, 0x1010, 0x2010])

    def test_unwind_without_runtime_functions_is_empty(self):
        self.assertEqual(
            uprof_context.parse_image(FILE_HEADERS + "\nUnwindInformation [\n]\n").unwind_starts, [])

    def test_export_names_and_rvas_are_paired_per_block(self):
        exports = uprof_context.parse_image(FILE_HEADERS + "\n" + EXPORTS).exports

        self.assertEqual(exports, [(0x1000, "fixture_entry"), (0x2010, None)])

    def test_export_without_a_name_keeps_its_rva(self):
        self.assertEqual(uprof_context.parse_image(FILE_HEADERS + "\n" + EXPORTS).exports[1], (0x2010, None))


class AnchorSelectionTest(unittest.TestCase):
    def setUp(self):
        self.sections = uprof_context.parse_image(FILE_HEADERS + "\n" + SECTIONS).sections

    def test_nearest_pdb_public_start_at_or_below_wins(self):
        anchor = uprof_context.select_anchor(
            0x1015, self.sections, [(0x1000, "entry"), (0x1010, "hot"), (0x1020, "after")])

        self.assertEqual(anchor.kind, "pdb_public")
        self.assertEqual(anchor.rva, 0x1010)
        self.assertEqual(anchor.name, "hot")

    def test_pdata_start_is_used_and_labelled_pdata(self):
        anchor = uprof_context.select_anchor(
            0x1015, self.sections, [], pdata_starts=[(0x1000, None), (0x1010, None)])

        self.assertEqual(anchor, uprof_context.Anchor("pdata", 0x1010))

    def test_export_start_is_used_and_labelled_export(self):
        anchor = uprof_context.select_anchor(
            0x1015, self.sections, [], export_starts=[(0x1000, "entry"), (0x1010, "hot")])

        self.assertEqual(anchor, uprof_context.Anchor("export", 0x1010, "hot"))

    def test_nearest_source_wins_across_sources(self):
        anchor = uprof_context.select_anchor(
            0x1015, self.sections, [(0x1005, "public")],
            pdata_starts=[(0x1010, None)], export_starts=[(0x1000, "export")])

        self.assertEqual(anchor, uprof_context.Anchor("pdata", 0x1010))

    def test_a_tie_prefers_the_named_export(self):
        anchor = uprof_context.select_anchor(
            0x1015, self.sections, [(0x1010, "public")],
            pdata_starts=[(0x1010, None)], export_starts=[(0x1010, "export")])

        self.assertEqual(anchor, uprof_context.Anchor("export", 0x1010, "export"))

    def test_a_tie_without_an_export_prefers_the_pdb_public(self):
        anchor = uprof_context.select_anchor(
            0x1015, self.sections, [(0x1010, "public")], pdata_starts=[(0x1010, None)])

        self.assertEqual(anchor, uprof_context.Anchor("pdb_public", 0x1010, "public"))

    def test_start_above_the_hot_rva_is_ignored(self):
        anchor = uprof_context.select_anchor(0x1015, self.sections, [(0x1020, "after")])

        self.assertEqual(anchor.kind, "section")
        self.assertEqual(anchor.rva, 0x1000)

    def test_start_in_another_section_is_ignored(self):
        anchor = uprof_context.select_anchor(
            0x1010, self.sections, [(0x3000, "data")],
            pdata_starts=[(0x3000, None)], export_starts=[(0x3000, "data")])

        self.assertEqual(anchor.kind, "section")
        self.assertEqual(anchor.rva, 0x1000)

    def test_no_safe_start_falls_back_to_the_code_section_start(self):
        anchor = uprof_context.select_anchor(0x1015, self.sections, [])

        self.assertEqual(anchor.kind, "section")
        self.assertEqual(anchor.rva, 0x1000)

    def test_cap_applies_to_the_section_fallback(self):
        sections = [uprof_context.PeSection(0x1000, uprof_context.ANCHOR_DISTANCE_CAP + 0x2000,
                                           True)]
        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.select_anchor(
                0x1000 + uprof_context.ANCHOR_DISTANCE_CAP + 1, sections, [])

        self.assertEqual(caught.exception.reason, "anchor_too_far")

    def test_cap_applies_to_a_distant_start_from_any_source(self):
        sections = [uprof_context.PeSection(0x1000, uprof_context.ANCHOR_DISTANCE_CAP + 0x2000,
                                           True)]
        hot = 0x1000 + uprof_context.ANCHOR_DISTANCE_CAP + 1
        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.select_anchor(
                hot, sections, [(0x1000, "entry")], pdata_starts=[(0x1000, None)],
                export_starts=[(0x1000, "entry")])

        self.assertEqual(caught.exception.reason, "anchor_too_far")

    def test_hot_rva_outside_a_code_section_is_omitted(self):
        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.select_anchor(0x4000, self.sections, [])

        self.assertEqual(caught.exception.reason, "no_code_section")

    def test_boundary_validation_rejects_a_mid_instruction_rva(self):
        instructions = [
            uprof_context.DecodedInstruction(0x1000, "mov", 4),
            uprof_context.DecodedInstruction(0x1004, "add", 4),
            uprof_context.DecodedInstruction(0x1008, "retq", 1),
        ]
        self.assertEqual(
            len(uprof_context.select_window(instructions, 0x1004)), 3)
        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.select_window(instructions, 0x1002)
        self.assertEqual(caught.exception.reason, "hot_rva_not_instruction_start")


class BuildIdentityTest(unittest.TestCase):
    def setUp(self):
        self.build = uprof_context.parse_image(FILE_HEADERS).build

    def test_size_of_image_is_read_from_the_headers(self):
        self.assertEqual(self.build.size_of_image, 8192)

    def test_matching_capture_checksum_is_verified(self):
        identity = uprof_context.verify_build(self.build, 0x1234ABCD)

        self.assertTrue(identity.verified)
        self.assertEqual(identity.identity, "checksum")
        self.assertEqual(identity.checksum, 0x1234ABCD)
        self.assertEqual(identity.capture_checksum, 0x1234ABCD)
        self.assertIsNone(identity.warning)

    def test_mismatched_checksum_is_omitted(self):
        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.verify_build(self.build, 0x1234ABCE)

        self.assertEqual(caught.exception.reason, "build_mismatch")

    def test_zero_capture_checksum_with_matching_size_is_size_verified(self):
        identity = uprof_context.verify_build(self.build, 0, capture_size=8192)

        self.assertTrue(identity.verified)
        self.assertEqual(identity.identity, "size")
        self.assertEqual(identity.capture_size, 8192)

    def test_zero_capture_checksum_with_different_size_is_unverified(self):
        identity = uprof_context.verify_build(self.build, 0, capture_size=8193)

        self.assertFalse(identity.verified)
        self.assertEqual(identity.identity, "unverified")
        self.assertIn("SizeOfImage", identity.warning)
        self.assertEqual(identity.capture_size, 8193)

    def test_zero_module_checksum_with_capture_checksum_is_unverified(self):
        build = uprof_context.parse_image(FILE_HEADERS.replace("0x1234ABCD", "0x0")).build

        identity = uprof_context.verify_build(build, 0x1234ABCD, capture_size=None)

        self.assertFalse(identity.verified)
        self.assertEqual(identity.identity, "unverified")
        self.assertIn("checksum", identity.warning)

    def test_both_checksums_missing_without_a_size_is_unverified(self):
        for capture_checksum in (None, 0):
            with self.subTest(checksum=capture_checksum):
                identity = uprof_context.verify_build(self.build, capture_checksum)

                self.assertFalse(identity.verified)
                self.assertEqual(identity.identity, "unverified")
                self.assertIn("no usable", identity.warning)


if __name__ == "__main__":
    unittest.main()
