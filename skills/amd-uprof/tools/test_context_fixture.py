"""End-to-end instruction context against the checked-in stripped PE fixture.

The unit suites replace the LLVM calls; these tests run them on fixtures/fixture.exe so the
readobj, objdump and symbolizer text formats stay exercised. Build commands and provenance are
in fixtures/README.md. Run from this directory: python -m unittest test_context_fixture -v
"""

import unittest
from pathlib import Path

import uprof_context

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fixture.exe"
READOBJ = uprof_context.find_readobj()
OBJDUMP = uprof_context.find_objdump()
SYMBOLIZER = uprof_context.find_symbolizer()
SKIP = "llvm-readobj, llvm-objdump and llvm-symbolizer are required"
HOT_RVA = 0x101B
TEXT_RVA = 0x1000


def _readobj_text() -> str:
    completed = uprof_context.run(
        [READOBJ, "--file-headers", "--sections", "--coff-imports", str(FIXTURE)])
    return completed.stdout


def _identity() -> tuple[uprof_context.BuildIdentity, list[uprof_context.PeSection]]:
    text = _readobj_text()
    return uprof_context.parse_file_headers(text), uprof_context.parse_sections(text)


class FakeConnection:
    def __init__(self, module_path: str, checksum: int, total: int = 10,
                 publics: tuple[tuple[int, str | None], ...] = ()) -> None:
        self.module_path = module_path
        self.checksum = checksum
        self.total = total
        self.publics = publics

    def execute(self, sql, parameters=None):
        text = " ".join(sql.split())
        if "duckdb_columns" in text:
            return FakeResult([(1,)])
        if "FROM Module" in text:
            return FakeResult([(self.module_path, self.checksum)])
        if "FROM v_public_starts" in text:
            return FakeResult(list(self.publics))
        if "FROM v_scope_totals" in text:
            return FakeResult([(self.total,)])
        if "FROM v_scoped" in text:
            return FakeResult([(TEXT_RVA, 1), (HOT_RVA, 4)])
        if "uprof_ibs_dump" in text:
            return FakeResult([("",)])
        raise AssertionError(f"unexpected query: {text}")


class FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.description = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


