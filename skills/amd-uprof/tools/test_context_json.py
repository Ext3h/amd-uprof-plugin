"""JSON context assembly: deterministic block shape, row cap and omission paths.

Run from this directory: python -m unittest test_context_json -v
"""

import subprocess
import unittest
from unittest import mock

import uprof_context
import uprof_db
import uprof_output


FILE_HEADERS = """\
File: fixture.exe
Format: COFF-x86-64
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
    Name: .text (2E 74 65 78 74 00 00 00)
    VirtualSize: 0x1000
    VirtualAddress: 0x1000
    RawDataSize: 0x200
    Characteristics [ (0x60000020)
      IMAGE_SCN_CNT_CODE (0x20)
      IMAGE_SCN_MEM_EXECUTE (0x20000000)
    ]
  }
]
"""

OBJDUMP = """\
Disassembly of section .text:
0000000140001000 <fixture_entry>:
140001000:\tpushq\t%rbp
140001004:\tmovl\t$0x1, %eax
140001015:\taddl\t$0x3, %eax
14000101b:\tcallq\t*0x1067(%rip)
140001021:\taddl\t$0x4, %eax
140001024:\tretq
"""

IMPORTS = """\
Import {
  Name: KERNEL32.dll
  ImportLookupTableRVA: 0x2078
  ImportAddressTableRVA: 0x2088
  Symbol: ExitProcess (0)
}
"""

SYMBOLS = """\
{"Address":"0x101b","ModuleName":"fixture.exe","Symbol":[{"Column":0,"FileName":"src\\\\main.cpp","FunctionName":"main","Line":12}]}
"""

COLUMNS = ["module", "rva", "function", "file", "line", "attribution", "samples",
           "pct_samples", "pct_time", "pct_cycles"]
ROW = ("fixture.exe", "0x101B", "main", "src\\main.cpp", 12, "unique", 3, 5.0, 2.0, 1.0)


class FakeConnection:
    def __init__(self, module_path, checksum=0x1234ABCD, total=60, publics=()):
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
            return FakeResult([(0x1000, 1), (0x101B, 3)])
        if "uprof_ibs_dump" in text:
            return FakeResult([("",)])
        raise AssertionError(f"unexpected query: {text}")


class FakeResult:
    def __init__(self, rows, description=None):
        self._rows = rows
        self.description = description or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def fake_run(command, **_kwargs):
    if "--coff-imports" in command:
        return subprocess.CompletedProcess(command, 0, IMPORTS, "")
    if "--file-headers" in command:
        return subprocess.CompletedProcess(command, 0, FILE_HEADERS + SECTIONS, "")
    if command[0] == "llvm-objdump.exe":
        return subprocess.CompletedProcess(command, 0, OBJDUMP, "")
    if command[0] == "llvm-symbolizer.exe":
        return subprocess.CompletedProcess(command, 0, SYMBOLS, "")
    raise AssertionError(f"unexpected command: {command}")


class BuildContextTest(unittest.TestCase):
    def test_window_order_symbol_import_and_raw_share(self):
        build = uprof_context.parse_file_headers(FILE_HEADERS)
        verified = uprof_context.verify_build(build, 0x1234ABCD)
        anchor = uprof_context.Anchor("export", 0x1000, "fixture_entry")
        window = uprof_context.parse_objdump(OBJDUMP, build.image_base)
        imports = uprof_context.parse_imports(IMPORTS, build)

        context = uprof_context.build_context(
            anchor, verified, 0x101B, window,
            samples={0x1000: 1, 0x101B: 3}, total_samples=60,
            symbols={0x101B: uprof_context.SymbolInfo("main", r"src\main.cpp", 12)},
            imports=imports, ibs={})

        self.assertEqual(context["anchor"],
                         {"kind": "export", "rva": "0x1000", "name": "fixture_entry",
                          "distance": 0x1B})
        self.assertTrue(context["build"]["verified"])
        self.assertEqual([item["rva"] for item in context["instructions"]],
                         ["0x1000", "0x1004", "0x1015", "0x101B", "0x1021", "0x1024"])
        hot = context["instructions"][3]
        self.assertEqual(hot["samples"], 3)
        self.assertEqual(hot["pct_samples"], 5.0)
        self.assertEqual(hot["symbol"],
                         {"function": "main", "file": r"src\main.cpp", "line": 12})
        self.assertEqual(hot["import"],
                         {"dll": "KERNEL32.dll", "name": "ExitProcess",
                          "slot_rva": "0x2088"})
        self.assertNotIn("ibs", hot)

    def test_absent_symbol_and_ibs_fields_are_omitted(self):
        build = uprof_context.parse_file_headers(FILE_HEADERS)
        verified = uprof_context.verify_build(build, 0x1234ABCD)
        instruction = uprof_context.DecodedInstruction(0x1000, "retq", None)

        context = uprof_context.build_context(
            uprof_context.Anchor("section", 0x1000), verified, 0x1000, [instruction],
            samples={}, total_samples=0, symbols={}, imports={}, ibs={})

        entry = context["instructions"][0]
        self.assertEqual(entry["samples"], 0)
        self.assertIsNone(entry["pct_samples"])
        self.assertNotIn("symbol", entry)
        self.assertNotIn("import", entry)
        self.assertNotIn("ibs", entry)


