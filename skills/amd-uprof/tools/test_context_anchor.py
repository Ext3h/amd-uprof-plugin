"""Anchor selection: nearest public start, section fallback, distance cap, boundary check.

Run from this directory: python -m unittest test_context_anchor -v
"""

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
  CheckSum: 0x1234ABCD
}
"""

SECTIONS = """\
Sections [
  Section {
    Number: 1
    Name: .text (2E 74 65 78 74 00 00 00)
    VirtualSize: 0x26
    VirtualAddress: 0x1000
    RawDataSize: 0x200
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
    RawDataSize: 0x200
    Characteristics [ (0x40000040)
      IMAGE_SCN_CNT_INITIALIZED_DATA (0x40)
      IMAGE_SCN_MEM_READ (0x40000000)
    ]
  }
]
"""


class FileHeaderTest(unittest.TestCase):
    def test_parses_image_base_stamp_checksum_and_magic(self):
        build = uprof_context.parse_file_headers(FILE_HEADERS)

        self.assertEqual(build.image_base, 0x140000000)
        self.assertEqual(build.time_date_stamp, 0x6AEF5E36)
        self.assertEqual(build.checksum, 0x1234ABCD)
        self.assertEqual(build.magic, 0x20B)
        self.assertEqual(build.slot_width, 8)

    def test_pe32_slots_are_four_bytes(self):
        text = FILE_HEADERS.replace("Magic: 0x20B", "Magic: 0x10B")

        self.assertEqual(uprof_context.parse_file_headers(text).slot_width, 4)


class SectionsTest(unittest.TestCase):
    def test_code_section_is_selected_by_characteristics(self):
        sections = uprof_context.parse_sections(SECTIONS)

        code = uprof_context.find_code_section(sections, 0x1010)
        self.assertEqual((code.start, code.end), (0x1000, 0x1026))
        self.assertIsNone(uprof_context.find_code_section(sections, 0x3010))


class AnchorSelectionTest(unittest.TestCase):
    def setUp(self):
        self.sections = uprof_context.parse_sections(SECTIONS)

    def test_nearest_public_start_at_or_below_wins(self):
        anchor = uprof_context.select_anchor(
            0x1015, self.sections, [(0x1000, "entry"), (0x1010, "hot"), (0x1020, "after")])

        self.assertEqual(anchor.kind, "export")
        self.assertEqual(anchor.rva, 0x1010)
        self.assertEqual(anchor.name, "hot")

    def test_public_start_above_the_hot_rva_is_ignored(self):
        anchor = uprof_context.select_anchor(0x1015, self.sections, [(0x1020, "after")])

        self.assertEqual(anchor.kind, "section")
        self.assertEqual(anchor.rva, 0x1000)

    def test_public_start_in_another_section_is_ignored(self):
        anchor = uprof_context.select_anchor(0x1010, self.sections, [(0x3000, "data")])

        self.assertEqual(anchor.kind, "section")
        self.assertEqual(anchor.rva, 0x1000)

    def test_no_public_start_falls_back_to_the_code_section_start(self):
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

    def test_cap_applies_to_a_distant_public_start(self):
        sections = [uprof_context.PeSection(0x1000, uprof_context.ANCHOR_DISTANCE_CAP + 0x2000,
                                           True)]
        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.select_anchor(
                0x1000 + uprof_context.ANCHOR_DISTANCE_CAP + 1, sections,
                [(0x1000, "entry")])

        self.assertEqual(caught.exception.reason, "anchor_too_far")

    def test_hot_rva_outside_a_code_section_is_omitted(self):
        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.select_anchor(0x4000, self.sections, [])

        self.assertEqual(caught.exception.reason, "no_code_section")

    def test_boundary_validation_rejects_a_mid_instruction_rva(self):
        self.assertTrue(uprof_context.is_instruction_start([0x1000, 0x1004, 0x1008], 0x1004))
        self.assertFalse(uprof_context.is_instruction_start([0x1000, 0x1004, 0x1008], 0x1002))


class BuildIdentityTest(unittest.TestCase):
    def setUp(self):
        self.build = uprof_context.parse_file_headers(FILE_HEADERS)

    def test_matching_capture_checksum_is_verified(self):
        identity = uprof_context.verify_build(self.build, 0x1234ABCD)

        self.assertTrue(identity.verified)
        self.assertEqual(identity.checksum, 0x1234ABCD)
        self.assertEqual(identity.capture_checksum, 0x1234ABCD)

    def test_mismatched_checksum_is_omitted(self):
        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.verify_build(self.build, 0x1234ABCE)

        self.assertEqual(caught.exception.reason, "build_mismatch")

    def test_capture_without_a_checksum_is_unverified_and_omitted(self):
        for capture_checksum in (None, 0):
            with self.subTest(checksum=capture_checksum):
                with self.assertRaises(uprof_context.ContextOmission) as caught:
                    uprof_context.verify_build(self.build, capture_checksum)

                self.assertEqual(caught.exception.reason, "build_unverified")

    def test_module_without_a_checksum_is_unverified(self):
        build = uprof_context.parse_file_headers(FILE_HEADERS.replace("0x1234ABCD", "0x0"))

        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.verify_build(build, 0x0)

        self.assertEqual(caught.exception.reason, "build_unverified")


if __name__ == "__main__":
    unittest.main()
