"""IAT annotation: parse llvm-readobj --coff-imports and resolve indirect call slots.

Run from the repository root: python tests/run_tests.py test_context_iat
"""
# Spec: SPEC-41

import subprocess
import unittest
from unittest import mock

import uprof_context


IMPORTS = """\
File: fixture.exe
Format: COFF-x86-64
Import {
  Name: KERNEL32.dll
  ImportLookupTableRVA: 0x2078
  ImportAddressTableRVA: 0x2088
  Symbol: ExitProcess (0)
}
Import {
  Name: ntdll.dll
  ImportLookupTableRVA: 0x2098
  ImportAddressTableRVA: 0x2090
  Symbol: RtlSizeHeap (1544)
  Symbol: NtClose (37)
}
"""

ORDINAL_IMPORTS = """\
Import {
  Name: FOO.dll
  ImportLookupTableRVA: 0x2030
  ImportAddressTableRVA: 0x2038
  Symbol:  (17)
  Symbol: Named (0)
}
"""


class ImportParseTest(unittest.TestCase):
    def test_slots_follow_symbol_order_and_the_iat_base(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)

        imports = uprof_context.parse_imports(IMPORTS, build)

        self.assertEqual(imports[0x2088], uprof_context.ImportSlot(0x2088, "KERNEL32.dll",
                                                                   "ExitProcess"))
        self.assertEqual(imports[0x2090], uprof_context.ImportSlot(0x2090, "ntdll.dll",
                                                                   "RtlSizeHeap"))
        self.assertEqual(imports[0x2098], uprof_context.ImportSlot(0x2098, "ntdll.dll",
                                                                   "NtClose"))

    def test_pe32_slots_are_four_bytes_apart(self):
        build = uprof_context.BuildIdentity(0x400000, 0, 0x100, 0x10B)

        imports = uprof_context.parse_imports(IMPORTS, build)

        self.assertEqual(imports[0x2088].name, "ExitProcess")
        self.assertEqual(imports[0x2090].name, "RtlSizeHeap")
        self.assertEqual(imports[0x2094].name, "NtClose")

    def test_empty_imports_are_handled(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)

        self.assertEqual(uprof_context.parse_imports("File: fixture.exe\n", build), {})

    def test_delay_imports_are_not_treated_as_imports(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)
        text = IMPORTS.replace("Import {", "DelayImport {", 1)

        imports = uprof_context.parse_imports(text, build)

        self.assertNotIn(0x2088, imports)

    def test_an_ordinal_only_symbol_is_named_by_its_ordinal(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)

        imports = uprof_context.parse_imports(ORDINAL_IMPORTS, build)

        self.assertEqual(imports[0x2038].name, "#17")

    def test_an_ordinal_only_symbol_still_consumes_its_slot(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)

        imports = uprof_context.parse_imports(ORDINAL_IMPORTS, build)

        self.assertEqual(imports[0x2040].name, "Named")
        self.assertEqual(imports[0x2038].slot_rva, 0x2038)


class IatCallTest(unittest.TestCase):
    def setUp(self):
        self.build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)
        self.imports = uprof_context.parse_imports(IMPORTS, self.build)

    def test_rip_relative_call_resolves_the_next_boundary(self):
        instruction = uprof_context.DecodedInstruction(0x101B, "callq\t*0x1067(%rip)", 0x1021)

        slot = uprof_context.resolve_iat_call(instruction, self.build.image_base, self.imports)

        self.assertEqual(slot.name, "ExitProcess")
        self.assertEqual(slot.slot_rva, 0x2088)

    def test_absolute_call_target_resolves(self):
        instruction = uprof_context.DecodedInstruction(0x101B, "callq\t*0x140002090", 0x1021)

        slot = uprof_context.resolve_iat_call(instruction, self.build.image_base, self.imports)

        self.assertEqual(slot.name, "RtlSizeHeap")

    def test_register_indirect_call_stays_unresolved(self):
        instruction = uprof_context.DecodedInstruction(0x101B, "callq\t*%rax", 0x1021)

        self.assertIsNone(
            uprof_context.resolve_iat_call(instruction, self.build.image_base, self.imports))

    def test_a_call_that_is_not_an_iat_slot_stays_unresolved(self):
        instruction = uprof_context.DecodedInstruction(0x101B, "callq\t*0x1000(%rip)", 0x1021)

        self.assertIsNone(
            uprof_context.resolve_iat_call(instruction, self.build.image_base, self.imports))

    def test_a_non_call_instruction_is_not_resolved(self):
        instruction = uprof_context.DecodedInstruction(0x101B, "addl\t$3, %eax", 0x1021)

        self.assertIsNone(
            uprof_context.resolve_iat_call(instruction, self.build.image_base, self.imports))


class LoadImportsTest(unittest.TestCase):
    def test_reads_the_import_table_from_the_module(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)
        completed = subprocess.CompletedProcess(["llvm-readobj"], 0, IMPORTS, "")

        with mock.patch.object(uprof_context, "run", return_value=completed) as invoked:
            imports = uprof_context.load_imports("fixture.exe", build,
                                                 readobj="llvm-readobj.exe")

        self.assertEqual(imports[0x2088].name, "ExitProcess")
        self.assertIn("--coff-imports", invoked.call_args[0][0])

    def test_readobj_failure_is_an_omission(self):
        build = uprof_context.BuildIdentity(0x140000000, 0, 0x100, 0x20B)
        completed = subprocess.CompletedProcess(["llvm-readobj"], 1, "", "boom")

        with mock.patch.object(uprof_context, "run", return_value=completed):
            with self.assertRaises(uprof_context.ContextOmission) as caught:
                uprof_context.load_imports("fixture.exe", build,
                                           readobj="llvm-readobj.exe")

        self.assertEqual(caught.exception.reason, "imports_failed")


if __name__ == "__main__":
    unittest.main()
