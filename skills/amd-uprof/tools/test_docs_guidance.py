"""The worked example and new header fields are documented where a user will read them.

These assertions pin the guidance that removes the second trial's detour: answer the CPU-time
question from the supported output, use `--by module` for the rollup and third-party
classification, and stop at module/RVA when a module has no symbols instead of writing a PE
parser. They also pin the VERSION and SYMBOL_COVERAGE header rows. Wording may change; the
load-bearing claims may not.

Run from this directory: python -m unittest test_docs_guidance -v
"""

import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
SKILL = (TOOLS_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
REFERENCE = (TOOLS_DIR.parent / "references" / "reference.md").read_text(encoding="utf-8")

SKILL_FRAGMENTS = (
    "## Quickstart",
    "`profile --by process` first",
    "profile --by module",
    "profile --by instruction --json",
    "## Worked example: where did CPU time go?",
    "attached, cycle-controlled IBS",
    "`VERSION:` is authoritative",
    "names the producing plugin/skill copy and its version",
    "rank by `sample_share`, not `time_share`",
    "`Module.isSystemModule = false`",
    "dominant non-workload dependency",
    "Stop when you have the module rollup",
    "isSystemModule",
    "cpu_seconds",
    "do not query `cpu.db` or write ad-hoc SQL",
    "samples × interval / core_frequency",
    "SYMBOL_COVERAGE:",
    "Do **not** write a PE parser",
    "map `.pdata` by hand",
    "convert RVAs to VAs",
    "Third-party",
    "| `VERSION` |",
    "| `SYMBOL_COVERAGE` |",
)

REFERENCE_FRAGMENTS = (
    "### Symbol coverage",
    "### Module rollup",
    "### Absolute CPU time",
    "tools/coverage.sql",
    "profile --by module",
    "isSystemModule = false",
    "kernel image",
    "dominant non-workload dependency",
    "no named functions",
    "v_module_flags",
    "cpu_seconds",
)


class DocsGuidanceTest(unittest.TestCase):
    def test_skill_carries_the_worked_example_and_header_rows(self):
        for fragment in SKILL_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, SKILL)

    def test_reference_carries_the_mechanics_behind_the_example(self):
        for fragment in REFERENCE_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, REFERENCE)

    def test_the_example_stops_below_a_symbol_less_module(self):
        start = SKILL.index("## Worked example: where did CPU time go?")
        end = SKILL.index("## Header lines", start)
        example = SKILL[start:end]

        self.assertIn("SYMBOL_COVERAGE:", example)
        self.assertIn("stop", example)
        self.assertNotIn("objdump -d", example)


if __name__ == "__main__":
    unittest.main()
