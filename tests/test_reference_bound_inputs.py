"""reference.md's bound-input table is the full set of variables profile binds for views.sql.

The table claims completeness and states the counts, so a new binding must add its row and keep
the counts true.
"""

import inspect
import re
import unittest
from pathlib import Path

import uprof_query

SKILL = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof"
REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "reference.md"
VIEWS = SKILL / "tools" / "views.sql"
# Bound by the profile call after it locates the dump's Parquet copy, not by _variables.
LATE_BOUND = {"uprof_ibs_parquet"}
WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen", "twenty", "twenty-one", "twenty-two", "twenty-three",
         "twenty-four", "twenty-five"]


def _section() -> str:
    text = REFERENCE.read_text(encoding="utf-8")
    start = text.index("## SQL schema")
    return text[start:text.index("\n## ", start + 1)]


def _table_names(section: str) -> set[str]:
    rows = section.split("| Bound input |", 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"`(uprof_\w+)`", rows))


class BoundInputTableTest(unittest.TestCase):
    def setUp(self):
        self.keys = set(re.findall(r'"(uprof_\w+)":', inspect.getsource(uprof_query._variables)))
        self.bound = self.keys | LATE_BOUND
        header = VIEWS.read_text(encoding="utf-8").split("\n\n", 1)[0]
        self.header = set(re.findall(r"^--\s+(uprof_\w+)", header, re.MULTILINE))
        self.section = _section()

    def test_the_table_lists_every_bound_input(self):
        self.assertEqual(_table_names(self.section), self.bound)

    def test_the_stated_counts_are_true(self):
        self.assertIn(f"binds {WORDS[len(self.bound)]} inputs", self.section)
        self.assertIn(f"the {WORDS[len(self.keys)]} `uprof_*` keys", self.section)
        self.assertIn(f"header lists {WORDS[len(self.header & self.bound)]} of the "
                      f"{WORDS[len(self.bound)]}", self.section)

    def test_every_macro_the_dump_attribution_uses_is_documented(self):
        rows = self.section.split("| Macro |", 1)[1].split("\n\n", 1)[0]
        self.assertIn("`module_ambiguous(name)`", rows)


if __name__ == "__main__":
    unittest.main()
