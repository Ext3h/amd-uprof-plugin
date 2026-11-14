"""The instruction view shows the decoded instruction at each row's RVA, in Markdown and JSON.

A small model asked what is hot and why disassembled the image itself or read the whole JSON
document, because the text view printed no instruction (#385). The row now carries an
`instruction` column, the llvm-objdump decode at the row's RVA from the same context build the
JSON window uses; it is NULL where the row's context omits. The context block stays JSON-only.
The view needs no NEXT back to `--by line`, and the line view's NEXT names the instruction view
without `--json`.

Runs on Windows with LLVM and skips elsewhere, with a reason that UPROF_REQUIRE_TOOLS=1 turns
into a failure. Run from the repository root: python tests/run_tests.py test_instruction_column
"""
# Spec: SPEC-86, SPEC-87

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F

REPO = Path(__file__).resolve().parents[1]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
@unittest.skipIf(os.name != "nt", "host: the instruction fixtures run on Windows")
@unittest.skipIf(F.READOBJ is None, "missing llvm-readobj; install LLVM")
class InstructionColumnTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "trials"))
        import build_captures

        cls.tmp = tempfile.TemporaryDirectory()
        cls.session = build_captures.build(Path(cls.tmp.name) / "c", ["attached"])["attached"]
        document = json.loads(cls.run_profile("--by", "function", "--json"))
        module = next(
            row["module"]
            for row in document["rows"]
            if row["function"] == "fixture_entry" and row["module"].endswith("fixture_full.exe")
        )
        cls.scope = ["--function", "fixture_entry", "--module", module]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def run_profile(cls, *args) -> str:
        result = subprocess.run(
            [sys.executable, str(UPROF), "profile", "--session", str(cls.session), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout

    def test_the_text_view_shows_the_instruction_at_each_row(self):
        lines = self.run_profile("--by", "instruction", *self.scope, "--top", "1").splitlines()
        table = [line for line in lines if line.startswith("| ")]
        header = [cell.strip() for cell in table[0].strip("|").split("|")]
        self.assertEqual(header[:3], ["module", "rva", "instruction"])
        first = [cell.strip() for cell in table[2].strip("|").split("|")]
        self.assertNotIn(first[2], ("", "NULL"))
        self.assertNotIn("\\t", first[2])
        for line in table[2:]:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells[0].startswith("&lt;other"):
                self.assertEqual(cells[2], "NULL")

    def test_the_json_row_carries_the_same_instruction_as_its_context(self):
        document = json.loads(
            self.run_profile("--by", "instruction", *self.scope, "--top", "1", "--json")
        )
        row = document["rows"][0]
        entry = next(
            item
            for run in row["context"]["runs"]
            for item in run["instructions"]
            if item["rva"] == row["rva"]
        )
        self.assertEqual(row["instruction"], " ".join(entry["instruction"].split()))
        for other in document["rows"]:
            if other["module"].startswith("<other"):
                self.assertIsNone(other["instruction"])

    def test_the_instruction_view_does_not_send_the_caller_back_to_lines(self):
        output = self.run_profile("--by", "instruction", *self.scope, "--top", "1")
        self.assertNotIn("--by line", output)


if __name__ == "__main__":
    unittest.main()
