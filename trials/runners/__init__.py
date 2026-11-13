"""The runner interface: one isolated agent run per trial, reported as a neutral transcript.

A runner adapts one agent harness (a CLI or an API loop) to the trials. It receives a TrialSpec
and returns a TrialResult whose events use the neutral schema below, so the run log and the
observer never depend on one harness's own format. A runner may keep its harness's raw output in
spec.out_dir beside the neutral transcript.

Neutral transcript, one JSON object per line, in order:
    {"type": "assistant_text", "text": str}
    {"type": "tool_call", "id": str, "tool": str, "input": object}
    {"type": "tool_result", "id": str, "is_error": bool, "output": str}
    {"type": "final", "answer": str}

Adding a runner: write a module in this package with a class that has a `name` and
`run(spec) -> TrialResult`, accepts its options as keyword strings, and add it to runners().
Set TrialResult.context_tokens from the harness's usage report of the final model turn; leave it
None when the harness reports no usage, and the run log says "not measured".
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TrialSpec:
    """Everything a runner needs for one trial; the paths exist when run() is called."""

    trial_id: str
    prompt: str
    model: str
    plugin_dir: Path
    capture_dir: Path
    cwd: Path  # fresh and empty except cwd/_temp
    out_dir: Path  # the runner's own files (raw output, settings) go here
    read_roots: tuple[Path, ...]  # the only places the agent may read: cwd, plugin, capture
    deny_roots: tuple[Path, ...]  # places the agent must not reach, e.g. the repository
    timeout_s: int
    path_prefix: tuple[str, ...] = ()  # directories put first on PATH (Python, LLVM)


@dataclass
class TrialResult:
    events: list[dict]
    exit_status: int | str  # the process exit code, or "timeout"
    wall_s: float
    evidence: dict[str, object] = field(default_factory=dict)  # isolation facts, model, cost
    # The model's input at its final turn, harness system prompt and cached tokens included;
    # None when the harness reports no usage.
    context_tokens: int | None = None


class Runner(Protocol):
    name: str

    def run(self, spec: TrialSpec) -> TrialResult: ...


def trial_env(
    spec: TrialSpec, drop_prefixes: tuple[str, ...] = (), drop: tuple[str, ...] = ()
) -> dict[str, str]:
    """The host environment minus the named variables, temp in the trial folder, PATH prefixed."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in drop and not key.startswith(drop_prefixes)
    }
    temp = spec.cwd / "_temp"
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(temp)
    if spec.path_prefix:
        env["PATH"] = os.pathsep.join([*spec.path_prefix, env.get("PATH", "")])
    return env


def run_process(
    command: list[str], cwd: Path, env: dict[str, str], stdout: Path, stderr: Path, timeout_s: int
) -> tuple[int | str, float]:
    """Run command with no stdin; kill only its own process tree on timeout."""
    started = time.monotonic()
    options: dict[str, object] = {}
    if os.name != "nt":
        options["start_new_session"] = True
    with open(stdout, "wb") as out, open(stderr, "wb") as err:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            **options,
        )
        try:
            status: int | str = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            status = "timeout"
    return status, round(time.monotonic() - started, 1)


def runners() -> dict[str, type]:
    from runners.claude_code import ClaudeCodeRunner

    return {ClaudeCodeRunner.name: ClaudeCodeRunner}
