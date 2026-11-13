"""The recorded trial scenarios in examples/ stay consistent with the current tools.

The timer-heap-churn scenario's README lists the minimal calls; this test runs exactly those
commands against the committed timer fixture and compares the output with expected.json, so a
tool change that alters the expected answer, the call chain or a documented command fails here.
Every share in the README's expected answer must follow from expected.json, so the prose cannot
go stale on its own (issue #37).

Run from the repository root: python tests/run_tests.py test_examples
"""

import json
import math
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import timer_fixture
import uprof_native

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
SCENARIO = EXAMPLES / "timer-heap-churn"
SKILL_DIR = REPO / "plugin" / "skills" / "amd-uprof"
COMMAND = re.compile(r"^\d+\. `python <SKILL_DIR>/tools/uprof\.py (profile [^`]+)`$", re.M)
STEP = re.compile(r"^\d+\. (.*)$", re.M)


def _skip_reason():
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return "duckdb is required; install duckdb==2.0.0"
    symbolizer = uprof_native.find_symbolizer()
    if symbolizer is None:
        return "llvm-symbolizer is required; install LLVM"
    return uprof_native.llvm_cause(str(symbolizer))


def _readme():
    return (SCENARIO / "README.md").read_text(encoding="utf-8")


def _expected():
    return json.loads((SCENARIO / "expected.json").read_text(encoding="utf-8"))


def _section(text, title):
    return text.split(f"## {title}", 1)[1].split("\n## ", 1)[0]


def _counts(expected):
    """Every sample count the expected answer may quote a share of."""
    total = expected["total_samples"]
    own = expected["modules"][expected["own_module"]]["samples"]
    counts = {values["samples"] for values in expected["modules"].values()}
    counts |= set(expected["functions"].values())
    counts.add(total - own)
    counts.add(sum(expected["functions"][name] for name in expected["heap_functions"]))
    return counts


def _percent(count, total):
    return f"{100 * count / total:.1f}"


def _half_width(count, total):
    share = count / total
    return f"{100 * 1.96 * math.sqrt(share * (1 - share) / total):.1f}"


class ExamplesLayoutTest(unittest.TestCase):
    def test_examples_live_outside_the_plugin_package(self):
        self.assertTrue((EXAMPLES / "README.md").is_file())
        self.assertFalse((REPO / "plugin" / "examples").exists())
        for scenario in ("timer-heap-churn", "ibs-trial-9"):
            with self.subTest(scenario=scenario):
                self.assertIn(f"]({scenario}/README.md)",
                              (EXAMPLES / "README.md").read_text(encoding="utf-8"))
                self.assertTrue((EXAMPLES / scenario / "README.md").is_file())

    def test_ibs_scenario_is_marked_not_reproducible(self):
        text = (EXAMPLES / "ibs-trial-9" / "README.md").read_text(encoding="utf-8")
        self.assertIn("**Status: not reproducible.**", text)

    def test_documented_call_count_matches_the_steps(self):
        expected = _expected()
        section = _section(_readme(), "Minimal path: ")
        count = int(section.split(" ", 1)[0])
        self.assertEqual(count, expected["minimal_tool_calls"])
        self.assertEqual(len(STEP.findall(section)), count)
        self.assertEqual(len(COMMAND.findall(section)), count)
        self.assertIn(f"Tool calls: {count} ", _section(_readme(), "Reference metrics"))

    def test_the_path_follows_the_skill_for_an_attached_capture(self):
        # SKILL.md picks the first call from the question; an attached capture has no process
        # ranking step, and loading the skill is not a counted step.
        steps = STEP.findall(_section(_readme(), "Minimal path: "))
        self.assertNotIn("--by process", steps[0])
        self.assertIn("--by module", steps[0])
        for step in steps:
            self.assertNotIn("SKILL.md", step)
        self.assertEqual([re.search(r"--by (\w+)", step).group(1) for step in steps],
                         _expected()["calls"])

    def test_every_share_in_the_answer_follows_from_expected_json(self):
        expected = _expected()
        total = expected["total_samples"]
        counts = _counts(expected)
        answer = _section(_readme(), "Expected answer")
        percents = {_percent(count, total) for count in counts}
        widths = {_half_width(count, total) for count in counts}
        quoted = re.findall(r"(?<![\d±])(\d+\.\d)%", answer)
        self.assertTrue(quoted)
        for value in quoted:
            with self.subTest(percent=value):
                self.assertIn(value, percents)
        for value in re.findall(r"±(\d+\.\d) points", answer):
            with self.subTest(width=value):
                self.assertIn(value, widths)
        for count in re.findall(r"(\d+) samples", answer):
            with self.subTest(samples=count):
                self.assertIn(int(count), counts | {total, expected["hot_rva"]["samples"]})


