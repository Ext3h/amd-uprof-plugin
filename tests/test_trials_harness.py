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
from typing import ClassVar

import debug_fixtures as F

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "trials"))
import run_trials
from runners import TrialResult, TrialSpec, claude_code


def spec(root: Path, **overrides) -> TrialSpec:
    values = {
        "trial_id": "SCN-02",
        "prompt": "Where did the time go?",
        "model": "small-model",
        "plugin_dir": root / "plugin",
        "capture_dir": root / "captures" / "SCN-02",
        "cwd": root / "trials" / "SCN-02",
        "out_dir": root / "out" / "SCN-02",
        "read_roots": (root / "trials" / "SCN-02", root / "plugin", root / "captures" / "SCN-02"),
        "deny_roots": (root / "repo",),
        "timeout_s": 60,
    }
    values.update(overrides)
    return TrialSpec(**values)


class ClaudeCodeAdapterTest(unittest.TestCase):
    def test_rule_paths_are_absolute_rule_form(self):
        self.assertEqual(claude_code.rule_path(PureWindowsPath(r"C:\runs\a b")), "//c/runs/a b")
        self.assertEqual(claude_code.rule_path(Path("/home/u/run")), "//home/u/run")

    def test_reads_are_limited_to_the_read_roots(self):
        root = Path(tempfile.gettempdir()) / "trial-root"
        settings = claude_code.settings(spec(root))["permissions"]
        reads = [rule for rule in settings["allow"] if rule.startswith(("Read(", "Grep(", "Glob("))]
        self.assertEqual(len(reads), 9)
        for read_root in spec(root).read_roots:
            self.assertIn(f"Read({claude_code.rule_path(read_root)}/**)", reads)
        writes = [rule for rule in settings["allow"] if rule.startswith(("Edit(", "Write("))]
        self.assertEqual(
            writes,
            [f"{tool}({claude_code.rule_path(spec(root).cwd)}/**)" for tool in ("Edit", "Write")],
        )
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
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "m",
                    "cwd": "c",
                    "permissionMode": "dontAsk",
                    "mcp_servers": [],
                    "plugins": [{"name": "amd-uprof", "path": "p"}],
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "parent_tool_use_id": None,
                    "message": {
                        "content": [
                            {"type": "text", "text": "Profiling."},
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Bash",
                                "input": {"command": "python uprof.py profile"},
                            },
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "is_error": False,
                                "content": [{"type": "text", "text": "| module |"}],
                            }
                        ]
                    },
                }
            ),
            "not json",
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 3,
                    "result": "The driver.",
                    "total_cost_usd": 0.01,
                    "permission_denials": [{"tool_name": "Read"}],
                }
            ),
        ]
        events, evidence = claude_code.convert(lines)
        self.assertEqual(
            [e["type"] for e in events], ["assistant_text", "tool_call", "tool_result", "final"]
        )
        self.assertEqual(events[1]["input"], {"command": "python uprof.py profile"})
        self.assertEqual(events[2]["output"], "| module |")
        self.assertEqual(events[3]["answer"], "The driver.")
        self.assertEqual(evidence["plugins"], ["amd-uprof p"])
        self.assertEqual(evidence["permission_denials"], ["Read"])

    def test_the_final_turn_context_counts_input_and_cache_tokens(self):
        def turn(usage, parent=None):
            return json.dumps(
                {
                    "type": "assistant",
                    "parent_tool_use_id": parent,
                    "message": {"content": [], "usage": usage},
                }
            )

        lines = [
            turn(
                {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": 3000,
                    "output_tokens": 50,
                }
            ),
            turn(
                {
                    "input_tokens": 7,
                    "cache_read_input_tokens": 12000,
                    "cache_creation_input_tokens": 800,
                    "output_tokens": 90,
                }
            ),
            # A subagent's turn has its own context; the final turn is the main agent's.
            turn({"input_tokens": 1, "cache_read_input_tokens": 99999}, parent="t1"),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "x",
                    "usage": {"input_tokens": 12, "cache_read_input_tokens": 21000},
                }
            ),
        ]
        self.assertEqual(claude_code.final_context_tokens(lines), 7 + 12000 + 800)

    def test_a_transcript_without_usage_leaves_the_context_unmeasured(self):
        lines = [json.dumps({"type": "assistant", "message": {"content": []}})]
        self.assertIsNone(claude_code.final_context_tokens(lines))
        self.assertIsNone(TrialResult([], 0, 1.0).context_tokens)


