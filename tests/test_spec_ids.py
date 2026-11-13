"""Every normative statement of the spec carries one stable ID.

IDs have the form SPEC-nn and are written as a leading bold marker (`**SPEC-07**`). An ID is
assigned once: it is unique, an amended statement keeps it, and a removed statement's ID is
retired, listed under "Retired IDs" and never used for a statement again.

Run from the repository root: python tests/run_tests.py test_spec_ids
"""

import re
import unittest
from pathlib import Path

SPEC_PATH = (Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof"
             / "references" / "spec.md")

MARKER = re.compile(r"\*\*(SPEC-\d{2,})\*\*")
ANY_ID = re.compile(r"SPEC-\w*")
RETIRED_HEADING = "## Retired IDs"


def _split(text):
    """The statements part and the retired-ID list of the spec."""
    head, found, tail = text.partition(RETIRED_HEADING)
    return head, tail if found else None


class SpecIdTest(unittest.TestCase):
    def setUp(self):
        self.text = SPEC_PATH.read_text(encoding="utf-8")
        self.body, self.retired_section = _split(self.text)

    def test_the_spec_has_a_retired_id_list(self):
        self.assertIsNotNone(self.retired_section)

    def test_statement_ids_are_present_and_unique(self):
        ids = MARKER.findall(self.body)
        self.assertTrue(ids)
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
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
        retired = set(re.findall(r"SPEC-\d{2,}", self.retired_section or ""))
        self.assertEqual(sorted(retired & set(MARKER.findall(self.body))), [])


if __name__ == "__main__":
    unittest.main()
