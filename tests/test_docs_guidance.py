"""The worked example, the derived function path and the header fields are documented.

These assertions pin the guidance that removes the second trial's detour and the trial-5 audit
detour: read the skill file before listing anything, answer the CPU-time question from the
supported output, use `--by module` for the rollup and third-party classification, drill into the
derived function (the tool builds it with LLVM) and fall back to `--by region` only when no
boundary source exists. They also pin the VERSION and SYMBOL_COVERAGE header rows. Wording may
change; the load-bearing claims may not.

Run from the repository root: python tests/run_tests.py test_docs_guidance
"""
# Spec: SPEC-56, SPEC-57, SPEC-58, SPEC-59, SPEC-60, SPEC-61,
#       SPEC-62, SPEC-63, SPEC-79, SPEC-80, SPEC-81

import re
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
SKILL = (TOOLS_DIR.parent / "SKILL.md").read_text(encoding="utf-8")
DOCS = Path(__file__).resolve().parents[1] / "docs"
REFERENCE = (DOCS / "reference.md").read_text(encoding="utf-8")
# The spec wording without its requirement ID markup.
SPEC = re.sub(r"\*\*SPEC-\d{2,}\*\* ", "", (DOCS / "spec.md").read_text(encoding="utf-8"))
VALIDATION = (DOCS / "validation.md").read_text(encoding="utf-8")

FIRST_CALL = (
    "**First call, `--by` from the question: `python <SKILL_DIR>/tools/uprof.py "
    "profile --session <capture dir> --by function|module|process`; its `NEXT` line "
    "names the next.**"
)
REGION_MEANING = (
    "region = 4 KiB address page; samples are grouped by address, not by control "
    "flow; a page may span unrelated code; the hot RVA is the most precise attribution "
    "and the page is the coarse bucket"
)


def _flat(text):
    """Compare wording without depending on where a document wraps its lines."""
    return " ".join(text.split())


