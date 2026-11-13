"""Every normative statement of the spec carries one stable ID.

IDs have the form SPEC-nn and are written as a leading bold marker (`**SPEC-07**`), one per
sentence of the requirement sections and one per requirement table row. An ID is assigned once: it
is unique, an amended statement keeps it, and a removed statement's ID is retired, listed under
"Retired IDs" and never used for a statement again.

Run from the repository root: python tests/run_tests.py test_spec_ids
"""

import re
import unittest
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "spec.md"

MARKER = re.compile(r"\*\*(SPEC-\d{2,})\*\*")
ANY_ID = re.compile(r"SPEC-\w*")
RETIRED_HEADING = "## Retired IDs"
# The requirement sections: from the naming rules up to the evidence pointer.
FIRST_SECTION = "## Naming rules"
LAST_SECTION = "## 10. Evidence"
# A sentence that only points to the implementation vocabulary is not a requirement.
POINTER = re.compile(r"are in `reference\.md`\.$")
SENTENCE_END = re.compile(r"(?<=\.)\s+")
LIST_ITEM = re.compile(r"(?:-|\d+\.) ")


def _split(text):
    """The statements part and the retired-ID list of the spec."""
    head, found, tail = text.partition(RETIRED_HEADING)
    return head, tail if found else None


def _number(spec_id):
    return int(spec_id.split("-")[1])


def _blocks(text):
    """Paragraphs, list items and table rows of the requirement sections, as flat strings."""
    section = text[text.index(FIRST_SECTION):text.index(LAST_SECTION)]
    lines = section.split("\n")
    blocks, current = [], []
    for k, line in enumerate(lines):
        if line.startswith("|"):
            is_header = k + 1 < len(lines) and lines[k + 1].startswith("|---")
            if not line.startswith("|---") and not is_header:
                blocks.append(("row", line))
            continue
        if not line.strip() or line.startswith("#"):
            if current:
                blocks.append(("text", " ".join(current)))
            current = []
            continue
        if LIST_ITEM.match(line) and current:
            blocks.append(("text", " ".join(current)))
            current = []
        current.append(line.strip())
    if current:
        blocks.append(("text", " ".join(current)))
    return blocks


class SpecIdTest(unittest.TestCase):
    def setUp(self):
        self.text = SPEC_PATH.read_text(encoding="utf-8")
        self.body, self.retired_section = _split(self.text)

    def test_the_spec_has_a_retired_id_list(self):
        self.assertIsNotNone(self.retired_section)

    def test_statement_ids_are_present_and_unique(self):
        numbers = [_number(i) for i in MARKER.findall(self.body)]
        self.assertTrue(numbers)
        duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
        self.assertEqual(duplicates, [])

    def test_every_id_is_well_formed(self):
        text = self.body.replace("SPEC-nn", "")
        malformed = [m.group(0) for m in ANY_ID.finditer(text)
                     if not re.fullmatch(r"SPEC-\d{2,}", m.group(0))]
        self.assertEqual(malformed, [])
        bare = [m.group(0) for m in re.finditer(r"SPEC-\d{2,}", text)
                if f"**{m.group(0)}**" not in text]
        self.assertEqual(bare, [])

    def test_no_retired_id_is_in_use(self):
        retired = {_number(i) for i in re.findall(r"SPEC-\d{2,}", self.retired_section or "")}
        in_use = {_number(i) for i in MARKER.findall(self.body)}
        self.assertEqual(sorted(retired & in_use), [])

    def test_every_id_up_to_the_highest_is_in_use_or_retired(self):
        # A removed statement whose ID is not listed as retired would let a later statement
        # reuse that ID unnoticed.
        retired = {_number(i) for i in re.findall(r"SPEC-\d{2,}", self.retired_section or "")}
        in_use = {_number(i) for i in MARKER.findall(self.body)}
        self.assertEqual(sorted(set(range(1, max(in_use | retired) + 1)) - in_use - retired), [])

    def test_every_requirement_sentence_and_row_has_its_own_id(self):
        for kind, block in _blocks(self.body):
            if kind == "row":
                with self.subTest(row=block):
                    self.assertTrue(block.startswith("| **SPEC-"))
                continue
            text = LIST_ITEM.sub("", block, count=1) if LIST_ITEM.match(block) else block
            for sentence in SENTENCE_END.split(text):
                if POINTER.search(sentence):
                    continue
                with self.subTest(sentence=sentence):
                    self.assertTrue(MARKER.match(sentence))
                    self.assertEqual(len(MARKER.findall(sentence)), 1)


if __name__ == "__main__":
    unittest.main()
