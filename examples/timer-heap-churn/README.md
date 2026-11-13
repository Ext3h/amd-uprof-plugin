# Scenario: timer capture, own hash loop versus OS heap (heap_churn)

Recorded with skill 2.1.0 at the commit that adds this folder (after #190 / PR #209).
`tests/test_examples.py` replays the calls below against the current tools and compares the
numbers with `expected.json`.

## Input

The committed timer capture in `tests/fixtures/timer/` (see its README for the build and capture
commands): `heap_churn.exe` launched under `AMDuProfCLI collect --config tbp` (timer 1 ms,
unelevated) on Windows 11, Intel i7-10750H, AMDuProfCLI 5.3.521. The workload hashes 64-176
byte blocks in its own code and allocates and frees every block with `HeapAlloc`/`HeapFree`.
The capture is 1446 samples; the session is rebuilt from `cpu_tables.json`:

```console
python -c "import sys; sys.path.insert(0, 'tests'); import timer_fixture, pathlib; timer_fixture.build(pathlib.Path('<SESSION_DIR>'))"
```

The image and its PDB are in `tests/fixtures/timer/`; the session's `Module.path` is the
placeholder `c:\fixture\heap_churn.exe`, so the symbolized calls pass that directory with
`--image-path`.

## Prompt

> Using the amd-uprof skill, profile the capture in `<SESSION_DIR>` (`heap_churn.exe`, image and
> PDB in `<FIXTURE_DIR>`). Where does the CPU time go? Name the offending function and source
> lines in the program's own code, the share spent outside it (OS, runtime) and whether the
> program can change that share, with the evidence and a cost estimate and its basis.

## Minimal path: 5 tool calls

1. Read `SKILL.md`.
2. `python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by process`
3. `python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by module`
4. `python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by function --image-path <FIXTURE_DIR>`
5. `python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by line --function "static unsigned int hash_block(const unsigned char *, unsigned int)" --module c:/fixture/heap_churn.exe --image-path <FIXTURE_DIR>`

Then the write-up; a subagent adds its hand-back call. Steps 2-4 follow the tools' `NEXT`
chain; step 5 is asked for by the prompt (source lines). No `--by region`, `--by instruction`,
`--json` re-run, `tools` or SQL is needed.

## Expected answer

- **Mode and basis.** Timer sampling, 1 ms: `sample_share` is the on-CPU time share of the
  sampled thread. There is no `time_share`, cycle cost or `cpu_seconds` for a timer capture
  (METRIC; the `cpu_seconds` cause gives the measured undercount). One thread, 1446 samples.
- **Own code: 72.8% ±2.3 points** (`heap_churn.exe`, 1052 samples, `isSystemModule=false`).
  - `hash_block` is the offending function: 826 samples, **57.1%** of the capture (±2.6 points).
    Its lines in `heap_churn.c`: **25** (`h ^= p[i];`) 468 samples (32.4%), **26**
    (`h *= 16777619u;`) 202 (14.0%), **24** (the loop header) 156 (10.8%). That is the
    byte-at-a-time FNV loop: a serial multiply chain per byte. The sample-hot address is
    `HOT_RVA` 0x129E (167 samples) inside it.
  - `fill_block` 211 samples (14.6%); `heap_churn_entry` 15 (1.0%).
- **Outside the program: 27.2%** (`ntdll.dll`, 393 samples, `isSystemModule=true`), almost all
  heap management: `RtlpLowFragHeapAllocFromContext` 206, `RtlFreeHeap` 99,
  `RtlpAllocateNTHeapInternal` 55, `RtlAllocateHeap` 28, together 388 samples (26.8%).
  It is changeable by the program: `heap_churn_entry` allocates and frees one block per
  iteration (`heap_churn.c` lines 38 and 41). Reusing one buffer removes most of that share.
  The link from the program's calls to these ntdll functions is by name and source, not
  measured: a timer capture carries no caller attribution into external modules (METRIC).
- **Cost estimate.** In CPU-time shares of this run: the hash loop 57.1% ±2.6 points, the heap
  calls 26.8%. In seconds: multiply the share by the workload's measured CPU time (the
  process's user + kernel time, not its wall time); the tool derives no seconds from timer
  ticks.

## Reference metrics

- Tool calls: 5 (above), all essential.
- Wall time and model: not recorded yet; record them with the first trial run against this
  scenario.
