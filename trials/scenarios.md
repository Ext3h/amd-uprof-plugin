# Trial scenarios

Typical profiling questions, each with its input, the correct actionable answer and the fewest
tool calls that reach it. A trial gives an agent on a small, fast model only the question and the
installed plugin, then compares its calls and answer with this catalogue.

`tests/test_trials.py` checks the catalogue: every scenario has its fields, every call parses with
the current `uprof.py`, and every session it names is one `build_captures.py` writes.

## Conventions

- Scenario IDs `SCN-nn` are stable. A dropped scenario's ID is retired, never reused.
- **Session**: the `build_captures.py` session the scenario runs on, or `none` with the reason.
  The sessions are synthetic: they exercise the tools' code paths, not real capture statistics.
- **Question**: the prompt, phrased as a user would write it. `<CAPTURE>` is the session
  directory; `<BEFORE>`/`<AFTER>` are two sessions, `<PDB_DIR>` and `<IMAGE_DIR>` the folders
  the user names.
- **Minimal calls**: the `uprof.py` calls in order. Loading the skill and the final write-up are
  not counted. `<SKILL_DIR>` is the installed skill; `<OWN_MODULE>` is the module cell the
  previous call printed for the user's own image. A scenario that is not answerable today says
  `none` and why.
- **Known gaps**: open issues the answer must state rather than paper over.
- `cpu_seconds` is samples × interval / the analysing host's core frequency, so its value differs
  between hosts; shares and sample counts do not.

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

## Sessions

| Session | Shape |
|---|---|
| `attached` | attached cycle-controlled IBS OP, process `storage_test.exe` PID 3112, 97 samples. Modules: own `fixture_full.exe` with its PDB beside it (12 samples); `amdvlk64.dll`, system flag, no PDB (60); `ntdll.dll`, system flag, names recorded by uProf (20); `vendorlib.dll`, recorded path not on this host (5) |
| `attached_after` | `attached` with the own module cut to a third (12 → 4 samples) |
| `pdb_far` | the own exe with its PDB in `bins/pdbs`, not beside it |
| `systemwide` | `attached` plus `node.exe` PID 4242 (80 samples), system-wide |
| `opcount` | `attached` with op-count IBS (dispatched ops per sample) |
| `pmc` | `attached` as a PMC `CYCLES_NOT_IN_HALT` capture without an IBS dump |
| `timer` | the committed real timer capture of `heap_churn.exe` (see `examples/timer-heap-churn`); image and PDB in `bins/timer` |

## SCN-01: where did my app's CPU time go

- **Session:** none (the 954-sample IBS capture of `examples/ibs-trial-9` is not published)
- **Question:** "Where did my app's CPU time go? What are the hottest functions? The capture is in `<CAPTURE>`."
- **Answer:** as recorded in `examples/ibs-trial-9`: the vendor driver `amdvlk64.dll` (system-flagged, outside) carries 51.78%; own `storage_test.exe` 8.70%; the hottest own range is derived (`pdata_…`), never called unresolved; user-mode only; `cpu_seconds` per row.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-02: mine versus the driver and ntdll

- **Session:** `attached`
- **Question:** "My frame loop is slow. I attached AMD uProf (IBS) to my app storage_test.exe; the capture is in `<CAPTURE>`. My own code is fixture_full.exe. How much of the CPU time is mine versus the Vulkan driver (amdvlk64.dll) and ntdll, and what in my code is hot?"
- **Answer:** outside total kept unshrunk: `amdvlk64.dll` 0.619, `ntdll.dll` 0.206, `vendorlib.dll` 0.052; user-mode only (`KERNEL_COVERAGE`) and CPU time, not frame time. Own `fixture_full.exe` 0.124 (12 samples, ±29%). Own hot function `fixture_entry`, hot RVA 0x1018, 9 samples (±33%). Which own call sites lead into the driver is not answerable (caller paths).
- **Known gaps:** #187
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-03: drill my hottest function to source lines

