"""The worked example, the derived function path and the header fields are documented.

These assertions pin the guidance that removes the second trial's detour and the trial-5 audit
detour: read the skill file before listing anything, answer the CPU-time question from the
supported output, use `--by module` for the rollup and third-party classification, drill into the
derived function (the tool builds it with LLVM) and fall back to `--by region` only when no
boundary source exists. They also pin the VERSION and SYMBOL_COVERAGE header rows. Wording may
change; the load-bearing claims may not.

Run from this directory: python -m unittest test_docs_guidance -v
"""

import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
SKILL = (TOOLS_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
REFERENCE = (TOOLS_DIR.parent / "references" / "reference.md").read_text(encoding="utf-8")
SPEC = (TOOLS_DIR.parent / "references" / "spec.md").read_text(encoding="utf-8")

READ_FIRST = ("**Read this file before running any command. The first tool call of the task is "
              "the Read of this file; wait for it to complete before any other tool call. Do not "
              "issue any other tool call (no `ls`, `source`, environment check or other command) "
              "in the same turn as this read; the next tool call is `profile --by process`. Do "
              "not list the skill tree, session directory, references or metadata.**")
REGION_MEANING = ("region = 4 KiB address page; samples are grouped by address, not by control "
                  "flow; a page may span unrelated code; the hot RVA is the precise attribution "
                  "and the page is the coarse bucket")

SKILL_FRAGMENTS = (
    READ_FIRST,
    "The first tool call of the task is the Read of this file",
    "wait for it to complete before any other tool call",
    "Do not issue any other tool call",
    "in the same turn as this read",
    "the next tool call is `profile --by process`",
    "Python 3.9 or newer",
    "do not add `-I`",
    "No environment or version preflight",
    "## Quickstart",
    "`profile --by process` first",
    "do not list the skill tree, the session directory, its references or its metadata, "
    "and run `tools` only if a command reports `MISSING`",
    "one `profile --by module` text call",
    "carrying the module's `hot_rva`",
    "do not re-run `--by module --json` to sum",
    "Parse a `--json` document once",
    "do not re-query a row or legend",
    "do not open `callstack.db` or `views.sql`",
    "`profile --by region` is the fallback",
    "do not run `--by region` after the function output has answered",
    "function rows are in the `unresolved` attribution and `function_source` is `none`",
    "instruction context is asked for, not as a default drill-down",
    "`unresolved` is only the tool's attribution category",
    "never `unresolved` or `no named functions`",
    "quote that split in the report",
    "`HOT_RVA:` line",
    "`hot_rva`/`hot_samples`/`other_samples`/`hot_sample_share`",
    "`other_samples` (`samples - hot_samples`)",
    "the rest of the function, not a second hot address",
    "the rest of the row, not a second hot address",
    "must not be used to claim the sample-hot RVA",
    "even when `--json` or `--out-file` writes the document",
    "`--image` selects process image names, not modules",
    "On a single-process attached capture `--image` adds nothing",
    "filter its JSON rows by `module`",
    "`omitted: true` with `reason: symbolizer_failed`",
    "do not spend calls removing temp files",
    "one `CONTEXT:` line",
    "do not read `uprof_context.py` or `context_help`",
    "profile --by module",
    "profile --by instruction --json",
    "## Worked example: where did CPU time go?",
    "attached, cycle-controlled IBS",
    "`VERSION:` is authoritative",
    "names the producing plugin/skill copy and its version",
    "rank by `sample_share`, not `time_share`",
    "`Module.isSystemModule = false`",
    "neither an OS-shipped predicate nor a kernel predicate",
    "dominant non-workload dependency",
    "Stop when you have the module rollup",
    "isSystemModule",
    "cpu_seconds",
    "do not re-derive these shares in SQL",
    "Stop at the derived function",
    "the tool derives function boundaries from the binary",
    "with LLVM",
    "do **not** write a PE parser",
    "map `.pdata` by hand",
    "convert RVAs to VAs",
    "Use `--by instruction` only when disassembly or instruction context is asked for",
    "Do not add `--top all`",
    "there is no `--sort`",
    "function_source",
    "function_derived",
    "function_reason",
    "fully named from the uProf DB; function-range derivation skipped",
    "named, derived (synthetic binary range) and unresolved",
    "hot_rva",
    "a finding, not a blocker",
    "does not modify the capture",
    "read the symbolization warnings on stderr",
    "samples × interval / core_frequency",
    "not a measured cycle count",
    "`--symbol-path",
    "does not read `_NT_SYMBOL_PATH`",
    "The URL is never contacted",
    "DWARF is read from the module itself",
    "first existing candidate",
    "profile symbolizer share",
    "uProf DB share",
    "cause:",
    "remedy:",
    "SYMBOLIZATION:",
    "SYMBOL_COVERAGE:",
    "Third-party",
    "| `VERSION` |",
    "| `SYMBOL_COVERAGE` |",
    "| `--function NAME` |",
    "| `--function-start RVA` |",
    "FILTER:",
    "PARENT:",
    "parent_sample_share",
    "sum/join of the same capture's rows",
    "as its one `NEXT` call",
    "the default, unscoped views keep their existing stop",
)

REFERENCE_FRAGMENTS = (
    "### Symbol coverage",
    "### Derived function ranges",
    "### Region fallback — 4 KiB address pages",
    "### Module rollup",
    "### Absolute CPU time",
    "tools/coverage.sql",
    "profile --by module",
    "One text call answers the module question",
    "highest-sample address as `hot_rva`",
    "`HOT_RVA:` summary",
    "maximum `hot_samples` across all function rows",
    "`unresolved` is the attribution category",
    "a derived range is attributed, not unresolved",
    "report the row attribution, not the channel count",
    "never \"unresolved\" or \"no named functions\"",
    "function rows are in the `unresolved` attribution and `function_source` is `none`",
    "must not be used to claim the sample-hot RVA",
    "one `CONTEXT:` line",
    "omitted: true",
    "single unscoped instruction run",
    "on stderr before the document is written",
    "selects process base images, not modules",
    "`other_samples` is `samples - hot_samples`",
    "isSystemModule = false",
    "neither an OS-shipped predicate nor a kernel predicate",
    "kernel image",
    "dominant non-workload dependency",
    "no source names",
    "v_module_flags",
    "v_function_ranges",
    "v_region_rows",
    "uprof_functions",
    "uprof_primary_measure",
    "profile-region.sql",
    "cpu_seconds",
    "not a measured cycle count",
    "Symbol-path flags on `profile`",
    "ignores `_NT_SYMBOL_PATH`",
    "the URL is never contacted",
    "first existing candidate",
    "uProf DB channel",
    "Profile symbolizer channel",
    "legend.symbol_coverage",
    "rows_drillable",
    "flag names used",
    "cause:",
    "remedy:",
    "Verify translate symbolization",
    "SYMBOLIZATION:",
    "One evidence line per non-resolved module",
    "the consumer does not write a PE parser",
    "does not read `.pdata` by hand",
    "fully named from the uProf DB; function-range derivation skipped",
    "named_samples",
    "derived_samples",
    "a derived range is an attribution",
    "### Function-scoped instruction and line views",
    "v_function_measured",
    "v_function_scope",
    "v_scope_measures",
    "parent_sample_share",
    "two no-range rows with the same name cannot be separated",
    "never a second measurement",
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

    def test_the_read_first_sentence_precedes_the_quickstart(self):
        self.assertLess(SKILL.index("# AMD uProf profile"), SKILL.index(READ_FIRST))
        self.assertLess(SKILL.index(READ_FIRST), SKILL.index("## Quickstart"))

    def test_the_read_and_the_first_command_are_separate_turns(self):
        self.assertLess(SKILL.index(READ_FIRST), SKILL.index("## Quickstart"))
        quickstart = SKILL[SKILL.index("## Quickstart"):SKILL.index("## Commands")]
        self.assertIn("do not issue any other tool call in that turn", quickstart)
        self.assertIn("Then run `profile --by process`", quickstart)

    def test_the_invocation_states_the_interpreter_and_keeps_sibling_imports(self):
        start = SKILL.index("One entry point")
        section = SKILL[start:SKILL.index("## Quickstart")]
        self.assertIn("Python 3.9 or newer", section)
        self.assertIn("do not add `-I`", section)
        self.assertIn("sibling modules", section)
        self.assertIn("No environment or version preflight", section)

    def test_the_region_fallback_keeps_the_required_wording(self):
        for name, text in (("SKILL", SKILL), ("reference", REFERENCE), ("spec", SPEC)):
            with self.subTest(document=name):
                self.assertIn(REGION_MEANING, text)

    def test_spec_lists_the_derived_function_and_region_granularities(self):
        for fragment in ("--by process\\|module\\|function\\|region\\|line\\|instruction",
                         "function_source", "function_derived", "function_reason",
                         "hot_samples", "hot_sample_share", "symbol_reason",
                         "must not write a PE parser",
                         "the tool derives its boundaries from the binary with LLVM",
                         "emits no top-level `function_ranges`",
                         "reduced `function_ranges` with only `derived_modules` and `ranges`",
                         "derived_samples",
                         "The impact order is the mode's primary measure for process, module",
                         "maximum\n  `hot_samples` across all function rows",
                         "must not be used to claim the sample-hot RVA",
                         "`unresolved` is only the attribution category",
                         "must not be called `unresolved` or \"no named functions\"",
                         "function rows remain in the `unresolved` attribution",
                         "function_ranges",
                         "| `--function NAME` |",
                         "FILTER",
                         "PARENT",
                         "function_scope",
                         "parent_sample_share",
                         "other_samples",
                         "even with `--json`/`--out-file`",
                         "`omitted: true` and a `reason`"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, SPEC)

    def test_the_example_stops_below_a_symbol_less_module(self):
        start = SKILL.index("## Worked example: where did CPU time go?")
        end = SKILL.index("## Header lines", start)
        example = SKILL[start:end]

        self.assertIn("SYMBOL_COVERAGE:", example)
        self.assertIn("stop", example)
        self.assertNotIn("objdump -d", example)


if __name__ == "__main__":
    unittest.main()
