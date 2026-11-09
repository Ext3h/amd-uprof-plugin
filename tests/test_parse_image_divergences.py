"""parse_image must match the single-purpose parsers on block-tolerant input."""

import unittest

import uprof_context

HEADERS = """ImageBase: 0x140000000
CheckSum: 0x1234
Magic: 0x20B
TimeDateStamp: 2026-06-10 (0x6AEF5E36)
"""

SECTION = """Section {
  Name: .text
  VirtualSize: 0x1000
  VirtualAddress: 0x1000
  RawDataSize: 512
  Characteristics [ (0x60000020) ]
  IMAGE_SCN_CNT_CODE
}
"""


class ParseImageDivergenceTest(unittest.TestCase):
    def test_an_export_block_with_extra_spacing_is_recognized(self):
        text = HEADERS + "Export  {\n  RVA: 0x1010\n  Name: foo\n}\n"

        image = uprof_context.parse_image(text)

        self.assertEqual(image.exports, [(0x1010, "foo")])

    def test_a_start_address_inside_a_section_block_is_recorded(self):
        text = HEADERS + SECTION.replace(
            "  IMAGE_SCN_CNT_CODE\n",
            "  StartAddress: (0x140001000)\n  IMAGE_SCN_CNT_CODE\n")

        image = uprof_context.parse_image(text)

        self.assertEqual(image.unwind_starts, [(0x1000, None)])

    def test_a_start_address_inside_an_export_block_is_recorded(self):
        text = HEADERS + (
            "Export {\n  RVA: 0x1010\n  Name: foo\n"
            "  StartAddress: (0x140001000)\n}\n")

        image = uprof_context.parse_image(text)

        self.assertEqual(image.unwind_starts, [(0x1000, None)])
        self.assertEqual(image.exports, [(0x1010, "foo")])


if __name__ == "__main__":
    unittest.main()