- **Session:** `attached`
- **Question:** "I have an AMD uProf capture of my app in `<CAPTURE>`. My code is fixture_full.exe (the PDB is next to the exe). Drill my hottest function down to source lines and instructions."
- **Answer:** `fixture_entry` in `fixture_full.exe`: line `fixture_debug.c:7` (recorded as `/build/fixture_debug.c`) at 0x1018 holds all 9 of the function's samples (`parent_sample_share` 1.0); the instruction window at 0x1018 from the verified build.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function fixture_entry --module <OWN_MODULE>`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --function fixture_entry --module <OWN_MODULE> --json`

## SCN-04: my PDB is in another folder

- **Session:** `pdb_far`
- **Question:** "I attached AMD uProf to my app; the capture is in `<CAPTURE>`. The PDB for my exe fixture_full.exe is not next to the exe, it is in `<PDB_DIR>`. Show me my hottest function down to source lines."
- **Answer:** as SCN-03, with `--symbol-path` on every symbolizing call: `fixture_debug.c:7` at 0x1018, 20 samples.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --symbol-path <PDB_DIR>`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function fixture_entry --module <OWN_MODULE> --symbol-path <PDB_DIR>`

## SCN-05: symbols missing, DLL not on this machine

- **Session:** `attached`
- **Question:** "In my AMD uProf capture (`<CAPTURE>`) symbols are missing for some DLLs, and vendorlib.dll isn't even on this machine. Where is the time going in those DLLs?"
- **Answer:** `vendorlib.dll`: `<unresolved>`, hot RVA 0x2040, 5 samples, module file not available; `--image-path` with the DLL is the alternative; no range. `amdvlk64.dll`: derived ranges from its exports and `.pdata` (`fixture_entry` 0x1000-0x1030 with 40 samples at 0x101B) as a finding, not a blocker. Shares unchanged.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-06: profile the process I just started

- **Session:** none (needs a live collect on an AMD host)
- **Question:** "Profile the process I just started, PID `<PID>`, for 30 seconds and tell me where its CPU time goes."
- **Answer:** collect, translate, then the SCN-02 answer for that process.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py collect --out-dir <OUT_DIR> --seconds 30 --process <PID>`
  2. `python <SKILL_DIR>/tools/uprof.py translate --session <SESSION> --out-dir <CAPTURE>`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  4. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-07: is this hotspot memory-bound

- **Session:** `attached`
- **Question:** "AMD uProf IBS capture of my app: `<CAPTURE>`. My code is fixture_full.exe. Is my hottest spot memory-bound?"
- **Answer:** the verdict comes from the per-instruction IBS fields at 0x1018 (`DataSrc`, `IbsDcMiss`, `IbsTagToRetCtr` of its 9 samples); the `tag_to_retire_share` versus `sample_share` ratio is a hint, not proof.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --function fixture_entry --module <OWN_MODULE> --json`

## SCN-08: did my fix help

- **Session:** `attached`, `attached_after`
- **Question:** "Did my fix help? I profiled my app with AMD uProf before and after the fix. Before: `<BEFORE>`  After: `<AFTER>`. My code is fixture_full.exe."
- **Answer:** compared as cost per unit of work (equal-work runs or the same phase), in `cpu_seconds` with both sample counts: own 12 → 4 samples (a third of the CPU time), driver unchanged at 60 samples. Shares are not compared: the unchanged driver's share grows.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <BEFORE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <AFTER> --by module`

## SCN-09: system-wide capture, what is slow in my process

- **Session:** `systemwide`
- **Question:** "This is a system-wide AMD uProf capture: `<CAPTURE>`. What is slow in my process storage_test.exe?"
- **Answer:** process ranking `storage_test.exe` 0.548, `node.exe` 0.452; then the SCN-02 answer scoped to PID 3112.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by process`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --pid 3112`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --pid 3112`

## SCN-10: only the loading phase is slow

- **Session:** none (no capture with QPC markers)
- **Question:** "Only the loading phase is slow. My app printed QueryPerformanceCounter `<QPC1>` at its start and `<QPC2>` at its end; the capture is in `<CAPTURE>`."
- **Answer:** both readings mapped onto the capture clock, then the SCN-02 answer under `--time-filter T1:T2`.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py anchor --session <CAPTURE> --qpc <QPC1>`
  2. `python <SKILL_DIR>/tools/uprof.py anchor --session <CAPTURE> --qpc <QPC2>`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module --time-filter <T1>:<T2>`
  4. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function --time-filter <T1>:<T2>`

