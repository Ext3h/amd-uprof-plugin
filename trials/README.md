# Trials

Checks that the skill's guidance and tool output lead an agent on a small, fast model along the
minimal call sequence to an actionable answer. A trial gives a fresh agent only the user's
question, a capture and the installed plugin, then compares its calls and answer with the
catalogue. This folder is outside the plugin package (`plugin/`): it is not installed with the
skill.

| File | Role |
|---|---|
| [`scenarios.md`](scenarios.md) | the catalogue: per scenario the question, the session, the correct answer and the minimal calls |
| [`build_captures.py`](build_captures.py) | writes the synthetic sessions the scenarios run on |
| [`run_trials.py`](run_trials.py) | runs the scenarios, each as its own isolated agent process, and writes transcripts and a run log |
| [`runners/`](runners/__init__.py) | the runner interface and its adapters; one ships, for the Claude Code CLI |
| [`observer.md`](observer.md) | the rubric for judging a run's transcripts, separately from the run |

`tests/test_trials.py` runs in CI: every catalogue call parses with the current CLI, every
session it names is one the builder writes, and every image a built session records passes the
tools' build-identity checks. `tests/test_trials_harness.py` checks the harness without a model.
A trial run itself needs a model API and is never part of CI.

## Building the captures

Needs what the tools need: Python 3.9 or newer with the `duckdb` 2.x package.

```console
python trials/build_captures.py <OUT_DIR>
python trials/build_captures.py <OUT_DIR> attached pdb_far
```

`<OUT_DIR>` must not exist. It receives one directory per session and `bins/` with the images
those sessions record. The images are copies of committed test fixtures, and the PE CheckSum and
SizeOfImage recorded in `cpu.db` are read from those copies, so a rebuilt fixture never leaves a
stale identity behind. The other modules (`amdvlk64.dll`, `ntdll.dll`, `node.exe`) are the
repository's own `tests/fixtures/fixture.exe` under those names, never a copied vendor or OS
binary.

Module paths in a session are the absolute paths of the images under `<OUT_DIR>`, so build the
captures on the host that analyses them and do not move the folder afterwards.

## Running the trials

```console
python trials/run_trials.py --run-dir <NEW_DIR> --runner claude-code --model <MODEL> --path-prefix <DIR_WITH_PYTHON_AND_DUCKDB> --path-prefix <LLVM_BIN>
```

Pick `<MODEL>` from the class the skill is optimized for: a small, fast model. `<NEW_DIR>` must
be outside the repository. Per scenario with a session, the harness:

- copies `plugin/` from `--plugin-rev` (default `HEAD`) once, or uses `--plugin-dir`;
- builds the scenario's own capture copy, so no session-cache entry is shared between trials,
  and names the sessions `capture` (`before`/`after` for two), not after the scenario;
- starts one agent process in a fresh empty folder with only the question, the real paths filled
  in; reads are limited to that folder, the plugin copy and the capture; the repository, the
  output folder and the other trials are denied; temp variables point into the trial folder;
- kills only the trial's own process tree after `--timeout` seconds; runs `--parallel` trials;
- writes `out/<SCN>/transcript.jsonl` (neutral schema), `meta.json`, the runner's raw output, and
  `runlog.md` with exit status, wall time, tool calls, tool errors, references to denied paths,
  the final-turn context against the limit in the catalogue's pass criteria (`not measured` when
  the runner reports no usage) and the isolation facts the runner reported. It does not judge
  answers: apply `observer.md`.

Scenarios without a session are listed as skipped with their reason. A known limit of the
Claude Code adapter: shell commands are allowed, so a write outside the trial folder (for example
to `/tmp`) or a shell read of a denied path through another spelling (a relative path or an
environment variable) cannot be blocked without the CLI's sandbox. The run log shows any tool
input naming a denied path in its usual spelling, so "none" there is not proof of isolation.

### Adding a runner

A runner adapts another agent harness or model API. Write a module in `runners/` with a class
that has a `name` and `run(spec: TrialSpec) -> TrialResult`, takes its options as keyword
strings (`--runner-option key=value`), and add it to `runners()` in `runners/__init__.py`. It must
start the agent with only the plugin in `spec.plugin_dir`, limit reads to `spec.read_roots`, deny
`spec.deny_roots`, pass `spec.model`, and return the transcript in the neutral schema documented
there, with `context_tokens` set from the harness's usage report of the final model turn (or
left `None`); `run_process` and `trial_env` handle the timeout and the environment.

## Running a scenario by hand

Build the session named in the scenario, replace the placeholders in its question with the real
paths (`<CAPTURE>` is `<OUT_DIR>/<session>`; for SCN-04 `<PDB_DIR>` is `<OUT_DIR>/bins/pdbs`;
for SCN-18 `<IMAGE_DIR>` is `<OUT_DIR>/bins/timer`), and give the question to an agent that has
only the plugin installed. Compare its `uprof.py` calls with the scenario's minimal calls and its
answer with the scenario's answer.
