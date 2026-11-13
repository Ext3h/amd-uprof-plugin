"""The recorded trial scenarios in examples/ stay consistent with the current tools.

The timer-heap-churn scenario's README lists the minimal calls; this test runs exactly those
commands against the committed timer fixture and compares the output with expected.json, so a
tool change that alters the expected answer, the call chain or a documented command fails here
(issue #37).

Run from the repository root: python tests/run_tests.py test_examples
"""

import json
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
STEP = re.compile(r"^\d+\. ", re.M)


def _skip_reason():
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return "duckdb is required; install duckdb==2.0.0"
    symbolizer = uprof_native.find_symbolizer()
    if symbolizer is None:
        return "llvm-symbolizer is required; install LLVM"
    return uprof_native.llvm_cause(str(symbolizer))


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
        text = (SCENARIO / "README.md").read_text(encoding="utf-8")
        expected = json.loads((SCENARIO / "expected.json").read_text(encoding="utf-8"))
        section = text.split("## Minimal path: ", 1)[1].split("\n## ", 1)[0]
        count = int(section.split(" ", 1)[0])
        self.assertEqual(count, expected["minimal_tool_calls"])
        self.assertEqual(len(STEP.findall(section)), count)


class TimerScenarioTest(unittest.TestCase):
    """Replay the README's commands on the fixture and compare with expected.json."""

    @classmethod
    def setUpClass(cls):
        reason = _skip_reason()
        if reason:
            raise unittest.SkipTest(reason)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.session = timer_fixture.build(Path(cls.tmp.name) / "session")
        cls.expected = json.loads((SCENARIO / "expected.json").read_text(encoding="utf-8"))
        text = (SCENARIO / "README.md").read_text(encoding="utf-8")
        cls.commands = COMMAND.findall(text)
        cls.outputs = [cls.run_documented(command) for command in cls.commands]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def run_documented(cls, command):
        args = [arg.replace("<SESSION_DIR>", str(cls.session))
                   .replace("<FIXTURE_DIR>", str(timer_fixture.IMAGE_DIR))
                for arg in shlex.split(command)]
        result = subprocess.run([sys.executable, str(SKILL_DIR / "tools" / "uprof.py"), *args],
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

    def test_the_readme_documents_four_profile_calls(self):
        self.assertEqual([re.search(r"--by (\w+)", c).group(1) for c in self.commands],
                         ["process", "module", "function", "line"])

    def test_each_call_is_the_previous_calls_next_step(self):
        for previous, following in (("process", "module"), ("module", "function")):
            with self.subTest(previous=previous):
                next_line = re.search(r"^NEXT: .*$", self.output(previous), re.M).group(0)
                self.assertIn(f"--by {following}", next_line)

    def test_metric_and_module_split(self):
        module = self.output("module")
        self.assertIn(self.expected["metric_prefix"], module)
        self.assertIn(self.expected["module_read_clause"], module)
        rows = {row["module"]: row for row in self.rows(module)}
        for name, values in self.expected["modules"].items():
            with self.subTest(module=name):
                self.assertEqual(int(rows[name]["samples"]), values["samples"])
                self.assertEqual(rows[name]["isSystemModule"],
                                 str(values["isSystemModule"]).lower())
        self.assertEqual(sum(int(row["samples"]) for row in rows.values()),
                         self.expected["total_samples"])

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
        rows = self.rows(self.output("line"))
        got = [{"file": row["file"], "line": int(row["line"]), "samples": int(row["samples"])}
               for row in rows[:len(self.expected["hash_block_lines"])]]
        self.assertEqual(got, self.expected["hash_block_lines"])


if __name__ == "__main__":
    unittest.main()