class JsonDocumentTest(unittest.TestCase):
    def test_context_is_attached_to_its_row(self):
        contexts = [{"anchor": {"kind": "section"}}, None]

        text = uprof_output.json_document([("METRIC", "x")], ["module", "rva"],
                                          [("a", "0x1"), ("b", "0x2")], contexts)

        self.assertIn('"context"', text)
        self.assertEqual(text.count('"context"'), 1)

    def test_no_contexts_keep_the_existing_bytes(self):
        without = uprof_output.json_document([("METRIC", "x")], ["module", "rva"],
                                             [("a", "0x1")])

        self.assertNotIn("context", without)

    def test_mismatched_context_count_is_rejected(self):
        with self.assertRaises(ValueError):
            uprof_output.json_document([], ["rva"], [("0x1",)], [])


class OutputTest(unittest.TestCase):
    def test_markdown_bytes_ignore_contexts(self):
        columns, types, rows = ["module", "rva"], ["VARCHAR", "VARCHAR"], [("a", "0x1")]
        with mock.patch.object(uprof_db, "fetch_typed", return_value=(columns, types, rows)):
            with mock.patch.object(uprof_db, "write_payload") as written:
                uprof_db.output(None, None, [("METRIC", "x")], False, None,
                                contexts=[{"anchor": {}}])
                with_context = written.call_args[0][0]
            with mock.patch.object(uprof_db, "write_payload") as written:
                uprof_db.output(None, None, [("METRIC", "x")], False, None)
                without_context = written.call_args[0][0]

        self.assertEqual(with_context, without_context)


class InstructionContextsTest(unittest.TestCase):
    def test_missing_module_is_a_machine_readable_omission(self):
        connection = FakeConnection(module_path="missing-fixture.exe")
        with mock.patch.object(uprof_context.Path, "is_file", return_value=False):
            contexts = uprof_context.instruction_contexts(connection, COLUMNS, [ROW])

        self.assertEqual(contexts[0],
                         {"omitted": True, "reason": "module_missing",
                          "detail": "missing-fixture.exe is not on this host"})

    def test_rows_past_the_cap_report_row_cap(self):
        connection = FakeConnection(module_path="missing-fixture.exe")
        rows = [ROW, ROW, ROW]
        with mock.patch.object(uprof_context.Path, "is_file", return_value=False):
            contexts = uprof_context.instruction_contexts(connection, COLUMNS, rows, cap=1)

        self.assertEqual(contexts[0]["reason"], "module_missing")
        self.assertEqual(contexts[1], {"omitted": True, "reason": "row_cap"})
        self.assertEqual(contexts[2], {"omitted": True, "reason": "row_cap"})

    def test_an_aggregate_row_has_no_context(self):
        row = ("<other instructions>", None, None, None, None, None, 1, 1.0, 1.0, 1.0)

        contexts = uprof_context.instruction_contexts(FakeConnection("x"), COLUMNS, [row])

        self.assertEqual(contexts, [None])

    def test_successful_context_joins_every_annotation(self):
        connection = FakeConnection(module_path=__file__, total=60,
                                    publics=[(0x1000, "fixture_entry")])
        with mock.patch.object(uprof_context, "run", side_effect=fake_run):
            with mock.patch.object(uprof_context, "find_readobj",
                                   return_value="llvm-readobj.exe"):
                with mock.patch.object(uprof_context, "find_objdump",
                                       return_value="llvm-objdump.exe"):
                    with mock.patch.object(uprof_context, "find_symbolizer",
                                           return_value="llvm-symbolizer.exe"):
                        with mock.patch.object(uprof_context, "find_pdb", return_value=None):
                            contexts = uprof_context.instruction_contexts(
                                connection, COLUMNS, [ROW])

        context = contexts[0]
        self.assertEqual(context["anchor"]["kind"], "export")
        self.assertEqual(context["anchor"]["name"], "fixture_entry")
        hot = next(item for item in context["instructions"] if item["rva"] == "0x101B")
        self.assertEqual(hot["import"]["name"], "ExitProcess")
        self.assertEqual(hot["symbol"]["function"], "main")


class OmissionJsonTest(unittest.TestCase):
    def test_omission_carries_reason_and_detail(self):
        omission = uprof_context.ContextOmission("build_unverified", "no checksum")

        self.assertEqual(omission.as_json(),
                         {"omitted": True, "reason": "build_unverified",
                          "detail": "no checksum"})


if __name__ == "__main__":
    unittest.main()