class FakeRunner:
    name = "fake"
    specs: ClassVar[list] = []
    context: ClassVar[dict] = {}

    def run(self, trial: TrialSpec) -> TrialResult:
        FakeRunner.specs.append(trial)
        events = [
            {
                "type": "tool_call",
                "id": "1",
                "tool": "Bash",
                "input": {"command": f"python uprof.py profile --session {trial.capture_dir}"},
            },
            {"type": "tool_result", "id": "1", "is_error": False, "output": "ok"},
            {"type": "final", "answer": "answer"},
        ]
        return TrialResult(
            events,
            0,
            1.0,
            {"isolated": True},
            context_tokens=FakeRunner.context.get(trial.trial_id),
        )


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class DriverTest(unittest.TestCase):
    def test_a_run_writes_transcripts_and_a_run_log(self):
        FakeRunner.specs = []
        with tempfile.TemporaryDirectory(prefix="uprof-trial-run-") as directory:
            run_dir = Path(directory) / "run"
            code = run_trials.main(
                [
                    "--run-dir",
                    str(run_dir),
                    "--runner",
                    "fake",
                    "--model",
                    "small-model",
                    "--plugin-dir",
                    str(REPO / "plugin"),
                    "--parallel",
                    "1",
                    "--scenario",
                    "SCN-06",
                    "--scenario",
                    "SCN-08",
                ],
                registry={"fake": FakeRunner},
            )
            self.assertEqual(code, 0)
            log = (run_dir / "runlog.md").read_text(encoding="utf-8")
            transcript = (run_dir / "out" / "SCN-08" / "transcript.jsonl").read_text(
                encoding="utf-8"
            )
            meta = json.loads(
                (run_dir / "out" / "SCN-08" / "meta.json").read_text(encoding="utf-8")
            )
            trial = FakeRunner.specs[0]
            before_exists = (trial.capture_dir / "before" / "cpu.db").is_file()

        self.assertEqual([t.trial_id for t in FakeRunner.specs], ["SCN-08"])
        self.assertIn("| SCN-08 | 0 |", log)
        self.assertIn("| SCN-06 | none", log)
        self.assertEqual(len(transcript.splitlines()), 3)
        self.assertEqual(meta["tool_calls"], 1)
        self.assertEqual(meta["outside_references"], [])
        self.assertTrue(before_exists)
        self.assertIn(str(trial.capture_dir / "after"), trial.prompt)
        self.assertNotRegex(trial.prompt, r"<[A-Z_]+>")
        self.assertIn(REPO, trial.deny_roots)
        self.assertEqual(trial.read_roots, (trial.cwd, trial.plugin_dir, trial.capture_dir))

    def test_the_context_limit_is_the_catalogues(self):
        self.assertEqual(run_trials.context_limit(), 50_000)

    def test_the_limit_is_read_from_the_pass_criteria_only(self):
        text = (
            "## SCN-01: x\n- **Answer:** it used fewer than 9 tokens\n\n## Pass criteria\n\n"
            "- **Context:** fewer than 50,000 tokens of total context\n\n## Sessions\n"
        )
        self.assertEqual(run_trials.context_limit(text), 50_000)
        with self.assertRaises(ValueError):
            run_trials.context_limit("## SCN-01: x\n- fewer than 9 tokens\n")

    def test_the_observer_treats_the_limit_as_a_defect_ceiling(self):
        # The limit marks a defective run; the goal is the context the minimal calls need.
        rubric = (REPO / "trials" / "observer.md").read_text(encoding="utf-8")
        deliver = " ".join(
            rubric.partition("## What the skill must deliver")[2].partition("\n## ")[0].split()
        )
        self.assertIn("as little context as the minimal calls need", deliver)
        self.assertIn("a defect ceiling, not a target", deliver)
        per_trial = rubric.partition("## Per trial")[2].partition("\n## ")[0]
        self.assertRegex(per_trial, r"\| Context growth \|.*same causes as extra calls")

    def test_the_observer_counts_an_unfiltered_function_call_as_extra(self):
        rubric = " ".join((REPO / "trials" / "observer.md").read_text(encoding="utf-8").split())
        self.assertIn("unfiltered `--by function` call before the `--pid`/`--image` filter", rubric)
        self.assertIn("cause (b) while", rubric)
        self.assertIn("cause (c) once", rubric)

    def test_a_run_records_each_scenarios_final_turn_context(self):
        FakeRunner.specs = []
        FakeRunner.context = {"SCN-02": 41_250, "SCN-03": 50_000}
        try:
            with tempfile.TemporaryDirectory(prefix="uprof-trial-run-") as directory:
                run_dir = Path(directory) / "run"
                run_trials.main(
                    [
                        "--run-dir",
                        str(run_dir),
                        "--runner",
                        "fake",
                        "--model",
                        "small-model",
                        "--plugin-dir",
                        str(REPO / "plugin"),
                        "--parallel",
                        "1",
                        "--scenario",
                        "SCN-02",
                        "--scenario",
                        "SCN-03",
                        "--scenario",
                        "SCN-08",
                    ],
                    registry={"fake": FakeRunner},
                )
                log = (run_dir / "runlog.md").read_text(encoding="utf-8")
                metas = {
                    scn: json.loads(
                        (run_dir / "out" / scn / "meta.json").read_text(encoding="utf-8")
                    )
                    for scn in ("SCN-02", "SCN-03", "SCN-08")
                }
        finally:
            FakeRunner.context = {}

        self.assertEqual(
            (metas["SCN-02"]["context_tokens"], metas["SCN-02"]["context"]), (41_250, "below limit")
        )
        self.assertEqual(
            (metas["SCN-03"]["context_tokens"], metas["SCN-03"]["context"]), (50_000, "over limit")
        )
        self.assertEqual(
            (metas["SCN-08"]["context_tokens"], metas["SCN-08"]["context"]), (None, "not measured")
        )
        self.assertIn("final-turn context", log)
        self.assertRegex(log, r"\| SCN-02 \|.*\| 41250 below limit \|")
        self.assertRegex(log, r"\| SCN-03 \|.*\| 50000 over limit \|")
        self.assertRegex(log, r"\| SCN-08 \|.*\| not measured \|")

    def test_a_run_folder_inside_the_repository_is_refused(self):
        with self.assertRaises(SystemExit):
            run_trials.main(
                ["--run-dir", str(REPO / "trial-run"), "--runner", "fake", "--model", "m"],
                registry={"fake": FakeRunner},
            )


