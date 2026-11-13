"""End-to-end instruction context against the checked-in stripped PE fixture.

The unit suites replace the LLVM calls; these tests run them on fixtures/fixture.exe so the
readobj, objdump and symbolizer text formats stay exercised. Build commands and provenance are
in fixtures/README.md. Run from the repository root: python tests/run_tests.py test_context_fixture
"""
# Spec: SPEC-54, SPEC-55, SPEC-56

import unittest
from pathlib import Path

import uprof_context

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fixture.exe"
READOBJ = uprof_context.find_readobj()
OBJDUMP = uprof_context.find_objdump()
SYMBOLIZER = uprof_context.find_symbolizer()
SKIP = ("llvm-readobj, llvm-objdump and llvm-symbolizer are required, "
        "and llvm-objdump must support --no-symbolize-operands")


def _objdump_supports_flag() -> bool:
    if not OBJDUMP:
        return False
    return "--no-symbolize-operands" in uprof_context.run([OBJDUMP, "--help"]).stdout
HOT_RVA = 0x101B
TEXT_RVA = 0x1000
SECOND_RVA = 0x1030
INTERNAL_RVA = 0x1050
IAT_SLOT = 0x20A0


def _readobj_text() -> str:
    completed = uprof_context.run(
        [READOBJ, "--file-headers", "--sections", "--coff-imports", "--unwind",
         "--coff-exports", str(FIXTURE)])
    return completed.stdout


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
            return FakeResult([("checksum",), ("size",)])
        if "FROM Module" in text:
            return FakeResult([(self.module_path, self.checksum, 0)])
        if "FROM v_public_starts" in text:
            return FakeResult(list(self.publics))
        if "FROM v_pdbs" in text:
            return FakeResult([(None,)])
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