## SCN-11: spinning or computing

- **Session:** `attached`
- **Question:** "AMD uProf IBS capture of my app: `<CAPTURE>`. My code is fixture_full.exe. Is the hot spot spinning (waiting) or actually computing?"
- **Answer:** the instruction mix at 0x1018 (`pause`, locked operations, `IbsDcLockedOp`) with its sample counts.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by instruction --function fixture_entry --module <OWN_MODULE> --json`

## SCN-12: which thread is hot

- **Session:** `attached`
- **Question:** "AMD uProf capture of my app storage_test.exe: `<CAPTURE>`. Which thread is hot?"
- **Answer:** per-thread samples and share with the hot range; TID 11 carries 75 of 97 samples.
- **Known gaps:** #32
- **Minimal calls:** none (no per-thread view)

## SCN-13: drill to source lines on Linux

- **Session:** none (no ELF capture fixture)
- **Question:** "I have an AMD uProf capture of my Linux app in `<CAPTURE>`, built with DWARF. Drill my hottest function down to source lines."
- **Answer:** as SCN-03, from DWARF.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function <FUNCTION> --module <OWN_MODULE>`

## SCN-14: is kernel time part of the cost

- **Session:** `attached`
- **Question:** "AMD uProf capture: `<CAPTURE>`. Is kernel time part of the cost shown in this profile?"
- **Answer:** no: `KERNEL_COVERAGE` states that the capture has no kernel-image samples although OS mode was requested; an elevated capture is the alternative.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by process`

## SCN-15: the hot code is inlined

- **Session:** none (no inline-frame fixture)
- **Question:** "My hottest function in `<CAPTURE>` is mostly inlined code. Which source lines are hot?"
- **Answer:** the innermost `file:line`, its containing function, and the inline-overload limitation stated.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function <FUNCTION> --module <OWN_MODULE>`

## SCN-16: samples in JIT code

- **Session:** none (no capture with anonymous code)
- **Question:** "My app runs JIT-compiled code. The capture is in `<CAPTURE>`. Where does the time go?"
- **Answer:** the share of samples without a module stated, never dropped.
- **Minimal calls:** none (not evaluated)

## SCN-17: where did time go on an op-count capture

- **Session:** `opcount`
- **Question:** "AMD uProf capture of my app: `<CAPTURE>`. Where did the time go?"
- **Answer:** rows ranked by dispatched-op share (`amdvlk64.dll` 0.619, `ntdll.dll` 0.206, `fixture_full.exe` 0.124); no `cpu_seconds`: the op-count interval measures work, not time, so the answer gives op shares, not a time cost.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-18: timer capture on a non-AMD host

- **Session:** `timer`
- **Question:** "I profiled my app heap_churn.exe with AMD uProf on an Intel machine, so it is a timer-based capture: `<CAPTURE>`. The exe and its PDB are in `<IMAGE_DIR>`. Where does its CPU time go?"
- **Answer:** as recorded in `examples/timer-heap-churn` (on-CPU sample share; the measures a timer capture lacks are stated once).
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
  3. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by line --function "static unsigned int hash_block(const unsigned char *, unsigned int)" --module c:/fixture/heap_churn.exe --image-path <IMAGE_DIR>`

## SCN-19: memory-bound on a PMC capture

- **Session:** `pmc`
- **Question:** "AMD uProf capture of my app: `<CAPTURE>`. Is my app memory-bound?"
- **Answer:** this PMC capture cannot answer it (no IBS samples): the cause is stated with the re-capture alternative `collect --event ibs-op`.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`

## SCN-20: only my exe is mine

- **Session:** `attached`
- **Question:** "AMD uProf capture of my app: `<CAPTURE>`. Only fixture_full.exe is my code; vendorlib.dll is a vendor DLL, not mine. How much time is mine versus everything else, and what is hot in my code?"
- **Answer:** own 0.124; everything else 0.876, including the third-party `vendorlib.dll` 0.052; own hot function `fixture_entry` at 0x1018.
- **Minimal calls:**
  1. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by module`
  2. `python <SKILL_DIR>/tools/uprof.py profile --session <CAPTURE> --by function`