def git(*args: str) -> bytes:
    import subprocess

    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True).stdout


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class CommittedCaptureTest(unittest.TestCase):
    """A Session may name a committed reference capture: defect_lab/<class>.<mode>.

    The trial gets a copy taken from a committed revision, never from the working tree, inside
    its own capture root, because the agent may read only its run folder.
    """

    QUESTION = "Here is a capture: `<CAPTURE>`; the images are in `<IMAGE_DIR>`."

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-trial-committed-")
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name)
        self.head = git("rev-parse", "HEAD").decode().strip()
        platforms = git("ls-tree", "--name-only", "HEAD", f"{run_trials.CAPTURES}/").decode()
        self.platform = platforms.split()[0].rsplit("/", 1)[-1]
        self.folder = f"{run_trials.CAPTURES}/{self.platform}"

    def prepare(self, session: str, rev: str = "HEAD", question: str = QUESTION):
        fields = {"Session": session, "Question": f'"{question}"'}
        return run_trials.prepare(self.run_dir, "SCN-99", fields, rev)

    def test_a_committed_capture_is_copied_from_the_revision_with_its_images(self):
        prompt, root, sources = self.prepare("`defect_lab/lock.timer`")
        capture, images = root / "capture", root / "images"
        for name in git("ls-tree", "--name-only", "HEAD", f"{self.folder}/lock.timer/").split():
            path = name.decode()
            with self.subTest(file=path):
                copied = capture / path.rsplit("/", 1)[-1]
                self.assertEqual(copied.read_bytes(), git("show", f"HEAD:{path}"))
        self.assertEqual(
            sorted(path.name for path in images.iterdir()),
            sorted(
                f"{stem}.{ext}"
                for stem, image in (
                    ("defect_lab", "exe"),
                    ("lab_core", "dll"),
                    ("lab_vendor", "dll"),
                )
                for ext in (image, "pdb")
            ),
        )
        self.assertIn(str(capture), prompt)
        self.assertIn(str(images), prompt)
        self.assertNotIn(str(REPO), prompt)
        self.assertEqual(sources, [f"{self.head}:{self.folder}/lock.timer"])

    def test_built_and_committed_sessions_mix(self):
        _, root, sources = self.prepare(
            "`attached`, `defect_lab/lock.timer`", question="Before `<BEFORE>`, after `<AFTER>`."
        )
        self.assertTrue((root / "before" / "cpu.db").is_file())
        self.assertTrue((root / "after" / "cpu.db").is_file())
        self.assertEqual(sources, [f"{self.head}:{self.folder}/lock.timer"])

    def test_an_explicit_platform_selects_its_folder(self):
        _, root, _ = self.prepare(f"`defect_lab/{self.platform}/wait.timer`")
        self.assertTrue((root / "capture" / "cpu.db").is_file())

    def test_unknown_captures_and_platforms_are_refused_before_anything_runs(self):
        for session, message in (
            ("`defect_lab/lock.nomode`", "no committed capture defect_lab/lock.nomode"),
            ("`defect_lab/no-such-platform/lock.timer`", "no platform no-such-platform"),
        ):
            with self.subTest(session=session):
                with self.assertRaises(ValueError) as caught:
                    self.prepare(session)
                self.assertIn(message, str(caught.exception))

    def test_a_revision_without_captures_is_refused(self):
        added = git("log", "--diff-filter=A", "--format=%H", "-1", "--", f"{run_trials.CAPTURES}/")
        before = added.decode().strip() + "~1"
        with self.assertRaises(ValueError) as caught:
            self.prepare("`defect_lab/lock.timer`", before)
        self.assertIn("no platform folder", str(caught.exception))

    def test_several_platforms_need_an_explicit_name(self):
        with self.assertRaises(ValueError) as caught:
            run_trials.choose_platform(["linux-x64", "windows-x64"], "lock.timer", "HEAD")
        self.assertIn("linux-x64, windows-x64", str(caught.exception))
        self.assertEqual(
            run_trials.choose_platform(["windows-x64"], "lock.timer", "HEAD"), "windows-x64"
        )

    def test_captures_come_from_the_plugin_revision_or_head(self):
        self.assertEqual(run_trials.capture_revision("HEAD", None), self.head)
        self.assertEqual(
            run_trials.capture_revision("HEAD~1", None), git("rev-parse", "HEAD~1").decode().strip()
        )
        # A plugin folder is not a revision; the captures still come from a commit.
        self.assertEqual(run_trials.capture_revision("HEAD~1", REPO / "plugin"), self.head)

    def run_main(self, catalogue: dict[str, dict[str, str]]):
        from unittest import mock

        FakeRunner.specs = []
        run_dir = self.run_dir / "run"
        with mock.patch.object(run_trials, "catalogue", return_value=catalogue):
            code = run_trials.main(
                [
                    "--run-dir",
                    str(run_dir),
                    "--runner",
                    "fake",
                    "--model",
                    "small-model",
                    "--plugin-dir",
                    str(REPO / "plugin"),
                    "--parallel",
                    "1",
                ],
                registry={"fake": FakeRunner},
            )
        return code, run_dir

    def test_a_bad_committed_name_stops_the_run_before_any_trial_starts(self):
        # Trials run in parallel, so a name checked only when its trial starts would let the
        # others spend model calls first.
        catalogue = {
            "SCN-97": {"Session": "`attached`", "Question": '"`<CAPTURE>`"'},
            "SCN-98": {"Session": "`defect_lab/lock.nomode`", "Question": '"`<CAPTURE>`"'},
        }
        with self.assertRaises(SystemExit) as caught:
            self.run_main(catalogue)
        self.assertIn("SCN-98: no committed capture defect_lab/lock.nomode", str(caught.exception))
        self.assertEqual(FakeRunner.specs, [])
        self.assertFalse((self.run_dir / "run").exists())

    def test_a_trial_records_where_its_committed_capture_came_from(self):
        catalogue = {
            "SCN-97": {"Session": "`defect_lab/wait.timer`", "Question": '"`<CAPTURE>`"'},
        }
        code, run_dir = self.run_main(catalogue)
        self.assertEqual(code, 0)
        meta = json.loads((run_dir / "out" / "SCN-97" / "meta.json").read_text(encoding="utf-8"))
        # With a plugin folder the captures come from HEAD.
        self.assertEqual(meta["capture_source"], [f"{self.head}:{self.folder}/wait.timer"])
        self.assertIn(f"from commit {self.head}", (run_dir / "runlog.md").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
