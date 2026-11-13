# Scenario: timer capture, own hash loop versus OS heap (heap_churn)

Recorded with skill 2.1.0 after #190 (timer captures) and #116 (first call chosen from the
question). `tests/test_examples.py` replays the calls below against the current tools and
compares the numbers with `expected.json`.

## Input

The committed timer capture in `tests/fixtures/timer/` (see its README for the build and capture
commands): `heap_churn.exe` launched under `AMDuProfCLI collect --config tbp` (timer 1 ms,
unelevated, attached to the launched process) on Windows 11, Intel i7-10750H, AMDuProfCLI
5.3.521. The workload hashes 64-176 byte blocks in its own code and allocates and frees every
block with `HeapAlloc`/`HeapFree`. The capture is 1446 samples on one thread; the session is
rebuilt from `cpu_tables.json`:

```console
python -c "import sys; sys.path.insert(0, 'tests'); import timer_fixture, pathlib; timer_fixture.build(pathlib.Path('<SESSION_DIR>'))"
```

The image and its PDB are in `tests/fixtures/timer/`; the session's `Module.path` is the
placeholder `c:\fixture\heap_churn.exe`, so a call that needs the image (source lines) passes
that directory with `--image-path`.

## Prompt

> Using the amd-uprof skill, profile the capture in `<SESSION_DIR>` (`heap_churn.exe`, image and
> PDB in `<FIXTURE_DIR>`). Where does the CPU time go? Name the offending function and source
> lines in the program's own code, the share spent outside it (OS, runtime) and whether the
> program can change that share, with the evidence and a cost estimate and its basis.

## Minimal path: 3 tool calls

Counted as `profile` calls; loading the skill, the write-up and a subagent's hand-back are not
counted.

1. `python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by module`
2. `python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by function`
3. `python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by line --function "static unsigned int hash_block(const unsigned char *, unsigned int)" --module c:/fixture/heap_churn.exe --image-path <FIXTURE_DIR>`

Step 1 is the first call SKILL.md names for "where CPU time went, mine versus the rest"; the
capture is attached, so there is no process ranking. Step 2 is step 1's `NEXT` call as printed;
its rows are already named from the capture's database. Step 3 is asked for by the prompt
(source lines): the function view prints no `NEXT`, so the call comes from the SKILL.md
template, with `--function`/`--module` taken from the function row and `--image-path` from the
prompt. No `--by process`, `--by region`, `--by instruction`, `--json` re-run, `tools` or SQL is
needed.

## Expected answer

- **Mode and basis.** Timer sampling, 1 ms: `sample_share` is the on-CPU time share of the
  sampled thread. There is no `time_share`, cycle cost or `cpu_seconds` for a timer capture
  (METRIC; the `cpu_seconds` cause gives the measured undercount). One thread, 1446 samples.
- **Own code: 72.8% ±2.3 points** (`heap_churn.exe`, 1052 samples, `isSystemModule=false`).
  - `hash_block` is the offending function: 826 samples, **57.1% ±2.6 points** of the capture.
    Its hot lines in `heap_churn.c` are **25** (`h ^= p[i];`), **26** (`h *= 16777619u;`) and
    **24** (the loop header), together all of its 826 samples, most on line 25. The sample-hot
    address is `HOT_RVA` 0x129E (167 samples) inside it. Source reading, not measured: this is
    the byte-at-a-time FNV loop, a serial multiply chain per byte.
  - `fill_block` 211 samples (14.6%); `heap_churn_entry` 15 samples (1.0%).
- **Outside the program: 27.2% ±2.3 points** (394 samples): `ntdll.dll` 393 samples
  (`isSystemModule=true`) plus one `apphelp.dll` sample. Almost all of it is heap management:
  `RtlpLowFragHeapAllocFromContext` 206, `RtlFreeHeap` 99, `RtlpAllocateNTHeapInternal` 55 and
  `RtlAllocateHeap` 28, together 388 samples (26.8%). The program can change it:
  `heap_churn_entry` allocates and frees one block per iteration (`heap_churn.c` lines 38 and
  41). The link from those calls to the ntdll functions is by name and source, not measured: a
  timer capture carries no caller attribution into external modules (METRIC).
- **Cost estimate.** In CPU-time shares of this run: the hash loop 57.1% ±2.6 points, the heap
  calls 26.8% ±2.3 points. Estimate, not measured: reusing one buffer would remove most of the
  heap share. In seconds: multiply a share by the workload's measured CPU time (the process's
  user + kernel time, not its wall time); the tool derives no seconds from timer ticks.

## Reference metrics

- Tool calls: 3 (above), all essential.
- Wall time and model: not recorded yet; record them with the first trial run against this
  scenario.
