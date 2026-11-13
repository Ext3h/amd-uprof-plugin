# Trial scenarios

Typical profiling questions, each with its input, the correct actionable answer and the fewest
tool calls that reach it. A trial gives an agent on a small, fast model only the question and the
installed plugin, then compares its calls and answer with this catalogue.

`tests/test_trials.py` checks the catalogue: every scenario has its fields and every answer part,
every call parses with the current `uprof.py`, every session it names is one `build_captures.py`
writes, and the key shares quoted for SCN-02 and SCN-09 are what the tools print for the built
captures.

## Conventions

- Scenario IDs `SCN-nn` are stable. A dropped scenario's ID is retired, never reused.
  Retired IDs: none.
- **Session**: the `build_captures.py` session the scenario runs on, or `none` with the reason.
  The sessions are synthetic: they exercise the tools' code paths, not real capture statistics.
- **Question**: the prompt, phrased as a user would write it. `<CAPTURE>` is the session
  directory; `<BEFORE>`/`<AFTER>` are two sessions, `<PDB_DIR>` and `<IMAGE_DIR>` the folders
  the user names.
- **Answer**: the five parts of the answer contract below; a part that cannot apply says `n/a`
  and why. A scenario recorded in `examples/` takes that example's expected answer instead. A
  scenario without a session gives the parts' shape; its values need a capture.
- **Minimal calls**: the `uprof.py` calls in order. Loading the skill and the final write-up are
  not counted. `<SKILL_DIR>` is the installed skill; `<OWN_MODULE>` is the module cell the
  previous call printed for the user's own image. A scenario that is not answerable today says
  `none` and why.
- **Known gaps**: open issues the answer must state rather than paper over.
- **Loaded sessions** (`systemwide_loaded`, `attached_inproc_loaded`) carry background load, so
  the user's rows sit below the top rows. A question about the user's own code narrows with
  `--pid` or `--image` before the function view; an unfiltered function call before that
  filter is a wasted turn. `--pid` and `--image` select processes: `--pid` one instance,
  `--image` every instance of that image. uProf records the full process path, and `--image`
  matches all of it, so the calls write `--image %<exe>`; the sessions record full paths too.
- A question about the user's own code names the process (exe name, and PID where the
  capture holds more than one instance) as well as the module (D-019).
- **Open steps** say `open (D-nnn)`:
  - open (D-028): the `defect_lab` captures for SCN-21..28 come from the owner's re-run on the
    reference machine with the images rebuilt for call stacks, reviewed for disclosure before
    commit. Until then each scenario names the captures it needs and gives the parts' shape;
    every number is marked TODO(re-run).
  - open (D-020): loaded attached captures are to be real captures recorded in CI, with a
    stressor process for the load. Until they exist, SCN-02..08, 11, 17 and 19 run on their
    unloaded sessions, and SCN-13 and 15 have none.
- On a timer capture an answer may name the instructions in the hot window as facts about the
  code, while saying that a timer capture cannot show they are the cause (D-021).
- `cpu_seconds` is samples × the sampling interval / the analysing host's core frequency, so its
  value differs between hosts; shares and sample counts do not. The IBS sessions sample every
  4,700,000 cycles.
- ± is the relative error 1/√samples.

## Answer contract

| Part | Content |
|---|---|
| Location | module + function range `[function_start, function_end)` + `hot_rva`, or source `file:line`, or instruction RVAs |
| Proof | samples behind it; `attribution` (`unique`/`derived`/`shared`); the symbol-coverage split; the build identity of the image used; the sampler from `METRIC` |
| Cost | primary-measure share, plus `cpu_seconds` (sampled CPU time, not wall or frame time) |
| Confidence | sample count and relative error ≈ 1/√samples; `SANITY`/`ACHIEVED`; `KERNEL_COVERAGE` |
| Saving | upper bound = the range's `cpu_seconds` |

- **Lens:** "own" is the module set the user names, not `isSystemModule`. Own modules may be
  drilled. Modules outside keep their full share in one summary line, never dropped or shrunk,
  and are not listed in detail.