class TimerScenarioTest(unittest.TestCase):
    """Replay the README's commands on the fixture and compare with expected.json."""

    @classmethod
    def setUpClass(cls):
        reason = _skip_reason()
        if reason:
            raise unittest.SkipTest(reason)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.session = timer_fixture.build(Path(cls.tmp.name) / "session")
        cls.expected = _expected()
        cls.commands = COMMAND.findall(_readme())
        cls.outputs = [cls.run_documented(command) for command in cls.commands]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def args(cls, command):
        return [arg.replace("<SESSION_DIR>", str(cls.session))
                   .replace("<FIXTURE_DIR>", str(timer_fixture.IMAGE_DIR))
                for arg in shlex.split(command)]

    @classmethod
    def run_documented(cls, command):
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "tools" / "uprof.py"), *cls.args(command)],
            capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            raise AssertionError(f"{command} exited {result.returncode}: {result.stderr}")
        return result.stdout

    @staticmethod
    def rows(text):
        lines = [line for line in text.splitlines() if line.startswith("| ")]
        header = [cell.strip() for cell in lines[0].strip("|").split("|")]
        return [dict(zip(header, (cell.strip() for cell in line.strip("|").split("|"))))
                for line in lines[2:]]

    def output(self, by):
        index = next(i for i, command in enumerate(self.commands)
                     if f"--by {by}" in command)
        return self.outputs[index]

    def test_the_second_call_is_the_first_calls_next_line(self):
        next_line = re.search(r"^NEXT: (.*)$", self.output("module"), re.M).group(1)
        printed = shlex.split(next_line, posix=False)
        tail = printed[printed.index("profile"):]
        documented = self.args(self.commands[1])
        self.assertEqual([arg.strip('"') for arg in tail], documented)

    def test_metric_and_module_split(self):
        module = self.output("module")
        total = self.expected["total_samples"]
        own = self.expected["modules"][self.expected["own_module"]]["samples"]
        self.assertIn(self.expected["metric_prefix"], module)
        self.assertIn(f"top row {self.expected['own_module']} {_percent(own, total)}% "
                      f"±{_half_width(own, total)} points (95% binomial, n={total})", module)
        rows = {row["module"]: row for row in self.rows(module)}
        for name, values in self.expected["modules"].items():
            with self.subTest(module=name):
                self.assertEqual(int(rows[name]["samples"]), values["samples"])
                self.assertEqual(rows[name]["isSystemModule"],
                                 str(values["isSystemModule"]).lower())
        self.assertEqual(sum(int(row["samples"]) for row in rows.values()), total)

    def test_functions_and_hot_rva(self):
        function = self.output("function")
        rows = self.rows(function)
        self.assertEqual(rows[0]["function"], self.expected["top_function"])
        samples = {row["function"]: int(row["samples"]) for row in rows}
        for name, count in self.expected["functions"].items():
            with self.subTest(function=name):
                self.assertEqual(samples.get(name), count)
        hot = self.expected["hot_rva"]
        self.assertRegex(function, rf"(?m)^HOT_RVA: .* at {hot['rva']} with {hot['samples']} "
                                   r"samples")

    def test_hash_block_lines(self):
        # The split between the loop's lines depends on the line table, so only the line set,
        # the top line and the total are pinned.
        lines = self.expected["hash_block_lines"]
        rows = self.rows(self.output("line"))
        top = rows[:len(lines["lines"])]
        self.assertEqual({row["file"] for row in top}, {lines["file"]})
        self.assertEqual(sorted(int(row["line"]) for row in top), lines["lines"])
        self.assertEqual(int(top[0]["line"]), lines["top_line"])
        self.assertEqual(sum(int(row["samples"]) for row in top),
                         self.expected["functions"][self.expected["top_function"]])


if __name__ == "__main__":
    unittest.main()