@unittest.skipUnless(READOBJ and OBJDUMP and SYMBOLIZER and _objdump_supports_flag(), SKIP)
class FixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        text = _readobj_text()
        parsed = uprof_context.parse_image(text)
        cls.build = parsed.build
        cls.sections = parsed.sections
        cls.starts = parsed.unwind_starts
        cls.exports = parsed.exports
        cls.text_end = uprof_context.find_code_section(cls.sections, HOT_RVA).end

    def test_headers_expose_the_identity_and_code_section(self):
        self.assertEqual(self.build.image_base, 0x140000000)
        self.assertEqual(self.build.magic, 0x20B)
        self.assertNotEqual(self.build.checksum, 0)
        self.assertNotEqual(self.build.time_date_stamp, 0)
        self.assertGreater(self.build.size_of_image, 0)
        self.assertEqual(self.build.slot_width, 8)
        section = uprof_context.find_code_section(self.sections, HOT_RVA)
        self.assertIsNotNone(section)
        self.assertEqual(section.start, TEXT_RVA)

    def test_pdata_and_exports_come_from_the_built_image(self):
        self.assertEqual(self.starts,
                         [(TEXT_RVA, None), (SECOND_RVA, None), (INTERNAL_RVA, None)])
        self.assertEqual(self.exports,
                         [(TEXT_RVA, "fixture_entry"), (SECOND_RVA, "fixture_second")])

    def test_matching_checksum_is_verified(self):
        identity = uprof_context.verify_build(self.build, self.build.checksum)

        self.assertTrue(identity.verified)
        self.assertEqual(identity.identity, "checksum")
        with self.assertRaises(uprof_context.ContextOmission) as mismatch:
            uprof_context.verify_build(self.build, self.build.checksum + 1)
        self.assertEqual(mismatch.exception.reason, "build_mismatch")

    def test_a_zero_checksum_without_size_evidence_is_unverified(self):
        identity = uprof_context.verify_build(self.build, 0)

        self.assertFalse(identity.verified)
        self.assertEqual(identity.identity, "unverified")
        self.assertIn("checksum", identity.warning)

    def test_a_zero_checksum_with_the_recorded_size_is_verified(self):
        identity = uprof_context.verify_build(self.build, 0,
                                              capture_size=self.build.size_of_image)

        self.assertTrue(identity.verified)
        self.assertEqual(identity.identity, "size")
        self.assertEqual(identity.capture_size, self.build.size_of_image)

    def test_a_zero_checksum_with_another_size_is_unverified(self):
        identity = uprof_context.verify_build(self.build, 0,
                                              capture_size=self.build.size_of_image + 1)

        self.assertFalse(identity.verified)
        self.assertEqual(identity.identity, "unverified")
        self.assertIn("SizeOfImage", identity.warning)

    def test_an_export_wins_a_tie_over_a_public_at_the_same_start(self):
        anchor = uprof_context.select_anchor(
            HOT_RVA, self.sections, [(TEXT_RVA, "fixture_entry")],
            pdata_starts=self.starts, export_starts=self.exports)

        self.assertEqual(anchor, uprof_context.Anchor("export", TEXT_RVA, "fixture_entry"))

    def test_an_internal_pdata_start_anchors_without_a_name(self):
        anchor = uprof_context.select_anchor(
            INTERNAL_RVA + 5, self.sections, [], pdata_starts=self.starts,
            export_starts=self.exports)

        self.assertEqual(anchor, uprof_context.Anchor("pdata", INTERNAL_RVA))

    def test_without_any_start_the_code_section_is_the_anchor(self):
        anchor = uprof_context.select_anchor(HOT_RVA, self.sections, [])

        self.assertEqual(anchor, uprof_context.Anchor("section", TEXT_RVA))

    def test_window_holds_the_decoded_instructions_around_the_hot_rva(self):
        verified = uprof_context.verify_build(self.build, self.build.checksum)
        window = uprof_context.select_window(
            uprof_context.decode_stream(
                str(FIXTURE), verified,
                uprof_context.Anchor("export", TEXT_RVA, "fixture_entry"), HOT_RVA,
                self.text_end),
            HOT_RVA)
        by_rva = {instruction.rva: instruction for instruction in window}

        self.assertEqual([instruction.rva for instruction in window],
                         sorted(by_rva))
        self.assertEqual(window[0].rva, TEXT_RVA)
        self.assertEqual(by_rva[HOT_RVA].text, "callq\t*0x107f(%rip)")
        self.assertEqual(by_rva[0x1015].text, "addl\t$0x3, %eax")
        self.assertEqual(by_rva[HOT_RVA].next_rva, 0x1021)
        self.assertEqual(by_rva[0x1021].text, "addl\t$0x4, %eax")
        self.assertEqual(by_rva[0x1025].text, "retq")

    def test_hot_rva_off_a_boundary_is_omitted(self):
        verified = uprof_context.verify_build(self.build, self.build.checksum)
        with self.assertRaises(uprof_context.ContextOmission) as omission:
            uprof_context.select_window(
                uprof_context.decode_stream(
                    str(FIXTURE), verified,
                    uprof_context.Anchor("export", TEXT_RVA, "fixture_entry"), 0x1002,
                    self.text_end),
                0x1002)
        self.assertEqual(omission.exception.reason, "hot_rva_not_instruction_start")

    def test_iat_call_resolves_to_exit_process(self):
        imports = uprof_context.load_imports(str(FIXTURE), self.build, readobj=READOBJ)
        self.assertEqual(imports[IAT_SLOT],
                         uprof_context.ImportSlot(IAT_SLOT, "KERNEL32.dll", "ExitProcess"))
        verified = uprof_context.verify_build(self.build, self.build.checksum)
        window = uprof_context.select_window(
            uprof_context.decode_stream(
                str(FIXTURE), verified, uprof_context.Anchor("section", TEXT_RVA), HOT_RVA,
                self.text_end),
            HOT_RVA)
        call = next(item for item in window if item.rva == HOT_RVA)

        slot = uprof_context.resolve_iat_call(call, self.build.image_base, imports)

        self.assertEqual(slot, uprof_context.ImportSlot(IAT_SLOT, "KERNEL32.dll",
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
        self.assertEqual(context["build"]["identity"], "checksum")
        hot = next(item for item in context["instructions"] if item["rva"] == "0x101B")
        self.assertEqual(hot["samples"], 4)
        self.assertEqual(hot["sample_share"], 0.4)
        self.assertEqual(hot["symbol"], {"function": "fixture_entry"})
        self.assertEqual(hot["import"],
                         {"dll": "KERNEL32.dll", "name": "ExitProcess",
                          "slot_rva": f"0x{IAT_SLOT:X}"})
        self.assertNotIn("ibs", hot)

    def test_instruction_contexts_omits_a_mismatched_build(self):
        connection = FakeConnection(str(FIXTURE), self.build.checksum + 1)

        context = uprof_context.instruction_contexts(
            connection, ["module", "rva"], [("fixture.exe", "0x101B")])[0]

        self.assertTrue(context["omitted"])
        self.assertEqual(context["reason"], "build_mismatch")
        self.assertFalse(context["build"]["verified"])
        self.assertEqual(context["build"]["identity"], "unverified")
        self.assertIn("checksum", context["build"]["warning"])

    def test_instruction_contexts_omits_an_off_boundary_hot_rva(self):
        connection = FakeConnection(str(FIXTURE), self.build.checksum)

        context = uprof_context.instruction_contexts(
            connection, ["module", "rva"], [("fixture.exe", "0x1002")])[0]

        self.assertEqual(context["omitted"], True)
        self.assertEqual(context["reason"], "hot_rva_not_instruction_start")
        self.assertTrue(context["build"]["verified"])
        self.assertEqual(context["build"]["identity"], "checksum")


if __name__ == "__main__":
    unittest.main()