- **Caller paths (#187):** captures carry no stacks, so no answer may attribute an outside hot
  path to the own call sites that lead into it.

## Pass criteria

A trial run passes when both hold:

- **Answer:** the final answer gives the scenario's answer, with every part of the contract.
- **Context:** the run ends with fewer than 50,000 tokens of total context: the model's input at
  its final turn, the harness system prompt included, as the harness's usage report gives it.
  The limit is a defect ceiling, not a target: a run that reaches it is defective, and a run
  below it should still use only the context its minimal calls need. A harness that reports no
  usage records the context as "not measured", and the run cannot pass this criterion.

## Sessions

| Session | Shape |
|---|---|
| `attached` | attached cycle-controlled IBS OP, process `storage_test.exe` PID 3112, 97 samples. Modules: own `fixture_full.exe` with its PDB beside it (12 samples); `amdvlk64.dll`, system flag, no PDB (60); `ntdll.dll`, system flag, names recorded by uProf (20); `vendorlib.dll`, recorded path not on this host (5) |
| `attached_after` | `attached` with the own module cut to a third (12 → 4 samples) |
| `pdb_far` | the own exe with its PDB in `bins/pdbs`, not beside it; 30 samples, 25 in the own exe |
| `systemwide` | `attached` plus `node.exe` PID 4242 (80 samples), system-wide, 177 samples |
| `opcount` | `attached` with op-count IBS (dispatched ops per sample) |
| `pmc` | `attached` as a PMC `CYCLES_NOT_IN_HALT` capture without an IBS dump |
| `timer` | the committed real timer capture of `heap_churn.exe` (see `examples/timer-heap-churn`); image and PDB in `bins/timer` |
| `systemwide_loaded` | `systemwide` under desktop load: 26 more processes of 22 images (browser, antivirus, indexer and others), 4,660 samples. `storage_test.exe` PID 3112 holds `attached`'s samples doubled (194, 4.2 %, rank 11 of 28 processes); PID 3113 is an unrelated instance of the same image (10 samples, all in `fixture_full.exe`). The unfiltered function view's 20 rows are all foreign |
| `attached_inproc_loaded` | `attached` plus 22 runtime, graphics and vendor DLLs inside the same process, each hotter than the own `fixture_entry`; 746 samples |

## SCN-01: where did my app's CPU time go

- **Session:** `systemwide_loaded`
- **Question:** "Where did the CPU time of my app storage_test.exe (PID 3112) go? What are the hottest functions? The capture is in `<CAPTURE>`."
- **Answer:**
  - **Location:** in the app's process (PID 3112): `amdvlk64.dll` `fixture_entry` at 0x101B, `ntdll.dll` `RtlQueryPerformanceCounter` at 0x1000, `amdvlk64.dll` `fixture_second` at 0x1030, then own `fixture_entry` in `fixture_full.exe` at 0x1018.
  - **Proof:** under `--pid 3112`, 80, 28, 24 and 18 samples, `attribution` unique; `amdvlk64.dll` has no PDB, its names are exports.
  - **Cost:** shares of PID 3112's 194 samples: modules `amdvlk64.dll` 0.619, `ntdll.dll` 0.206, `fixture_full.exe` 0.124, `vendorlib.dll` 0.052; functions 0.412, 0.144, 0.124, 0.093. The PID's share of the whole capture is not printed under `--pid`.
  - **Confidence:** PID ±7% (194 samples), own `fixture_entry` ±24% (18).
  - **Saving:** at most own `fixture_entry`'s `cpu_seconds`; the driver's cost is outside the app's code.
- **Known gaps:** #286 (a `--pid` scope does not state its share of the capture).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --pid 3112`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --pid 3112`

## SCN-02: mine versus the driver and ntdll

- **Session:** `attached`
- **Question:** "My frame loop is slow. I attached AMD uProf (IBS) to my app storage_test.exe; the capture is in `<CAPTURE>`. My own code is fixture_full.exe. How much of the CPU time is mine versus the Vulkan driver (amdvlk64.dll) and ntdll, and what in my code is hot?"
- **Answer:**
  - **Location:** own hot function `fixture_entry` in `fixture_full.exe`, hot RVA 0x1018.
  - **Proof:** 9 samples at 0x1018, `attribution` unique, named by uProf and by the PDB beside the exe; cycle-controlled IBS OP sampler; outside modules: `amdvlk64.dll` 0.619 (60 samples), `ntdll.dll` 0.206 (20), `vendorlib.dll` 0.052 (5), kept unshrunk in one summary.
  - **Cost:** own `fixture_full.exe` 0.124 of the core cycles (12 samples), `fixture_entry` 0.093 (9); `cpu_seconds` = samples × 4.7 M cycles / core frequency; CPU time, not frame time; user mode only (`KERNEL_COVERAGE`: no kernel samples).
  - **Confidence:** own module ±29% (12 samples), `fixture_entry` ±33% (9).
  - **Saving:** at most `fixture_entry`'s `cpu_seconds`; which own call sites lead into the driver is not answerable (caller paths).
- **Known gaps:** #187
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-03: drill my hottest function to source lines

- **Session:** `attached`
- **Question:** "I have an AMD uProf capture of my app in `<CAPTURE>`. My code is fixture_full.exe (the PDB is next to the exe). Drill my hottest function down to source lines and instructions."
- **Answer:**
  - **Location:** `fixture_entry` in `fixture_full.exe`, line `fixture_debug.c:7` (recorded as `/build/fixture_debug.c`), instruction 0x1018 `movl 0x24(%rsp), %ecx`.
  - **Proof:** all 9 of the function's samples on that line and instruction (`parent_sample_share` 1.0, `attribution` unique); the disassembled image is the captured build (`build.verified`, identity by checksum).
  - **Cost:** 0.093 of the capture's core cycles; `cpu_seconds` = 9 × 4.7 M cycles / core frequency.
  - **Confidence:** ±33% (9 samples).
  - **Saving:** at most that line's `cpu_seconds`.
- **Known gaps:** #339 (the instruction JSON can exceed a harness's output limit on a real capture).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function fixture_entry --module <OWN_MODULE>`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --function fixture_entry --module <OWN_MODULE> --json --top 1`

## SCN-04: my PDB is in another folder

- **Session:** `pdb_far`
- **Question:** "I attached AMD uProf to my app; the capture is in `<CAPTURE>`. The PDB for my exe fixture_full.exe is not next to the exe, it is in `<PDB_DIR>`. Show me my hottest function down to source lines."
- **Answer:**
  - **Location:** `fixture_entry` in `fixture_full.exe`, line `fixture_debug.c:7` at 0x1018.
  - **Proof:** 20 samples, all of the function's (`parent_sample_share` 1.0), named through the PDB passed with `--symbol-path` on every symbolizing call.
  - **Cost:** 0.667 of the capture's core cycles (20 of 30 samples); `cpu_seconds` = 20 × 4.7 M cycles / core frequency.
  - **Confidence:** ±22% (20 samples).
  - **Saving:** at most that line's `cpu_seconds`.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --symbol-path <PDB_DIR>`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function fixture_entry --module <OWN_MODULE> --symbol-path <PDB_DIR>`

## SCN-05: symbols missing, DLL not on this machine

- **Session:** `attached`
- **Question:** "In my AMD uProf capture (`<CAPTURE>`) symbols are missing for some DLLs, and vendorlib.dll isn't even on this machine. Where is the time going in those DLLs?"
- **Answer:**
  - **Location:** `amdvlk64.dll`: `fixture_entry` `[0x1000, 0x1030)` hot 0x101B, `fixture_second` `[0x1030, 0x1050)`, derived `pdata_0x1050` `[0x1050, 0x1062)`; `vendorlib.dll`: `<unresolved>`, hot RVA 0x2040, no range.
  - **Proof:** `amdvlk64.dll` ranges come from its exports and `.pdata` (`attribution` unique for exports, derived for `pdata_`), a finding, not a blocker; `vendorlib.dll` is not available on this host (`module_missing`), and `--image-path` with the DLL is the alternative.
  - **Cost:** `amdvlk64.dll` 0.619 (60 samples: 40, 12, 8), `vendorlib.dll` 0.052 (5); `cpu_seconds` = samples × 4.7 M cycles / core frequency.
  - **Confidence:** `fixture_entry` in the driver ±16% (40 samples), `vendorlib.dll` ±45% (5).
  - **Saving:** n/a (neither DLL is the user's code).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-06: profile the process I just started

- **Session:** none (needs a live collect on an AMD host)
- **Question:** "Profile the process I just started, PID `<PID>`, for 30 seconds and tell me where its CPU time goes."
- **Answer:** the SCN-02 parts for the collected process; values need a capture.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py collect --out-dir <OUT_DIR> --seconds 30 --process <PID>`
  2. `python <SKILL_DIR>/tools/uprof.py translate --session <SESSION> --out-dir <CAPTURE>`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  4. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-07: is this hotspot memory-bound

- **Session:** `attached`
- **Question:** "AMD uProf IBS capture of my app: `<CAPTURE>`. My code is fixture_full.exe. Is my hottest spot memory-bound?"
- **Answer:**
  - **Location:** `fixture_entry` in `fixture_full.exe`, instruction 0x1018 `movl 0x24(%rsp), %ecx` (`fixture_debug.c:7`).
  - **Proof:** yes, memory-bound: all 9 sampled ops at 0x1018 are this load and all 9 missed the data cache (`IbsDcMiss` 9 of 9), with 2400 tag-to-retire cycles each (`IbsTagToRetCtr` sum 21600); `DataSrc` code 8 for all 9 (AMD-encoded, not decoded by the tools). The function's `tag_to_retire_share` 0.357 against its `sample_share` 0.093 points the same way but is a hint, not the proof.
  - **Cost:** 0.093 of the core cycles; `cpu_seconds` = 9 × 4.7 M cycles / core frequency.
  - **Confidence:** ±33% (9 samples).
  - **Saving:** at most that instruction's `cpu_seconds`, if the load stops missing.
- **Known gaps:** #339 (the instruction JSON can exceed a harness's output limit on a real capture).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --function fixture_entry --module <OWN_MODULE> --json --top 1`

## SCN-08: did my fix help

- **Session:** `attached`, `attached_after`
- **Question:** "Did my fix help? I profiled my app with AMD uProf before and after the fix. Before: `<BEFORE>`  After: `<AFTER>`. My code is fixture_full.exe."
- **Answer:**
  - **Location:** own `fixture_full.exe` in both captures.
  - **Proof:** own module 12 samples before, 4 after; `amdvlk64.dll` 60 in both; compared as cost per unit of work (equal-work runs or the same phase), not as shares: the unchanged driver's share grows from 0.619 to 0.674.
  - **Cost:** own `cpu_seconds` falls to a third (12 → 4 samples × 4.7 M cycles / core frequency).
  - **Confidence:** ±29% before (12 samples), ±50% after (4); the drop exceeds both.
  - **Saving:** the measured difference, 8 samples' `cpu_seconds`, if both runs did the same work.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <BEFORE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <AFTER> --by module`

## SCN-09: system-wide capture, what is slow in my process

- **Session:** `systemwide_loaded`
- **Question:** "This is a system-wide AMD uProf capture: `<CAPTURE>`. What is slow in my process storage_test.exe, PID 3112?"
- **Answer:**
  - **Location:** in PID 3112: own `fixture_entry` at 0x1018; outside, `amdvlk64.dll` `fixture_entry` at 0x101B.
  - **Proof:** scoped to PID 3112 (194 samples): `amdvlk64.dll` `fixture_entry` 80 samples, own `fixture_entry` 18, `attribution` unique. `--image %storage_test.exe` would also count PID 3113, an unrelated instance of the same image, and raise `fixture_full.exe` from 24 to 34 samples.
  - **Cost:** shares of PID 3112's samples: `amdvlk64.dll` 0.619, `ntdll.dll` 0.206, `fixture_full.exe` 0.124, `vendorlib.dll` 0.052; own `fixture_entry` 0.093. The PID's share of the whole capture is not printed under `--pid`. `cpu_seconds` = samples × 4.7 M cycles / core frequency.
  - **Confidence:** PID ±7% (194 samples), own module ±20% (24).
  - **Saving:** at most own `fixture_entry`'s `cpu_seconds`.
- **Known gaps:** #286 (a `--pid` scope does not state its share of the capture).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --pid 3112`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --pid 3112`

## SCN-10: only the loading phase is slow

- **Session:** `systemwide_loaded`
- **Question:** "Only the loading phase is slow. My app storage_test.exe (PID 3112) printed QueryPerformanceCounter 12000000 at its start and 15000000 at its end; the capture is in `<CAPTURE>`."
- **Answer:**
  - **Location:** in the window, in the app's process: `amdvlk64.dll` `fixture_entry` at 0x101B, `ntdll.dll` `RtlAllocateHeap` at 0x1050, own `fixture_helper` in `fixture_full.exe` at 0x1000.
  - **Proof:** `anchor` maps the readings to 0.2 s and 0.5 s from capture start at its default 10 MHz, because the session records no QPC frequency; `--time-filter 0.2:0.5` with `--pid 3112` keeps 55 samples: 33, 12 and 6, `attribution` unique.
  - **Cost:** shares of the window's samples in PID 3112: modules `amdvlk64.dll` 0.673, `ntdll.dll` 0.218, `fixture_full.exe` 0.109; functions 0.600, 0.218, 0.109.
  - **Confidence:** window ±13% (55 samples), own `fixture_helper` ±41% (6).
  - **Saving:** at most own `fixture_helper`'s `cpu_seconds` in the window.
- **Known gaps:** #286 (a scope does not state its share of the capture).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py anchor --session <CAPTURE> --qpc 12000000`
  2. `python <SKILL_DIR>/tools/uprof.py anchor --session <CAPTURE> --qpc 15000000`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --pid 3112 --time-filter 0.2:0.5`
  4. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --pid 3112 --time-filter 0.2:0.5`

## SCN-11: spinning or computing

- **Session:** `attached`
- **Question:** "AMD uProf IBS capture of my app: `<CAPTURE>`. My code is fixture_full.exe. Is the hot spot spinning (waiting) or actually computing?"
- **Answer:**
  - **Location:** `fixture_entry` in `fixture_full.exe`, instruction 0x1018 `movl 0x24(%rsp), %ecx`.
  - **Proof:** not spinning: the decoded window `[0x1010, 0x1030)` has no `pause` and no locked instruction, and the dump has no `IbsDcLockedOp` column to suggest one; the hot op is a load that missed the data cache in all 9 samples, so the code computes but stalls on memory.
  - **Cost:** 0.093 of the core cycles; `cpu_seconds` = 9 × 4.7 M cycles / core frequency.
  - **Confidence:** ±33% (9 samples).
  - **Saving:** at most that instruction's `cpu_seconds`.
- **Known gaps:** #339 (the instruction JSON can exceed a harness's output limit on a real capture).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --function fixture_entry --module <OWN_MODULE> --json --top 1`

## SCN-12: which thread is hot

- **Session:** `attached`
- **Question:** "AMD uProf capture of my app storage_test.exe: `<CAPTURE>`. Which thread is hot?"
- **Answer:**
  - **Location:** TID 11, with the driver's `fixture_entry` at 0x101B as its hot range.
  - **Proof:** TID 11 carries 75 of the 97 samples (`SANITY` names the busiest TID's count); no view breaks rows down by thread.
  - **Cost:** 0.773 of the core cycles.
  - **Confidence:** ±12% (75 samples).
  - **Saving:** n/a (a thread is not a code range).
- **Known gaps:** #32
- **Minimal calls:** none (no per-thread view)

## SCN-13: drill to source lines on Linux

- **Session:** none (no ELF capture fixture)
- **Question:** "I have an AMD uProf capture of my Linux app in `<CAPTURE>`, built with DWARF. Drill my hottest function down to source lines."
- **Answer:** the SCN-03 parts, with the lines read from DWARF; values need a capture.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function <FUNCTION> --module <OWN_MODULE>`

## SCN-14: is kernel time part of the cost

- **Session:** `attached`
- **Question:** "AMD uProf capture: `<CAPTURE>`. Is kernel time part of the cost shown in this profile?"
- **Answer:**
  - **Location:** n/a (the question is about the capture, not a code range).
  - **Proof:** no: `KERNEL_COVERAGE` reports no sample in any kernel image although the sampler requested OS mode; only an elevated capture includes kernel samples.
  - **Cost:** every share and `cpu_seconds` in this profile is user-mode CPU time only.
  - **Confidence:** 0 kernel samples of 97.
  - **Saving:** n/a.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by process`

## SCN-15: the hot code is inlined

- **Session:** none (no inline-frame fixture)
- **Question:** "My hottest function in `<CAPTURE>` is mostly inlined code. Which source lines are hot?"
- **Answer:** the SCN-03 parts, with the innermost inlined `file:line`, its containing function, and the stated limitation that inline overloads of one name are not told apart; values need a capture.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function <FUNCTION> --module <OWN_MODULE>`

## SCN-16: samples in JIT code

- **Session:** none (no capture with anonymous code)
- **Question:** "My app runs JIT-compiled code. The capture is in `<CAPTURE>`. Where does the time go?"
- **Answer:** the share of samples without a module, stated and never dropped, beside the SCN-02 parts for the modules; values need a capture.
- **Minimal calls:** none (not evaluated without a capture)

## SCN-17: where did time go on an op-count capture

- **Session:** `opcount`
- **Question:** "AMD uProf capture of my app: `<CAPTURE>`. Where did the time go?"
- **Answer:**
  - **Location:** the driver's `fixture_entry` at 0x101B is the hottest range; own `fixture_entry` at 0x1018.
  - **Proof:** rows ranked by dispatched-op share (`METRIC`: op-count interval): `amdvlk64.dll` 0.619, `ntdll.dll` 0.206, `fixture_full.exe` 0.124, `vendorlib.dll` 0.052.
  - **Cost:** n/a as time: the op-count interval measures work, so the shares are op shares and there is no `cpu_seconds`.
  - **Confidence:** driver ±13% (60 samples), own ±29% (12).
  - **Saving:** n/a as time (a cycle-controlled capture gives it).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-18: timer capture on a non-AMD host

- **Session:** `timer`
- **Question:** "I profiled my app heap_churn.exe with AMD uProf on an Intel machine, so it is a timer-based capture: `<CAPTURE>`. The exe and its PDB are in `<IMAGE_DIR>`. Where does its CPU time go?"
- **Answer:** the expected answer recorded in `examples/timer-heap-churn`.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function "static unsigned int hash_block(const unsigned char *, unsigned int)" --module c:/fixture/heap_churn.exe --image-path <IMAGE_DIR>`

## SCN-19: memory-bound on a PMC capture

- **Session:** `pmc`
- **Question:** "AMD uProf capture of my app: `<CAPTURE>`. Is my app memory-bound?"
- **Answer:**
  - **Location:** n/a (no memory evidence in this capture).
  - **Proof:** this PMC `CYCLES_NOT_IN_HALT` capture has no IBS samples, so it cannot show data-cache misses or load latency; the alternative is a re-capture with `collect --event ibs-op`.
  - **Cost:** n/a for memory; the rows still rank busy core cycles (own `fixture_full.exe` 0.124).
  - **Confidence:** n/a.
  - **Saving:** n/a.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-20: only my exe is mine

- **Session:** `attached_inproc_loaded`
- **Question:** "AMD uProf capture of my app storage_test.exe: `<CAPTURE>`. Only fixture_full.exe is my code; vendorlib.dll is a vendor DLL, not mine. How much time is mine versus everything else, and what is hot in my code?"
- **Answer:**
  - **Location:** not reached in the default rows. With the process filter (`--image` or `--pid` before the function view), outside code inside the same process can push the own functions below the default rows; no scope narrows below the process, and only a larger `--top` lists them. The filtered function view's 20 rows are all outside functions, each hotter than any own function.
  - **Proof:** `fixture_full.exe` has 12 of the process's 746 samples, rank 25 of 26 modules, so the module view needs `--top all`; everything outside the own exe is one total, including the third-party `vendorlib.dll`.
  - **Cost:** own 0.016 (12 samples), everything else 0.984 (734); `cpu_seconds` = samples × 4.7 M cycles / core frequency.
  - **Confidence:** own ±29% (12 samples).
  - **Saving:** n/a: the own hot function is not located.
- **Known gaps:** #345 (outside code inside the process pushes the own functions out of the default rows).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --top all`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %storage_test.exe`

## SCN-21: what can I improve (memory class)

- **Session:** none (open (D-028): needs `memory.timer` from the owner's re-run with the rebuilt images, under `trials/defect_lab/captures/<platform>/` with `images/` beside it; the cause also needs `memory.ibs`, a system-wide IBS capture, which the SANITY guard refuses today: that part is blocked on #358)
- **Question:** "lab_core.dll is my module, in my program defect_lab.exe. Here is an AMD uProf capture: `<CAPTURE>`; the exe, its DLLs and their PDBs are in `<IMAGE_DIR>`. What can I improve?"
- **Answer:** the parts' shape. Location: `lab_memory` in `lab_core.dll` and its hot RVA. Proof: its samples; on an IBS capture, `IbsDcMiss` set and long tag-to-retire at the hot load make it memory-bound; on a timer capture, hotspot and cost shown, cause not determinable from a timer capture. Cost: its share of `defect_lab.exe`'s samples and `cpu_seconds`. Confidence: 1/√samples. Saving: at most its `cpu_seconds`, by changing the access pattern or data layout. TODO(re-run): the function's samples and share of `defect_lab.exe`, its `cpu_seconds`, the hot RVA and its source line, from the owner's re-run with the rebuilt images.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --image %defect_lab.exe`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %defect_lab.exe`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --image %defect_lab.exe --function <FUNCTION> --module c:/capture/lab_core.dll --image-path <IMAGE_DIR> --top 1`

## SCN-22: what can I improve (lock class)

- **Session:** none (open (D-028): needs `lock.timer` from the owner's re-run with the rebuilt images, under `trials/defect_lab/captures/<platform>/` with `images/` beside it; the cause also needs `lock.ibs`, a system-wide IBS capture, which the SANITY guard refuses today: that part is blocked on #358)
- **Question:** "lab_core.dll is my module, in my program defect_lab.exe. Here is an AMD uProf capture: `<CAPTURE>`; the exe, its DLLs and their PDBs are in `<IMAGE_DIR>`. What can I improve?"
- **Answer:** the parts' shape. Location: `lab_lock` and `lock_thread` in `lab_core.dll`. Proof: on an IBS capture, `IbsDcLockedOp` and a `pause` spin in the decoded window show the time is waiting for the lock; on a timer capture, hotspot and cost shown, cause not determinable from a timer capture (the `lock cmpxchg` and `pause` in the code may be named as facts). Cost: their share and `cpu_seconds`. Confidence: 1/√samples. Saving: by reducing contention, at most their `cpu_seconds`. TODO(re-run): the function's samples and share of `defect_lab.exe`, its `cpu_seconds`, the hot RVA and its source line, from the owner's re-run with the rebuilt images.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --image %defect_lab.exe`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %defect_lab.exe`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --image %defect_lab.exe --function <FUNCTION> --module c:/capture/lab_core.dll --image-path <IMAGE_DIR> --top 1`

## SCN-23: what can I improve (branch class)

- **Session:** none (open (D-028): needs `branch.timer` from the owner's re-run with the rebuilt images, under `trials/defect_lab/captures/<platform>/` with `images/` beside it; the cause also needs `branch.ibs`, a system-wide IBS capture, which the SANITY guard refuses today: that part is blocked on #358)
- **Question:** "lab_core.dll is my module, in my program defect_lab.exe. Here is an AMD uProf capture: `<CAPTURE>`; the exe, its DLLs and their PDBs are in `<IMAGE_DIR>`. What can I improve?"
- **Answer:** the parts' shape. Location: `lab_branch` in `lab_core.dll` and its hot RVA. Proof: on an IBS capture, `IbsOpBrnMisp` set at the hot branch shows mispredictions; on a timer capture, hotspot and cost shown, cause not determinable from a timer capture. Cost: its share and `cpu_seconds`. Confidence: 1/√samples. Saving: at most its `cpu_seconds`, by making the branch predictable or branch-free. TODO(re-run): the function's samples and share of `defect_lab.exe`, its `cpu_seconds`, the hot RVA and its source line, from the owner's re-run with the rebuilt images.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --image %defect_lab.exe`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %defect_lab.exe`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --image %defect_lab.exe --function <FUNCTION> --module c:/capture/lab_core.dll --image-path <IMAGE_DIR> --top 1`

## SCN-24: what can I improve (os class)

- **Session:** none (open (D-028): needs `os.timer` from the owner's re-run with the rebuilt images, under `trials/defect_lab/captures/<platform>/` with `images/` beside it; and `os.timer-cg` for the call stacks from `lab_os` into `ntdll.dll`)
- **Question:** "lab_core.dll is my module, in my program defect_lab.exe. Here is an AMD uProf capture: `<CAPTURE>`; the exe, its DLLs and their PDBs are in `<IMAGE_DIR>`. What can I improve?"
- **Answer:** the parts' shape. Location: outside the user's module, the `ntdll.dll` heap functions; `lab_os` itself is small. Proof: their samples within `defect_lab.exe`; own call sites into them cannot be attributed (#187). Cost: `ntdll.dll`'s share of the process. Confidence: 1/√samples. Saving: by allocating less often from `lab_os`, a hypothesis, not measured. TODO(re-run): the function's samples and share of `defect_lab.exe`, its `cpu_seconds`, the hot RVA and its source line, from the owner's re-run with the rebuilt images.
- **Known gaps:** #187 (own call paths into outside code; the timer-cg capture holds the stacks, no view shows them yet)
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --image %defect_lab.exe`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %defect_lab.exe`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --image %defect_lab.exe --function <FUNCTION> --module c:/capture/lab_core.dll --image-path <IMAGE_DIR> --top 1`

## SCN-25: what can I improve (vendor class)

- **Session:** none (open (D-028): needs `vendor.timer` from the owner's re-run with the rebuilt images, under `trials/defect_lab/captures/<platform>/` with `images/` beside it; and `vendor.timer-cg` for the call stacks from `lab_core.dll` into `lab_vendor.dll`)
- **Question:** "lab_core.dll is my module, in my program defect_lab.exe. Here is an AMD uProf capture: `<CAPTURE>`; the exe, its DLLs and their PDBs are in `<IMAGE_DIR>`. What can I improve?"
- **Answer:** the parts' shape. Location: outside the user's module, `vendor_work` in `lab_vendor.dll`, kept as one outside line. Proof: its samples within `defect_lab.exe`; own call sites into it cannot be attributed (#187). Cost: its share of the process. Confidence: 1/√samples. Saving: by calling it less, or by a vendor change; a hypothesis, not measured. TODO(re-run): the function's samples and share of `defect_lab.exe`, its `cpu_seconds`, the hot RVA and its source line, from the owner's re-run with the rebuilt images.
- **Known gaps:** #187 (own call paths into outside code; the timer-cg capture holds the stacks, no view shows them yet)
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --image %defect_lab.exe`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %defect_lab.exe`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --image %defect_lab.exe --function <FUNCTION> --module c:/capture/lab_vendor.dll --image-path <IMAGE_DIR> --top 1`

## SCN-26: what can I improve (wait class)

- **Session:** none (open (D-028): needs `wait.timer` from the owner's re-run with the rebuilt images, under `trials/defect_lab/captures/<platform>/` with `images/` beside it)
- **Question:** "lab_core.dll is my module, in my program defect_lab.exe. Here is an AMD uProf capture: `<CAPTURE>`; the exe, its DLLs and their PDBs are in `<IMAGE_DIR>`. What can I improve?"
- **Answer:** the parts' shape. Location: `lab_wait` in `lab_core.dll`, the little code that runs. Proof: few samples against the capture's wall time: CPU sampling only samples running threads, so the time the thread spends blocked is off-CPU and does not appear. Cost: its small share and `cpu_seconds`, CPU time, not wall-clock time. Confidence: 1/√samples. Saving: n/a: the improvement lies in what the program waits for, which needs a wait or I/O analysis outside this skill. TODO(re-run): the function's samples and share of `defect_lab.exe`, its `cpu_seconds`, the hot RVA and its source line, from the owner's re-run with the rebuilt images.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --image %defect_lab.exe`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %defect_lab.exe`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --image %defect_lab.exe --function <FUNCTION> --module c:/capture/lab_core.dll --image-path <IMAGE_DIR> --top 1`

## SCN-27: what can I improve (chain class)

- **Session:** none (open (D-028): needs `chain.timer` from the owner's re-run with the rebuilt images, under `trials/defect_lab/captures/<platform>/` with `images/` beside it; the cause also needs `chain.ibs`, a system-wide IBS capture, which the SANITY guard refuses today: that part is blocked on #358)
- **Question:** "lab_core.dll is my module, in my program defect_lab.exe. Here is an AMD uProf capture: `<CAPTURE>`; the exe, its DLLs and their PDBs are in `<IMAGE_DIR>`. What can I improve?"
- **Answer:** the parts' shape. Location: `lab_chain` in `lab_core.dll` and its hot RVA. Proof: on an IBS capture, no `IbsDcMiss` and long tag-to-retire on `imul`/`div` show a latency-bound dependency chain; on a timer capture, hotspot and cost shown, cause not determinable from a timer capture (the `imul`/`div` chain may be named as a fact). Cost: its share and `cpu_seconds`. Confidence: 1/√samples. Saving: at most its `cpu_seconds`, by breaking the chain into independent accumulators or replacing the divide. TODO(re-run): the function's samples and share of `defect_lab.exe`, its `cpu_seconds`, the hot RVA and its source line, from the owner's re-run with the rebuilt images.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --image %defect_lab.exe`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %defect_lab.exe`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --image %defect_lab.exe --function <FUNCTION> --module c:/capture/lab_core.dll --image-path <IMAGE_DIR> --top 1`

## SCN-28: what can I improve (efficient class)

- **Session:** none (open (D-028): needs `efficient.timer` from the owner's re-run with the rebuilt images, under `trials/defect_lab/captures/<platform>/` with `images/` beside it)
- **Question:** "lab_core.dll is my module, in my program defect_lab.exe. Here is an AMD uProf capture: `<CAPTURE>`; the exe, its DLLs and their PDBs are in `<IMAGE_DIR>`. What can I improve?"
- **Answer:** the parts' shape. Location: `lab_efficient` in `lab_core.dll` and its hot RVA. Proof: its samples; the answer names the hotspot and its cost and never claims that no algorithmic optimization is left, because a profile cannot show that. Cost: its share and `cpu_seconds`. Confidence: 1/√samples. Saving: at most its `cpu_seconds`, from doing less work or a different algorithm, found by reading the source. A run that proposes an instruction-level fix without evidence, or declares the code optimal, fails. TODO(re-run): the function's samples and share of `defect_lab.exe`, its `cpu_seconds`, the hot RVA and its source line, from the owner's re-run with the rebuilt images.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --image %defect_lab.exe`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --image %defect_lab.exe`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --image %defect_lab.exe --function <FUNCTION> --module c:/capture/lab_core.dll --image-path <IMAGE_DIR> --top 1`
