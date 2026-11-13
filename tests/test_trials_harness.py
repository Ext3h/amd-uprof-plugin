"""The trial harness builds isolated runs and neutral transcripts without calling a model.

A real trial needs a model API, so CI covers what the harness does around it: the Claude Code
adapter's permission settings and command line, its conversion of stream-json to the neutral
transcript, and a full driver run with a fake runner.

Run from the repository root: python tests/run_tests.py test_trials_harness
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath

import debug_fixtures as F

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "trials"))
import run_trials  # noqa: E402
from runners import TrialResult, TrialSpec  # noqa: E402
from runners import claude_code  # noqa: E402


def spec(root: Path, **overrides) -> TrialSpec:
    values = dict(trial_id="SCN-02", prompt="Where did the time go?", model="small-model",
                  plugin_dir=root / "plugin", capture_dir=root / "captures" / "SCN-02",
                  cwd=root / "trials" / "SCN-02", out_dir=root / "out" / "SCN-02",
                  read_roots=(root / "trials" / "SCN-02", root / "plugin",
                              root / "captures" / "SCN-02"),
                  deny_roots=(root / "repo",), timeout_s=60)
    values.update(overrides)
    return TrialSpec(**values)


class ClaudeCodeAdapterTest(unittest.TestCase):
    def test_rule_paths_are_absolute_rule_form(self):
        self.assertEqual(claude_code.rule_path(PureWindowsPath(r"C:\runs\a b")), "//c/runs/a b")
        self.assertEqual(claude_code.rule_path(Path("/home/u/run")), "//home/u/run")

    def test_reads_are_limited_to_the_read_roots(self):
        root = Path(tempfile.gettempdir()) / "trial-root"
        settings = claude_code.settings(spec(root))["permissions"]
        reads = [rule for rule in settings["allow"] if rule.startswith(("Read(", "Grep(",
                                                                         "Glob("))]
        self.assertEqual(len(reads), 9)
        for read_root in spec(root).read_roots:
            self.assertIn(f"Read({claude_code.rule_path(read_root)}/**)", reads)
        writes = [rule for rule in settings["allow"] if rule.startswith(("Edit(", "Write("))]
        self.assertEqual(writes, [f"{tool}({claude_code.rule_path(spec(root).cwd)}/**)"
                                  for tool in ("Edit", "Write")])
        self.assertIn(f"Read({claude_code.rule_path(root / 'repo')}/**)", settings["deny"])
        self.assertTrue({"Agent", "WebFetch", "WebSearch"} <= set(settings["deny"]))
        self.assertEqual(settings["defaultMode"], "dontAsk")

    def test_the_command_passes_the_model_and_loads_no_settings_sources(self):
        root = Path(tempfile.gettempdir()) / "trial-root"
        command = claude_code.command("claude", spec(root), root / "settings.json")
        self.assertEqual(command[command.index("--model") + 1], "small-model")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertEqual(command[command.index("--plugin-dir") + 1], str(root / "plugin"))
        for flag in ("--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, command)

    def test_stream_json_becomes_the_neutral_transcript(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init", "model": "m", "cwd": "c",
                        "permissionMode": "dontAsk", "mcp_servers": [],
                        "plugins": [{"name": "amd-uprof", "path": "p"}]}),
            json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {
                "content": [{"type": "text", "text": "Profiling."},
                            {"type": "tool_use", "id": "t1", "name": "Bash",
                             "input": {"command": "python uprof.py profile"}}]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": False,
                 "content": [{"type": "text", "text": "| module |"}]}]}}),
            "not json",
            json.dumps({"type": "result", "subtype": "success", "is_error": False,
                        "num_turns": 3, "result": "The driver.", "total_cost_usd": 0.01,
                        "permission_denials": [{"tool_name": "Read"}]}),
        ]
        events, evidence = claude_code.convert(lines)
        self.assertEqual([e["type"] for e in events],
                         ["assistant_text", "tool_call", "tool_result", "final"])
        self.assertEqual(events[1]["input"], {"command": "python uprof.py profile"})
        self.assertEqual(events[2]["output"], "| module |")
        self.assertEqual(events[3]["answer"], "The driver.")
        self.assertEqual(evidence["plugins"], ["amd-uprof p"])
        self.assertEqual(evidence["permission_denials"], ["Read"])

    def test_the_final_turn_context_counts_input_and_cache_tokens(self):
        def turn(usage, parent=None):
            return json.dumps({"type": "assistant", "parent_tool_use_id": parent,
                               "message": {"content": [], "usage": usage}})

        lines = [
            turn({"input_tokens": 5, "cache_read_input_tokens": 9000,
                  "cache_creation_input_tokens": 3000, "output_tokens": 50}),
            turn({"input_tokens": 7, "cache_read_input_tokens": 12000,
                  "cache_creation_input_tokens": 800, "output_tokens": 90}),
            # A subagent's turn has its own context; the final turn is the main agent's.
            turn({"input_tokens": 1, "cache_read_input_tokens": 99999}, parent="t1"),
            json.dumps({"type": "result", "subtype": "success", "result": "x",
                        "usage": {"input_tokens": 12, "cache_read_input_tokens": 21000}}),
        ]
        self.assertEqual(claude_code.final_context_tokens(lines), 7 + 12000 + 800)

    def test_a_transcript_without_usage_leaves_the_context_unmeasured(self):
        lines = [json.dumps({"type": "assistant", "message": {"content": []}})]
        self.assertIsNone(claude_code.final_context_tokens(lines))
        self.assertIsNone(TrialResult([], 0, 1.0).context_tokens)


class FakeRunner:
    name = "fake"
    specs: list = []
    context: dict = {}

    def run(self, trial: TrialSpec) -> TrialResult:
        FakeRunner.specs.append(trial)
        events = [{"type": "tool_call", "id": "1", "tool": "Bash",
                   "input": {"command": f"python uprof.py profile --session {trial.capture_dir}"}},
                  {"type": "tool_result", "id": "1", "is_error": False, "output": "ok"},
                  {"type": "final", "answer": "answer"}]
        return TrialResult(events, 0, 1.0, {"isolated": True},
                           context_tokens=FakeRunner.context.get(trial.trial_id))


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class DriverTest(unittest.TestCase):
    def test_a_run_writes_transcripts_and_a_run_log(self):
        FakeRunner.specs = []
        with tempfile.TemporaryDirectory(prefix="uprof-trial-run-") as directory:
            run_dir = Path(directory) / "run"
            code = run_trials.main(
                ["--run-dir", str(run_dir), "--runner", "fake", "--model", "small-model",
                 "--plugin-dir", str(REPO / "plugin"), "--parallel", "1",
                 "--scenario", "SCN-01", "--scenario", "SCN-08"],
                registry={"fake": FakeRunner})
            self.assertEqual(code, 0)
            log = (run_dir / "runlog.md").read_text(encoding="utf-8")
            transcript = (run_dir / "out" / "SCN-08" / "transcript.jsonl").read_text(
                encoding="utf-8")
            meta = json.loads((run_dir / "out" / "SCN-08" / "meta.json").read_text(
                encoding="utf-8"))
            trial = FakeRunner.specs[0]
            before_exists = (trial.capture_dir / "before" / "cpu.db").is_file()

        self.assertEqual([t.trial_id for t in FakeRunner.specs], ["SCN-08"])
        self.assertIn("| SCN-08 | 0 |", log)
        self.assertIn("| SCN-01 | none", log)
        self.assertEqual(len(transcript.splitlines()), 3)
        self.assertEqual(meta["tool_calls"], 1)
        self.assertEqual(meta["outside_references"], [])
        self.assertTrue(before_exists)
        self.assertIn(str(trial.capture_dir / "after"), trial.prompt)
        self.assertNotRegex(trial.prompt, r"<[A-Z_]+>")
        self.assertIn(REPO, trial.deny_roots)
        self.assertEqual(trial.read_roots, (trial.cwd, trial.plugin_dir, trial.capture_dir))

    def test_the_context_limit_is_the_catalogues(self):
        self.assertEqual(run_trials.context_limit(), 100_000)

    def test_a_run_records_each_scenarios_final_turn_context(self):
        FakeRunner.specs = []
        FakeRunner.context = {"SCN-02": 41_250, "SCN-03": 100_000}
        try:
            with tempfile.TemporaryDirectory(prefix="uprof-trial-run-") as directory:
                run_dir = Path(directory) / "run"
                run_trials.main(
                    ["--run-dir", str(run_dir), "--runner", "fake", "--model", "small-model",
                     "--plugin-dir", str(REPO / "plugin"), "--parallel", "1",
                     "--scenario", "SCN-02", "--scenario", "SCN-03", "--scenario", "SCN-08"],
                    registry={"fake": FakeRunner})
                log = (run_dir / "runlog.md").read_text(encoding="utf-8")
                metas = {scn: json.loads((run_dir / "out" / scn / "meta.json").read_text(
                    encoding="utf-8")) for scn in ("SCN-02", "SCN-03", "SCN-08")}
        finally:
            FakeRunner.context = {}

        self.assertEqual((metas["SCN-02"]["context_tokens"], metas["SCN-02"]["context"]),
                         (41_250, "below limit"))
        self.assertEqual((metas["SCN-03"]["context_tokens"], metas["SCN-03"]["context"]),
                         (100_000, "over limit"))
        self.assertEqual((metas["SCN-08"]["context_tokens"], metas["SCN-08"]["context"]),
                         (None, "not measured"))
        self.assertIn("final-turn context", log)
        self.assertRegex(log, r"\| SCN-02 \|.*\| 41250 below limit \|")
        self.assertRegex(log, r"\| SCN-03 \|.*\| 100000 over limit \|")
        self.assertRegex(log, r"\| SCN-08 \|.*\| not measured \|")

    def test_a_run_folder_inside_the_repository_is_refused(self):
        with self.assertRaises(SystemExit):
            run_trials.main(["--run-dir", str(REPO / "trial-run"), "--runner", "fake",
                             "--model", "m"], registry={"fake": FakeRunner})


if __name__ == "__main__":
    unittest.main()
