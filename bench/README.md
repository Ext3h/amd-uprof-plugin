# Maintainer benchmarks

Timing harness for the skill's commands, the reproducible baseline asked for in #19. This
folder is outside the plugin package (`plugin/`): it is not installed with the skill.

| File | Role |
|---|---|
| [`bench_timing.py`](bench_timing.py) | times `profile`, `translate` and `collect`; writes a JSON document and a Markdown summary |

`tests/test_bench_timing.py` checks the harness's own logic in CI, with one end-to-end
`profile-process` run on a committed defect-lab capture.

## What it measures

Needs what the tools need: Python 3.9 or newer with the `duckdb` 2.x package, plus LLVM for the
symbolizing scopes and AMD uProf for `translate`, `collect` and a missing IBS dump. It runs the
tools in-process from `plugin/skills/amd-uprof/tools` (`--tools-dir` picks another copy).

Each case runs in two modes:

- plain: no patches, no profiler. Wall time over `--reps` runs (median, p95 by nearest rank,
  min, max), the number a budget is set from.
- instrumented: DuckDB's JSON profiler per statement, a probe on every native child the tools
  start (`uprof_native.run` and `run_watched`), and cProfile over the Python frames, mapped to
  named stages. `--instrumented-reps` runs; their wall time is inflated by the instrumentation
  and is not a budget number.

Cases: `profile-process`, `profile-module`, `profile-function`, `profile-line`,
`profile-region`, `profile-instruction`, `translate`, `translate-ascii` and `collect`
(`--seconds` capture). The default is every `profile` scope.

Each profile case runs once per session-cache mode (`--session-cache`, default `cold,warm`): cold
gives every run an empty session cache, warm shares one that a warmup fills. Results name them
`profile-function/cold` and `profile-function/warm`. Warmups (`--warmup`, default 1) are untimed
runs before the timed ones; the first one generates a missing IBS dump, and its wall time is
reported as the cold dump generation.

Profile outputs must be byte-identical across plain runs and between plain and instrumented
runs; a mismatch is reported, not hidden. `translate` and `collect` record output hashes only,
because their output carries paths and timestamps.

## Isolation

For the whole run, the session cache, the dump cache, the user cache directory and every temp
directory of the tools and their children point below the harness's work directory, so a
measurement never reads results an earlier run left in the caller's caches and leaves none
behind. The symbol search path (`_NT_SYMBOL_PATH`, `DEBUGINFOD_URLS`) stays as configured: symbol
search is part of what a user's run costs. The results record which of those variables were set.

## Running it

```console
python bench/bench_timing.py --session <SESSION> --out <OUTSIDE_REPO>/baseline.json
python bench/bench_timing.py --session <SESSION> --out <OUTSIDE_REPO>/baseline.json --cases profile-function --reps 3 --keep-work <NEW_DIR>
```

`--out` must be outside the repository: measurement snapshots go stale with the next change, so
they are posted to the issue or pull request record (for the baseline, #19), never committed.
The Markdown summary is written beside the JSON and printed. `--keep-work <NEW_DIR>` keeps every
run's console output (`*.stdout`, `*.stderr`), outputs and caches; read them before trusting a
number. `--keep-prof` also keeps the DuckDB profiles and cProfile dumps beside `--out`.
`--max-total-minutes` stops before the next case once the run is that long, and a plain run
longer than `--per-rep-stop-seconds` cuts its case to three runs.

The exit status is 0 when every plain run of every case exited 0, else 2. Run with `--help` for
all arguments.
