"""Disassembly windows: objdump parsing, boundary selection and raw sample shares.

Run from the repository root: python tests/run_tests.py test_context_window
"""
# Spec: SPEC-88

import subprocess
import unittest
from unittest import mock

import uprof_context

OBJDUMP = """\

fixture.exe:\tfile format coff-x86-64

Disassembly of section .text:

0000000140001000 <fixture_entry>:
140001000:\tpushq\t%rbp
140001001:\tmovq\t%rsp, %rbp
140001004:\tsubq\t$0x20, %rsp
140001008:\tmovl\t$0x1, %eax
14000100d:\taddl\t$0x2, %eax
140001010:\txorl\t%ecx, %ecx
140001012:\tmovl\t%eax, -0x4(%rbp)
140001015:\taddl\t$0x3, %eax
140001018:\tmovl\t%eax, -0x8(%rbp)
14000101b:\tcallq\t*0x1067(%rip)           # 0x140002088
140001021:\taddl\t$0x4, %eax
140001024:\tleave
140001025:\tretq
"""


class ObjdumpParseTest(unittest.TestCase):
    def test_parses_addresses_and_instruction_text(self):
        decoded = uprof_context.parse_objdump(OBJDUMP, image_base=0x140000000)

        self.assertEqual(
            [item.rva for item in decoded],
            [
                0x1000,
                0x1001,
                0x1004,
                0x1008,
                0x100D,
                0x1010,
                0x1012,
                0x1015,
                0x1018,
                0x101B,
                0x1021,
                0x1024,
                0x1025,
            ],
        )
        self.assertEqual(decoded[0].text, "pushq\t%rbp")
        self.assertEqual(decoded[9].text, "callq\t*0x1067(%rip)")

    def test_ignores_section_headers_and_symbol_labels(self):
        decoded = uprof_context.parse_objdump(OBJDUMP, image_base=0x140000000)

        self.assertEqual(
            [item.text for item in decoded],
            [
                "pushq\t%rbp",
                "movq\t%rsp, %rbp",
                "subq\t$0x20, %rsp",
                "movl\t$0x1, %eax",
                "addl\t$0x2, %eax",
                "xorl\t%ecx, %ecx",
                "movl\t%eax, -0x4(%rbp)",
                "addl\t$0x3, %eax",
                "movl\t%eax, -0x8(%rbp)",
                "callq\t*0x1067(%rip)",
                "addl\t$0x4, %eax",
                "leave",
                "retq",
            ],
        )

    def test_attaches_the_next_instruction_address(self):
        decoded = uprof_context.parse_objdump(OBJDUMP, image_base=0x140000000)

        self.assertEqual(decoded[9].next_rva, 0x1021)
        self.assertIsNone(decoded[-1].next_rva)


class WindowSelectionTest(unittest.TestCase):
    def stream(self, count=100):
        return [
            uprof_context.DecodedInstruction(0x1000 + index, f"insn{index}", None)
            for index in range(count)
        ]

    def test_last_thirty_two_before_and_thirty_two_after(self):
        window = uprof_context.select_window(self.stream(), 0x1000 + 50)

        self.assertEqual(len(window), 65)
        self.assertEqual(window[0].rva, 0x1000 + 18)
        self.assertEqual(window[32].rva, 0x1000 + 50)
        self.assertEqual(window[-1].rva, 0x1000 + 82)

    def test_leading_window_is_truncated_at_the_anchor(self):
        window = uprof_context.select_window(self.stream(), 0x1000)

        self.assertEqual(len(window), 33)
        self.assertEqual(window[0].rva, 0x1000)

    def test_trailing_window_is_truncated_at_the_stream_end(self):
        window = uprof_context.select_window(self.stream(), 0x1000 + 99)

        self.assertEqual(len(window), 33)
        self.assertEqual(window[-1].rva, 0x1000 + 99)

    def test_a_mid_instruction_rva_is_rejected(self):
        stream = self.stream()
        del stream[50]

        with self.assertRaises(uprof_context.ContextOmission) as caught:
            uprof_context.select_window(stream, 0x1000 + 50)

        self.assertEqual(caught.exception.reason, "hot_rva_not_instruction_start")


class ShareTest(unittest.TestCase):
    def test_raw_sample_share_uses_the_scoped_total(self):
        self.assertAlmostEqual(uprof_context.instruction_share(3, 60), 0.05)
        self.assertEqual(uprof_context.instruction_share(0, 60), 0.0)

    def test_an_empty_total_has_no_share(self):
        self.assertIsNone(uprof_context.instruction_share(3, 0))


class DecodeWindowTest(unittest.TestCase):
    SECTION_END = 0x2000

    def run_objdump(self, output, returncode=0):
        return mock.patch.object(
            uprof_context,
            "run",
            return_value=subprocess.CompletedProcess(["llvm-objdump"], returncode, output, ""),
        )

    def test_decode_rejects_a_hot_rva_that_is_not_a_boundary(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)
        anchor = uprof_context.Anchor("section", 0x1000)

        with (
            self.run_objdump(OBJDUMP),
            self.assertRaises(uprof_context.ContextOmission) as caught,
        ):
            stream = uprof_context.decode_stream(
                "fixture.exe",
                build,
                anchor,
                0x1003,
                self.SECTION_END,
                objdump="llvm-objdump.exe",
            )
            uprof_context.select_window(stream, 0x1003)

        self.assertEqual(caught.exception.reason, "hot_rva_not_instruction_start")

    def test_decode_returns_the_window_around_the_hot_rva(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)
        anchor = uprof_context.Anchor("section", 0x1000)

        with self.run_objdump(OBJDUMP):
            stream = uprof_context.decode_stream(
                "fixture.exe", build, anchor, 0x1015, self.SECTION_END, objdump="llvm-objdump.exe"
            )
            window = uprof_context.select_window(stream, 0x1015)

        self.assertEqual(window[0].rva, 0x1000)
        self.assertEqual(window[-1].rva, 0x1025)

    def test_stop_address_is_clamped_to_the_section_end(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)
        anchor = uprof_context.Anchor("section", 0x1000)

        with self.run_objdump(OBJDUMP) as invoked:
            uprof_context.decode_stream(
                "fixture.exe", build, anchor, 0x1015, 0x1026, objdump="llvm-objdump.exe"
            )

        self.assertIn("--stop-address=0x140001026", invoked.call_args[0][0])

    def test_objdump_failure_is_an_omission(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)
        anchor = uprof_context.Anchor("section", 0x1000)

        with (
            self.run_objdump("", returncode=1),
            self.assertRaises(uprof_context.ContextOmission) as caught,
        ):
            uprof_context.decode_stream(
                "fixture.exe",
                build,
                anchor,
                0x1015,
                self.SECTION_END,
                objdump="llvm-objdump.exe",
            )

        self.assertEqual(caught.exception.reason, "objdump_failed")


if __name__ == "__main__":
    unittest.main()