SKILL_FRAGMENTS = (
    FIRST_CALL,
    "Python 3.9 or newer",
    "do not add `-I`",
    "No environment or version preflight",
    "## Quickstart",
    "run `tools` only if a command reports `MISSING`",
    "First call by question",
    "where CPU time went, or mine versus the rest: `--by module`",
    "a system-wide capture with no process named: `--by process`",
    "An unscoped module, function or region view of a system-wide capture names `--by process` "
    "first in `NEXT`",
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
    "tools/uprof.py profile --session <SESSION_DIR> --by module",
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
    "or an image GUID no PDB is used",
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
    'never "unresolved" or "no named functions"',
    "function rows are in the `unresolved` attribution and `function_source` is `none`",
    "must not be used to claim the sample-hot RVA",
    "one `CONTEXT:` line",
    "omitted: true",
    "single unscoped instruction run",
    "on stderr before the document is written",
    "`--image` filters on `Process.name`, not modules",
    "`other_samples` is `samples - hot_samples`",
    "isSystemModule = false",
    "neither an OS-shipped predicate nor a kernel predicate",
    "kernel image",
    "dominant non-workload dependency",
    "no source names",
    "v_module_flags_exact",
    "v_module_flags_basename",
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
    "or an image GUID (llvm-readobj missing",
    "uProf DB channel",
    "Profile symbolizer channel",
    "legend.symbol_coverage",
    "rows_drillable",
    "flag names used",
    "cause:",
    "remedy:",
    "Verify translate symbolization",
    "SYMBOLIZATION:",
    "one evidence line per distinct cause, naming its modules",
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

    def test_the_first_call_sentence_precedes_the_quickstart(self):
        self.assertLess(SKILL.index("# AMD uProf profile"), SKILL.index(FIRST_CALL))
        self.assertLess(SKILL.index(FIRST_CALL), SKILL.index("## Quickstart"))

    def test_the_function_lens_is_offered_before_the_module_lens(self):
        # Small models take the first option offered; most questions need --by function first,
        # and only "where did time go" or mine-versus-the-rest starts at --by module.
        first_line = SKILL[SKILL.index("**First call") : SKILL.index("One entry point")]
        self.assertIn("--by function|module|process", first_line)
        quickstart = SKILL[SKILL.index("## Quickstart") : SKILL.index("## Commands")]
        choice = quickstart[quickstart.index("First call by question") :]
        self.assertLess(choice.index("`--by function`"), choice.index("`--by module`"))
        self.assertNotIn("the CPU-time path is module, then function", SKILL)

    def test_no_fixed_first_call_or_self_read_is_mandated(self):
        # A loaded skill needs no Read of itself, and an attached capture goes straight to the
        # lens the question needs; only a system-wide capture starts with the process ranking.
        for fragment in (
            "the Read of this file",
            "`profile --by process` first",
            "Then run `profile --by process`",
            "1. **Rank the processes.**",
        ):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, SKILL)

    def test_the_invocation_states_the_interpreter_and_keeps_sibling_imports(self):
        start = SKILL.index("One entry point")
        section = SKILL[start : SKILL.index("## Quickstart")]
        self.assertIn("Python 3.9 or newer", section)
        self.assertIn("do not add `-I`", section)
        self.assertIn("sibling modules", section)
        self.assertIn("No environment or version preflight", section)

    def test_the_region_fallback_keeps_the_required_wording(self):
        for name, text in (("SKILL", SKILL), ("reference", REFERENCE), ("spec", SPEC)):
            with self.subTest(document=name):
                self.assertIn(REGION_MEANING, _flat(text))

    def test_spec_states_the_requirements_behind_the_derived_function_and_region_paths(self):
        spec = _flat(SPEC)
        for fragment in (
            "--by process\\|module\\|function\\|region\\|line\\|instruction",
            "`--function NAME`",
            "the tool derives boundaries from the binary",
            "must not parse the image or map its function tables by hand",
            "A synthetic range is visibly labelled and attributed",
            "`unresolved` is only the attribution category",
            '`derived`, never `unresolved` or "no named functions"',
            "rows remain `unresolved` only when neither a name nor a derived range covers them",
            "report per-scope agreement and coverage",
            "the maximum per-function hot sample count",
            "must not be used to claim the sample-hot address",
            "other_samples",
            "module keys are normalized",
            "parent totals are a join of the capture's rows, not a second measurement",
            "the profile symbolizer share, not the share of samples the capture itself named",
            "another build's source is a resolution failure, not success",
            "even with `--json`/`--out-file`",
            "an omitted row consumes a slot",
            "a supplied measure is present even at zero",
            "scope-coverage verdicts are part of `profile`",
            "system-flagged vendor dependency is the dominant non-workload dependency",
            "`interval` does not exist",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, spec)

    def test_the_example_stops_below_a_symbol_less_module(self):
        start = SKILL.index("## Worked example: where did CPU time go?")
        end = SKILL.index("## Header lines", start)
        example = SKILL[start:end]

        self.assertIn("SYMBOL_COVERAGE:", example)
        self.assertIn("stop", example)
        self.assertNotIn("objdump -d", example)


class ValidationQuoteTest(unittest.TestCase):
    def test_agreement_quotes_use_the_printed_field_syntax(self):
        # The AGREEMENT line prints `mismatch_rate=<p>%`, `name_mismatches=<n> addresses of <m>
        # compared` and `unresolved=<u> addresses`; a quote of it may abbreviate but not respell.
        spans = [
            s
            for s in re.findall(r"`([^`]*)`", VALIDATION)
            if re.search(r"mismatch_rate|name_mismatches|unresolved[ =]\d", s)
        ]
        self.assertTrue(spans)
        for span in spans:
            with self.subTest(quote=span):
                if "mismatch_rate" in span:
                    self.assertRegex(span, r"mismatch_rate=[\d.]+%")
                if "name_mismatches" in span:
                    self.assertRegex(span, r"name_mismatches=\d+ addresses of \d+ compared")
                if re.search(r"unresolved[ =]\d", span):
                    self.assertRegex(span, r"unresolved=\d+( addresses|$)")


if __name__ == "__main__":
    unittest.main()