@unittest.skipUnless(READOBJ and OBJDUMP and SYMBOLIZER, SKIP)
class FixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build, cls.sections = _identity()

    def test_headers_expose_the_identity_and_code_section(self):
        self.assertEqual(self.build.image_base, 0x140000000)
        self.assertEqual(self.build.magic, 0x20B)
        self.assertNotEqual(self.build.checksum, 0)
        self.assertNotEqual(self.build.time_date_stamp, 0)
        self.assertEqual(self.build.slot_width, 8)
        section = uprof_context.find_code_section(self.sections, HOT_RVA)
        self.assertIsNotNone(section)
        self.assertEqual(section.start, TEXT_RVA)

    def test_build_verification_accepts_only_the_recorded_checksum(self):
        self.assertTrue(uprof_context.verify_build(self.build, self.build.checksum).verified)
        with self.assertRaises(uprof_context.ContextOmission) as mismatch:
            uprof_context.verify_build(self.build, self.build.checksum + 1)
        self.assertEqual(mismatch.exception.reason, "build_mismatch")
        with self.assertRaises(uprof_context.ContextOmission) as missing:
            uprof_context.verify_build(self.build, 0)
        self.assertEqual(missing.exception.reason, "build_unverified")

    def test_anchor_prefers_the_export_and_falls_back_to_the_section(self):
        export = uprof_context.select_anchor(
            HOT_RVA, self.sections, [(TEXT_RVA, "fixture_entry")])
        self.assertEqual(export, uprof_context.Anchor("export", TEXT_RVA, "fixture_entry"))
        section = uprof_context.select_anchor(HOT_RVA, self.sections, [])
        self.assertEqual(section, uprof_context.Anchor("section", TEXT_RVA))

    def test_window_holds_the_decoded_instructions_around_the_hot_rva(self):
        verified = uprof_context.verify_build(self.build, self.build.checksum)
        window = uprof_context.decode_window(
            str(FIXTURE), verified, uprof_context.Anchor("export", TEXT_RVA), HOT_RVA)
        by_rva = {instruction.rva: instruction for instruction in window}

        self.assertEqual([instruction.rva for instruction in window],
                         sorted(by_rva))
        self.assertEqual(window[0].rva, TEXT_RVA)
        self.assertEqual(by_rva[HOT_RVA].text, "callq\t*0x1067(%rip)")
        self.assertEqual(by_rva[0x1015].text, "addl\t$0x3, %eax")
        self.assertEqual(by_rva[HOT_RVA].next_rva, 0x1021)
        self.assertEqual(by_rva[0x1021].text, "addl\t$0x4, %eax")
        self.assertEqual(by_rva[0x1025].text, "retq")

    def test_hot_rva_off_a_boundary_is_omitted(self):
        verified = uprof_context.verify_build(self.build, self.build.checksum)
        with self.assertRaises(uprof_context.ContextOmission) as omission:
            uprof_context.decode_window(
                str(FIXTURE), verified, uprof_context.Anchor("export", TEXT_RVA), 0x1002)
        self.assertEqual(omission.exception.reason, "hot_rva_not_instruction_start")

    def test_iat_call_resolves_to_exit_process(self):
        imports = uprof_context.load_imports(str(FIXTURE), self.build, readobj=READOBJ)
        self.assertEqual(imports[0x2088],
                         uprof_context.ImportSlot(0x2088, "KERNEL32.dll", "ExitProcess"))
        verified = uprof_context.verify_build(self.build, self.build.checksum)
        window = uprof_context.decode_window(
            str(FIXTURE), verified, uprof_context.Anchor("section", TEXT_RVA), HOT_RVA)
        call = next(item for item in window if item.rva == HOT_RVA)

        slot = uprof_context.resolve_iat_call(call, self.build.image_base, imports)

        self.assertEqual(slot, uprof_context.ImportSlot(0x2088, "KERNEL32.dll",
                                                        "ExitProcess"))

    def test_symbolizer_names_the_export_without_source(self):
        symbols = uprof_context.symbolize_rvas(str(FIXTURE), [HOT_RVA])

        self.assertEqual(symbols[HOT_RVA],
                         uprof_context.SymbolInfo("fixture_entry", None, None))

    def test_instruction_contexts_builds_a_verified_block(self):
        connection = FakeConnection(str(FIXTURE), self.build.checksum,
                                    publics=((TEXT_RVA, "fixture_entry"),))

        contexts = uprof_context.instruction_contexts(
            connection, ["module", "rva"], [("fixture.exe", "0x101B")])

        context = contexts[0]
        self.assertNotIn("omitted", context)
        self.assertEqual(context["anchor"],
                         {"kind": "export", "rva": "0x1000", "name": "fixture_entry",
                          "distance": HOT_RVA - TEXT_RVA})
        self.assertTrue(context["build"]["verified"])
        hot = next(item for item in context["instructions"] if item["rva"] == "0x101B")
        self.assertEqual(hot["samples"], 4)
        self.assertEqual(hot["pct_samples"], 40.0)
        self.assertEqual(hot["symbol"], {"function": "fixture_entry"})
        self.assertEqual(hot["import"],
                         {"dll": "KERNEL32.dll", "name": "ExitProcess", "slot_rva": "0x2088"})
        self.assertNotIn("ibs", hot)

    def test_instruction_contexts_omits_a_mismatched_build(self):
        connection = FakeConnection(str(FIXTURE), self.build.checksum + 1)

        context = uprof_context.instruction_contexts(
            connection, ["module", "rva"], [("fixture.exe", "0x101B")])[0]

        self.assertEqual(context["omitted"], True)
        self.assertEqual(context["reason"], "build_mismatch")

    def test_instruction_contexts_omits_an_off_boundary_hot_rva(self):
        connection = FakeConnection(str(FIXTURE), self.build.checksum)

        context = uprof_context.instruction_contexts(
            connection, ["module", "rva"], [("fixture.exe", "0x1002")])[0]

        self.assertEqual(context["omitted"], True)
        self.assertEqual(context["reason"], "hot_rva_not_instruction_start")


if __name__ == "__main__":
    unittest.main()
