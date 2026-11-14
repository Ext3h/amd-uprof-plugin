"""Header lines do not grow: the words per header field stay within a measured ceiling.

SKILL.md leaves the meaning of each share, the next call and every cause to the header lines the
tools print (#118), so those lines are read on every call and their size is paid in every run's
context. Each ceiling is the field's word count on the trials' attached session when this test
was added, isSystemModule definition of #96 included. A ceiling may shrink; a change that has to
grow a field raises its ceiling here, in the same change, with the reason in the commit.

Paths are replaced by one placeholder word before counting, so a checkout path with spaces does
not change the count. The module view runs everywhere. The symbolizing views run on Windows,
where the ceilings were measured, and skip elsewhere; without llvm-readobj they skip with a reason
that UPROF_REQUIRE_TOOLS=1 turns into a failure.

Run from the repository root: python tests/run_tests.py test_header_budget
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F

REPO = Path(__file__).resolve().parents[1]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"
HEADER = re.compile(r"^([A-Z][A-Z_]+): (.*)$")

CEILINGS = {
    "module": {
        "ACHIEVED": 41,
        "COLUMNS": 145,
        "DUMP": 7,
        "KERNEL_COVERAGE": 38,
        "METRIC": 41,
        "NEXT": 7,
        "READ": 12,
        "SANITY": 24,
        "SYMBOL_COVERAGE": 66,
        "TAG_TO_RETIRE_SHARE": 18,
        "TIME_SHARE": 87,
        "VERSION": 7,
        "WEIGHTS": 13,
    },
    "function": {
        "ACHIEVED": 41,
        "AGREEMENT": 65,
        "COLUMNS": 205,
        "DUMP": 7,
        "HOT_RVA": 35,
        "KERNEL_COVERAGE": 38,
        "METRIC": 41,
        "NEXT": 7,
        "READ": 12,
        "SANITY": 24,
        "SYMBOL_COVERAGE": 150,
        "TAG_TO_RETIRE_SHARE": 18,
        "TIME_SHARE": 87,
        "VERSION": 7,
        "WEIGHTS": 18,
    },
    "line": {
        "ACHIEVED": 41,
        "AGREEMENT": 49,
        "COLUMNS": 58,
        "DUMP": 7,
        "FILTER": 29,
        "KERNEL_COVERAGE": 38,
        "METRIC": 41,
        "NEXT": 13,
        "PARENT": 55,
        "READ": 51,
        "SANITY": 24,
        "SYMBOL_COVERAGE": 150,
        "TAG_TO_RETIRE_SHARE": 18,
        "TIME_SHARE": 87,
        "VERSION": 7,
        "WEIGHTS": 12,
    },
    "instruction": {
        "ACHIEVED": 41,
        "AGREEMENT": 49,
        "COLUMNS": 66,
        "DUMP": 7,
        "FILTER": 29,
        "KERNEL_COVERAGE": 38,
        "METRIC": 41,
        "NEXT": 13,
        "PARENT": 55,
        "READ": 61,
        "SANITY": 24,
        "SYMBOL_COVERAGE": 150,
        "TAG_TO_RETIRE_SHARE": 18,
        "TIME_SHARE": 87,
        "VERSION": 7,
        "WEIGHTS": 13,
        "context_help": 935,
        "legend": 414,
    },
}
SYMBOLIZING = ("function", "line", "instruction")


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class HeaderBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "trials"))
        import build_captures

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "c"
        cls.session = build_captures.build(root, ["attached"])["attached"]
        cls.paths = sorted(
            {
                str(path)
                for base in (REPO, root)
                for path in (base, base.as_posix(), str(base).lower(), base.as_posix().lower())
            },
            key=len,
            reverse=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_profile(self, *args) -> str:
        result = subprocess.run(
            [sys.executable, str(UPROF), "profile", "--session", str(self.session), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def words(self, text: str) -> int:
        for path in self.paths:
            text = text.replace(path, "PATH")
        return len(text.split())

    def measured(self, view: str) -> dict[str, int]:
        if view == "module":
            return self.markdown("--by", "module")
        scope = ["--function", "fixture_entry", "--module", self.own_module()]
        if view == "function":
            return self.markdown("--by", "function")
        if view == "line":
            return self.markdown("--by", "line", *scope)
        document = json.loads(
            self.run_profile("--by", "instruction", "--json", "--top", "1", *scope)
        )
        return {
            key: self.words(value if isinstance(value, str) else json.dumps(value))
            for key, value in document.items()
            if key != "rows"
        }

    def markdown(self, *args) -> dict[str, int]:
        fields: dict[str, int] = {}
        for line in self.run_profile(*args).splitlines():
            if match := HEADER.match(line):
                fields[match.group(1)] = fields.get(match.group(1), 0) + self.words(match.group(2))
        return fields

    def own_module(self) -> str:
        document = json.loads(self.run_profile("--by", "function", "--json"))
        return next(
            row["module"]
            for row in document["rows"]
            if row["function"] == "fixture_entry" and row["module"].endswith("fixture_full.exe")
        )

    def check_views(self, views):
        for view in views:
            fields = self.measured(view)
            for field, words in fields.items():
                with self.subTest(view=view, field=field):
                    self.assertIn(field, CEILINGS[view], f"new header field {field}")
                    self.assertLessEqual(words, CEILINGS[view][field])

    def test_module_header_fields_do_not_grow(self):
        self.check_views(["module"])

    @unittest.skipIf(
        os.name != "nt", "host: the symbolizing views' ceilings were measured on Windows"
    )
    @unittest.skipIf(F.READOBJ is None, "missing llvm-readobj; install LLVM")
    def test_symbolizing_header_fields_do_not_grow(self):
        self.check_views(["function", "line", "instruction"])


if __name__ == "__main__":
    unittest.main()
