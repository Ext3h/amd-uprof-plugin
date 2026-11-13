# Validation evidence

Measured acceptance figures, their capture, filter and estimator. A regression baseline, not
requirements. The estimator itself is defined in `reference.md`; the figures below are the ones
the current `profile` surface can reproduce.

## Estimator (defined in reference.md)

The estimator, its window-before-lag rule and gap charging are in `reference.md`, "Estimator: exact
DeltaTick gaps" (Hofer, JKU PhD thesis, section 3.2.4); `sample_share` is the raw dump sample share,
never a `windowId` count.

## Capture

`D:\Projects\strata-res\ibs-roleplay-3-fresh\AMDuProf-SWP-IBS_Oct-24-2026_18-50-45`:
60 s, 16 CPUs, system-wide, `--config ibs`, op interval 1,000,000 ops. The full ASCII dump has
1,595,914 rows, 16 first-per-CPU; total weight 959.195 s of core time against 961.056 s implied by
`session.uprof`. The 1.861 s deficit is the per-CPU boundary gaps, so the current check reports
OK without a tolerance. The 0:10 window has 296,917 rows, 16 first-in-window.
Environment: Windows 11, Ryzen 7 9800X3D, Python 3.14.6, DuckDB 2.0.0, uProf CLI 5.3.521.0,
LLVM (llvm-symbolizer/llvm-pdbutil/llvm-readobj) from `C:\Program Files\LLVM\bin`.

## Archive acceptance figures

Reproduced 2026-11-07 with the cached 60 s dump reused (`DUMP: ... reused`), so the capture was
not re-translated. Command prefix for every row:
`python tools/uprof.py profile --session D:\Projects\strata-res\ibs-roleplay-3-fresh\AMDuProf-SWP-IBS_Oct-24-2026_18-50-45`.
The denominator is stated with each row: the process-image scope includes **every module of every
matching process** (system DLLs, drivers, JIT code), not only the process's base image.

| Command scope | Quantity | Value | Denominator |
|---|---|---:|---|
| `--by function --image '%strata.exe'` | `ExpertPool::worker` `time_share` | 0.4598452886467337 | process image, whole capture: 1,496,649 samples / 465,548,774,278 ns |
| same | `ExpertPool::worker` `sample_share` | 0.29659726495657967 | same |
| same | `ExpertPool::worker` `tag_to_retire_share` | 0.07919207413573721 | same |
| `--by function --image '%strata.exe' --time-filter 0:10` | `ExpertPool::worker` `time_share` | 0.47746537148319796 | process image, 0:10: 280,047 samples / 83,408,222,088 ns |
| same | `ExpertPool::worker` `sample_share` | 0.29000132120679744 | same |
| same | `ExpertPool::worker` `tag_to_retire_share` | 0.0763744218348601 | same |
| `--by function` | `ExpertPool::worker` `time_share` | 0.22318753474451373 | whole capture: 1,595,914 samples / 959,195,193,101 ns |
| `--by process` | `strata.exe` (pid 5932) `time_share` | 0.4853535314047172 | whole capture: 1,595,914 samples / 959,195,193,101 ns |
| same | `strata.exe` `sample_share` / `tag_to_retire_share` | 0.9378005331114333 / 0.9092966743904525 | same |
| `--by process --time-filter 0:10` | `strata.exe` `time_share` | 0.5226572213110555 | 0:10 window: 296,917 samples, 16 first-in-window |

Every granularity on the whole capture sums to 1.0: process, function and instruction
`sample_share`, `time_share` and `tag_to_retire_share` are 1.0; line `tag_to_retire_share` is
0.9999999999999999 (floating-point sum). Denominators read from `v_scoped` with the same inputs: whole capture
1,595,914 samples / 959,195,193,101 ns over 233 modules; process image 1,496,649 /
465,548,774,278 ns over 53 modules; 0:10 process image 280,047 / 83,408,222,088 ns over 42
modules. The process-scope worker share is lower than the historical module-scope figure because
its denominator includes the process's samples in every module, not only `strata.exe`.

### Agreement (line and instruction)

Exact command: `... --by line --top 20` or `... --by instruction --top 20` (whole capture, no
image filter). Both print:

`mismatch_rate=33.749068171404346% (531048/1573519); name_mismatches=40211 addresses of 47752 compared; unresolved=10284 addresses, 22281 samples (1.396127861526373% of 1595914 scoped)`.

Line and instruction coordinates use the innermost executed frame (`Symbol[0]`); the symbolizer
name is the containing function (`Symbol[last]`), the name uProf `Function.id` carries, so inline
instances are not counted as mismatches.

### Line baseline (innermost-frame attribution)

`... --by line` (whole capture, default `--top 20`). This is the post-fix baseline. Row 21 is the
`<other lines>` remainder.

| # | file | line | function | module | attribution | samples | sample_share | time_share |
|---:|---|---:|---|---|---|---:|---:|---:|
| 1 | `D:\Projects\strata\src\kernels\cpu\pool.cpp` | 1000 | `strata::kernels::cpu::ExpertPool::worker(int)` | strata.exe | unique | 130376 | 0.08169362509508657 | 0.06570961379115599 |
| 2 | `D:\Projects\strata\src\kernels\cpu\pool.cpp` | 1013 | `strata::kernels::cpu::ExpertPool::worker(int)` | strata.exe | unique | 130425 | 0.08172432850391688 | 0.065628702354611885 |
| 3 | `D:\Projects\strata\src\kernels\cpu\pool.cpp` | 1001 | `strata::kernels::cpu::ExpertPool::worker(int)` | strata.exe | unique | 130746 | 0.08192546716176436 | 0.0655713460277707 |
| 4 | `...\include\atomic` | 917 | `std::_Atomic_storage<unsigned int, 4>::load(enum std::memory_order) const` | strata.exe | unique | 52512 | 0.032904028663198646 | 0.026435205718686334 |
| 5 | `D:\Projects\strata\src\kernels\cpu\pool.cpp` | 1014 | `strata::kernels::cpu::ExpertPool::worker(int)` | strata.exe | unique | 52007 | 0.032587595572192485 | 0.026100453829488544 |
| 6 | `...\include\atomic` | 706 | `std::_Atomic_storage<bool, 1>::load(enum std::memory_order) const` | strata.exe | unique | 51935 | 0.03254248035921735 | 0.02599295713565436 |
| 7 | `<no symbol>` | 0 | `<no symbol>` | claude.exe | unresolved | 2370 | 0.001485042427098202 | 0.0234032598989852 |
| 8 | `<no symbol>` | 0 | `<no symbol>` | amdhip64_7.dll | unresolved | 8780 | 0.00550154958224566 | 0.014193052446381997 |
| 9 | `...\include\atomic` | 916 | `std::_Atomic_storage<unsigned int, 4>::load(enum std::memory_order) const` | strata.exe | unique | 26367 | 0.016521566951602655 | 0.013299343380525852 |
| 10 | `...\include\atomic` | 708 | `std::_Atomic_storage<bool, 1>::load(enum std::memory_order) const` | strata.exe | unique | 26072 | 0.016336719898440644 | 0.013163138942743349 |
| 11 | `...\include\atomic` | 705 | `std::_Atomic_storage<bool, 1>::load(enum std::memory_order) const` | strata.exe | unique | 26158 | 0.016390607513938722 | 0.013036231511518157 |
| 12 | `<no source line>` | 0 | `KiBoundFault` | ntoskrnl.exe | unique | 1463 | 0.00091671606364754 | 0.01270615271079312 |
| 13 | `<no symbol>` | 0 | `<no symbol>` | libcef.dll | unresolved | 363 | 0.0002274558654163069 | 0.012375362787863855 |
| 14 | `<no source line>` | 0 | `MiValidFault` | ntoskrnl.exe | unique | 855 | 0.0005357431540797311 | 0.008623283101804544 |
| 15 | `<no source line>` | 0 | `KiScheduleThreadToRescheduleContext` | ntoskrnl.exe | unique | 487 | 0.00030515428776237315 | 0.007706825836044 |
| 16 | `<no source line>` | 0 | `MiFlushTbList` | ntoskrnl.exe | unique | 1333 | 0.0008352580402202124 | 0.007351824060128985 |
| 17 | `<no source line>` | 0 | `MiGetPage` | ntoskrnl.exe | unique | 443 | 0.00027758387983312385 | 0.006995113864476481 |
| 18 | `<no source line>` | 0 | `CmpDoParseKey` | ntoskrnl.exe | unique | 1549 | 0.0009706036791456181 | 0.006086308646028924 |
| 19 | `<no source line>` | 0 | `MiInsertPagesInList` | ntoskrnl.exe | unique | 9995 | 0.006262868801201067 | 0.005999875979772568 |
| 20 | `...\amd\memcpy_repmovs_amd.asm` | 21 | `memcpy_repmovs_amd` | vcruntime140.dll | unique | 3341 | 0.0020934712020823176 | 0.005911077587523921 |
| 21 | `<other lines>` | NULL | NULL | NULL | NULL | 938337 | 0.5879621332979095 | 0.5737108703880413 |

Module names are the short forms the output prints; `...\` abbreviates the MSVC 14.51 include path
and the vcruntime amd64 asm path. The full paths are in the `file` column on the left.

The pre-fix `iq_avx512.cpp:174` row (111,806 samples, `time_share` 0.014741379847085119) is absent.
Line 174 is the `Fmt<T>::decode` call site inside the `row_dot<T>` body: for hot
`row_dot<21,2>` addresses, `Symbol[0]` is `Fmt<21>::decode` at `iq_avx512.cpp:135/148` and
`Symbol[last]` is `row_dot<21,2>` at line 174, so those samples move to the inlinee's executed
lines. The remainder arithmetic confirms it: 938,337 = 829,872 (pre-fix remainder) + 111,806
(that row) - 3,341 (`memcpy_repmovs_amd` moved into rank 20). Do not use the old row as a
regression number.

## Capture-mode fixtures

Status by capture mode:

| capture | status |
|---|---|
| supported cycle event | verified |
| other PMC event | not established |
| IBS OP, cycle interval | implemented; verified on an external cycle-controlled capture |
| IBS OP, op interval | verified |
| IBS fetch | supported, unverified |

Commands: `python tools/uprof.py profile --session <path> --by <granularity> --top all`. Row counts
are the uncapped views.

| Fixture | Path | Capture | Rows (process/function/line/instruction) | Shares | Column set |
|---|---|---|---:|---|
| `icf-session` | `%TEMP%\uprof-fixture\icf-session` | IBS OP, no tag column | 1/8/9/13 | `sample_share`, `time_share` 1.0 | samples, sample_share, time_share; `TAG_TO_RETIRE_SHARE` names the missing `IbsTagToRetCtr` |
| `pmc-session` | `%TEMP%\uprof-fixture\pmc-session` | PMC `CYCLES_NOT_IN_HALT` | 1/6/12/27 | `sample_share` 1.0 | samples, sample_share only; TIME_SHARE/TAG_TO_RETIRE_SHARE "not applicable" |
| `ibs-session` | `%TEMP%\uprof-fixture\ibs-session` | IBS OP, tagged | 57/4249/1606/6915 | all three shares 1.0 | adds `tag_to_retire_share` |
| `fetch-session` | `%TEMP%\t1-round3\fetch-session` | IBS fetch | 1/8/9/13 | `sample_share`, `time_share` 1.0 | no `tag_to_retire_share`; `TAG_TO_RETIRE_SHARE` says this export has no tag-to-retire counter |
| `t1-fold-session` | `%TEMP%\t1-fold-session` | IBS OP, no tag | 1/1/1/1 | `sample_share`, `time_share` 1.0 | line row `shared: fold_alpha \| fold_beta`, attribution `shared` |
| `t1-nopdb-session` | `%TEMP%\t1-nopdb-session` | IBS OP, no tag, no PDB | 1/1/1/1 | `sample_share`, `time_share` 1.0 | `<no symbol>` rows, attribution `unresolved`; AGREEMENT `unresolved=1 addresses, 3 samples (100.0% of 3 scoped)` |

`icf-session` line detail: `mixed.cpp:8` and `:9` are shared-fold rows
(`shared: fold_alpha \| fold_beta`, 2 samples each); `mixed.cpp:20` is the standalone
`mix_inline(int)` copy (8 samples) and `mixed.cpp:12/13/14` are `plain_mul` / `plain_xor` /
`plain_volatile`, all `unique`. Instruction detail: `0x1140/0x1145/0x1155` are inline instances
(`mix_inline`, line 20), `0x1070/0x1075` the standalone `mix_inline(int)` copy, `0x1150`
`call_inline_b`, and `0x1160/0x1163` shared fold rows. AGREEMENT:
`mismatch_rate=16.666666666666668% (4/24); name_mismatches=2 addresses of 13 compared; unresolved=0`.
`edge.cpp:1` and `overload.cpp:5` line rows carry `<multiple>` for the overload set.

### Tagged IBS and `tag_to_retire_share`

`ibs-session` has 59,243 samples on 16 CPUs. The default (top 20) run returns 20 rows plus one
`<other processes|functions|lines|instructions>` remainder whose identifying columns are NULL;
the three shares still sum to 1.0 at every granularity. `tag_to_retire_share` is the share of
`min(IbsTagToRetCtr, 65535)`, the 16-bit field's maximum; only an unsigned decimal integer cell
counts, so NULL, negative, fractional and exponent cells are excluded from its denominator.

- Clip: a two-row fixture with tags 200000 and 1000 gives `65535/66535 = 0.9849703163748403`
  and `1000/66535 = 0.01502968362515969`; a tag above 65535 is charged 65535. No real capture in
  the fixture set has a value above 65535, so the clip is exercised synthetically.
- All-zero column: a column of zeros is non-null, so `tag_to_retire_share` is present but every
  share is NULL; the column does not sum to 1.0 because no tag cycles were counted.
- NULL tag: appending an all-blank `IbsTagToRetCtr` to `icf-session` keeps exit 0, omits
  `tag_to_retire_share`, and prints `IbsTagToRetCtr has no non-null values`. Appending mixed junk
  and numeric cells emits `tag_to_retire_share`; instruction `0x1170` is
  `8/131130 = 0.0000610081598413787844`, whose denominator 131,130 = 5 + 7 + 65535 + 65535 +
  16 × 3, so `abc`, `1.5`, `-4` and `1e5` are excluded rather than charged the maximum.

### PMC behavior

`pmc-session` has 15,323 samples: the four granularities above print `samples` and `sample_share`
only, totals 1.0, `TIME_SHARE` / `TAG_TO_RETIRE_SHARE` say "not applicable", and `--time-filter`
exits 2 with `--time-filter cannot be applied to a PMC capture`.

Live unelevated path, reproduced 2026-11-07 on `fold_busy.exe`:

1. `collect --out-dir <dir> --process <pid> --seconds 1 --sample-interval 200us` printed
   `SAMPLE_INTERVAL=200us -> 940000 cycles at 4700 MHz` and `Elevated=False`; the session records
   `hwId=118 interval=940000`.
2. `translate --session <live> --out-dir <copy>` wrote `cpu.db`.
3. `profile --session <copy> --by process|function` printed `METRIC: ... interval 940000`, PMC
   column set only, and 5,726 samples for the captured PID.

IBS fallback on this host: `collect --event ibs-op --seconds 1` printed
`SAMPLER: falling back to cycle sampling (time-based); IBS unavailable: VBS is running
(Win32_DeviceGuard VirtualizationBasedSecurityStatus 2), so IBS ...; remedy: ...`,
then captured PMC at the 1 ms default (`interval=4700000`); after `translate`, `profile` showed
the PMC column set and 1,143 samples. The fallback attaches to the target process, so the target
must still be alive when the fallback collect runs.

### Dump generation and the cache

Cache key: `sha256(normcase(abspath(session)))[:16]` under `%TEMP%\uprof-dump-cache` (Linux `~/.cache/amd-uprof/uprof-dump-cache`).
`profile` on a session with no dump beside it prints
`DUMP: <cache>\IbsOpDump.csv, <size> MB; cache <cache> generated (no cached dump for this session)`
and `GENERATING DUMP: <cache> ...` on stderr; the source session is untouched.

`profile --time-filter` filters the dump in SQL and never re-translates: a generated cache for a
raw copy recorded `timeFilter: ""` and held the full 1,595,914-row dump (294.6 MB) even though
the table was computed for 0:2. The cache's `timeFilter` comes from the session's recorded
`translateCommand`. To produce a genuinely windowed dump, translate explicitly:
`translate --session <copy> --out-dir <out> --time-filter 0:2 --ascii ibsop-event-dump`; on the
archive copy this wrote 59,243 samples (11,381,666 bytes) in 67.7 s, and `profile` on it printed
`SANITY ... matches window 2.000 s`.

## Instruction context and `context_help` (2.1.0)

Validated 2026-11-08 on Windows 11 with LLVM 23.1.4 on `PATH`, using the in-repo PE fixture and
the unit suite. No live capture is needed: the block is built from PE headers plus the capture's
own rows, and the fixture is a real linked image.

`python tests/run_tests.py` runs the full suite.
`python tests/run_tests.py test_context_fixture` runs `llvm-readobj`, `llvm-objdump` and
`llvm-symbolizer` end to end against `tests/fixtures/fixture.exe` (2560 bytes, nonzero `CheckSum`,
`.text` at 0x1000, `.pdata` at 0x3000, hot boundary 0x101B, IAT slot 0x20A0 for
`KERNEL32.dll!ExitProcess`).

- **Anchor.** The fixture's 0x101B row anchors on export `fixture_entry` at 0x1000 with
  `distance` 27. A public start names the export when the export table supplies it; a hot RVA
  under a `.pdata`-only start reports `kind: "pdata"`; no safe start within 1 MiB reports
  `anchor_too_far`; a mid-instruction RVA (0x1001) reports
  `hot_rva_not_instruction_start`.
- **Build identity.** A matching `CheckSum` gives `verified: true`, `identity: "checksum"`; a
  mismatch reports `build_mismatch`; a zero checksum without size evidence gives
  `verified: false`, `identity: "unverified"` and a warning; a matching recorded `SizeOfImage`
  gives `identity: "size"`.
- **Omission build evidence.** An omission raised after the module was examined carries the same
  `build` object as an emitted block. The fixture's off-boundary row shows `identity: "checksum"`;
  a synthetic zero-checksum capture with a differing recorded `SizeOfImage` shows
  `verified: false`, `identity: "unverified"` and the size-mismatch warning on the
  `hot_rva_not_instruction_start` row. A `build_mismatch` omission carries the same shape and
  names the checksum in `detail` and `warning`.
- **Share.** `instruction_share(3, 60)` is `0.05`, and `instruction_share(3, 0)` is `null`; the
  fixture's 0x101B instruction has 4 of 10 scoped samples and reports `sample_share` `0.4`. This
  is the row's fraction and the row's mode-specific meaning, not a percent or a second scale.
- **IBS tag policy.** The expression is
  `LEAST(TRY_CAST("IbsTagToRetCtr" AS BIGINT), 65535)` and only a `\s*[0-9]+\s*` cell counts.
  The clip and mixed-junk arithmetic above remains the evidence: 200000 and 1000 give
  0.9849703163748403 and 0.01502968362515969; a mixed column excludes `abc`, `1.5`, `-4` and
  `1e5` rather than charging 65535. IBS values are emitted as AMD-encoded counts or histograms.
- **`context_help`.** Fixed key order `scope`, `share`, `fields`, `omission_reasons`, `read`,
  `next`; repeated calls serialize identically; the text contains no `pct` name, no percent
  scaling and no doubled 16-bit maximum; `row.attribution` and the tag-to-retire reading appear
  only when the legend carries those columns; every omission reason the code can emit has a
  meaning; `scope` says the cap counts row slots and an omitted row still consumes one;
  `instructions` says the text may keep objdump's nearest-symbol decoration and directs readers
  to `symbol`; `instructions[].ibs` says the values are AMD-encoded, not decoded. Markdown contains
  no help text.
- **Document shape.** Key order is `METRIC`, `legend`, `context_help`, `rows`; `context_help`
  appears exactly once; a document without the feature is unchanged, and Markdown bytes carry no
  context or help.

## Historical module scope (not reachable from the current surface)

The former `interval --module` scope filtered samples by module. Those figures remain exact for
the same estimator, but no `profile` argument expresses them:

| Scope | Value | Denominator |
|---|---:|---|
| full 60 s, `module_name ILIKE '%strata.exe'` | 54.01229618952104 | 1,435,369 samples / 396,354,951,724 ns |
| 0:10, `module_name ILIKE '%strata.exe'` | 54.88033451498129 | 268,754 samples / 72,566,135,203 ns |

Reproduce by running `views.sql` with the same variables and adding
`AND w.module_name ILIKE '%strata.exe'` to `v_scoped`; reproduced 2026-11-06 with the figures
above. `--image` is a process-image filter, not a module filter.

## Window weighting (rejected)

The `D * count / sum(count) over windowId` estimator was removed; do not reintroduce it. Against
exact DeltaTick gaps on the 60 s capture, the worst per-group share difference was 23.46 pp at
4–8 ms windows; it reported per-process wall time rather than core time summed over CPUs; and it
changed the ranking.

## Sanity check revision (2026-11-07)

The former `sum(weight)` versus `elapsed x CPUs` check with a 2.0% tolerance failed load-dependent
fractions of good captures: the deficit is exactly the per-CPU boundary gaps (first gap plus the
time after each CPU's last sample), 0.04-0.56 s on busy CPUs but 1.6-9.9 s on idle ones. Re-judged
on the reference host with the rebuilt `v_dump_sanity`:

| capture | mode | samples | coverage | verdict |
|---|---|---:|---|---|
| `loop-2mib-swwide` | system-wide 45 s | 9936 | 712.079 / 721.104 s | OK, boundary 9.0 s |
| `loop-16mib-swwide` | system-wide 45 s | 5784 | 704.696 / 720.944 s | OK, boundary 16.2 s |
| `loop-16mib-swwide2` | system-wide 75 s | 7947 | 1150.309 / 1200.464 s | OK, boundary 50.2 s |
| `loop-2mib-nohash` | system-wide 45 s | 3373 | 683.356 / 720.304 s | OK, boundary 36.9 s |
| `loop-2mib-ibs` | attached 39.682 s | 954 | 40.011 s thread time over 52 TIDs; busiest TID covers 39.386 s | OK (attached; dump-wide) |

Previously these five verdicts were −1.25% (pass), −2.25%, −4.18%, −5.13% and −28.46% (four
rejected). The attached −28.46% was the retired per-CPU comparison: 454.203 s of per-CPU weight,
the overcount this revision fixes, against `elapsed x CPUs` = 634.912 s. It was never a coverage
reading, and the attached verdict no longer computes a CPU capacity. The attached capture is now
judged as its 40.011 s thread-time total over 52 TIDs (the first sample per thread carries no
weight; the sum over threads may exceed the span because independent threads overlap), with no
implied bound. Each verdict was reproduced through `uprof_db.open_query` with a synthetic
`session.uprof`.

Synthetic DuckDB fixtures cover the branches directly: a dump whose weight stays below the
capacity bound but whose samples run outside the recorded elapsed (0.010-1.600 s against 1.000 s)
errors with `1 CPU(s) hold samples outside the recorded elapsed [0.000, 1.000) s`; 45 s of samples
against a 90 s span prints the late-window `WARN`; a session with no duration warns; an empty dump
errors; and a recorded `--time-filter 10:12` on a 60 s capture is judged against `[10, 12) s`, not
the 2 s width, reporting `OK` with leading 0.300 s and trailing 1.200 s gaps.

The `--by function --time-filter 1:39.5 --image '%storage_test%' --top 25` rows on
`loop-2mib-swwide` are byte-identical to the pre-change baseline (`baseline-loop2-function.txt`);
only the SANITY, ACHIEVED and KERNEL_COVERAGE header lines differ. That capture was taken
unelevated, so its OS-mode request yielded no kernel-image sample.

Anchoring, verified with `uprof.py anchor`: attached BEGIN −0.700 s, system-wide BEGIN −0.151 s,
system-wide END +40.182 s (the capture starts during the warm-up pass, so the loop's BEGIN precedes
it by the attach latency).

Achieved rate on the same captures, rerun with the busiest-context check. The per-CPU average
divides by all 16 dump CPUs, so idle CPUs dilute it; the busiest context is a CPU for a
system-wide capture and a TID for the attached one:

| capture | per-CPU average | busiest context | active contexts |
|---|---:|---:|---:|
| `loop-2mib-swwide` | 13.78/s | CPU 1,110/s (median gap 901 us) | 12 |
| `loop-16mib-swwide` | 8.02/s | CPU 1,067/s (median 937 us) | 10 |
| `loop-16mib-swwide2` | 6.62/s | CPU 557/s (median 1.8 ms) | 10 |
| `loop-2mib-nohash` | 4.68/s | CPU 722/s (median 1.39 ms) | 7 |
| `loop-2mib-ibs` | 1.50/s | thread 409/s (median 2.44 ms) | 1 |

None prints the below-10% `WARN`. Under the previous whole-capture average all five did, although
every busiest context stayed within 10x of the requested 1 ms interval; the attached capture's
denominator even included CPUs it never ran on (1 active context, 16 CPUs in the dump). The
attached thread's running bursts were about 0.9 ms apart (about 1,070/s); the all-gap median of
2.44 ms includes descheduling gaps below the 10x cut and is therefore a lower bound on delivered
cadence.

## Achieved-rate and command-surface evidence (2026-11-07)

The five `profile --by process --top 1` runs above used the translated `-x` copies of the capture
directories, which hold `cpu.db`; the raw directories hold none. Each printed an ACHIEVED line with
the rates listed above.

PMC, `knee-2mib-x`, `profile --by process --top 1`:

`ACHIEVED: 181982 samples, 30.058 s, 16 CPUs, 378.40/s per CPU (requested 4700000 cycles at 4700 MHz = 1 ms; ideal 1,000/s per CPU); busiest core 536.16/s; 16 active cores`

`UnifiedSampleSeries` holds 175,260 rows over 16 `coreId` values; the event column sums to 181,982
samples. The same line appears with `--image '%fold_busy%'`, whose scope matches nothing (the
separate `WARNING: no samples in the selected scope` remains), because the count is capture-level.
PMC has no per-sample timestamps, so the busiest core's 536.16/s is the delivered cadence: the
busiest context's samples divided by the capture window.

Translate, on `loop-2mib-ibs` to a new temporary copy:

`translate --session <loop-2mib-ibs> --out-dir <new copy> --ascii ibsop-event-dump` ends with
`ACHIEVED: 954 samples, 39.682 s, 16 CPUs, 1.50/s per CPU (requested 4700000 cycles at 4700 MHz = 1 ms; ideal 1,000/s per CPU); busiest thread 409/s (median gap 2.44 ms); 1 active thread`.
Without `--ascii` it prints `ACHIEVED: reported at profile time (translate wrote no dump without
--ascii)`. The line is computed by the same `dump_cadence`/`achieved_summary` functions the
`profile` run above used on the identical 954-sample dump.

Command surface, recorded 2026-11-07:

- `tools`: `UPROF=...AMDuProfCLI.EXE; version=AMDuProfCLI.exe Version 5.3.521.0`;
  `DUCKDB=duckdb 2.0.0 (python); version=2.0.0`; symbolizer, pdbutil and readobj
  `LLVM version 23.1.4`.
- `collect --dry-run --out-dir <new path>`: `SAMPLE_INTERVAL=1ms -> 4700000 cycles at 4700 MHz
  (source: Windows registry HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0\~MHz)`;
  `EVENT=event=CYCLES_NOT_IN_HALT,interval=4700000,user=1,os=1`; `COMMAND=...`; `Elevated=False`;
  `DRYRUN=1`. No capture ran.
- `schema --session <loop-2mib-swwide-x>`: exit 0, a 151-row live table/column listing over the
  expected tables (`Function`, `Module`, `Process`, `UnifiedSampleSeries`, ...).

Anchor exact values: `loop-2mib-ibs --qpc 42140285053 --freq 10000000` printed
`RELATIVE_S=-0.699788599`; `loop-2mib-swwide --qpc 43099238262 --freq 10000000` printed
`RELATIVE_S=-0.150568799`. Without `--freq`, `HZ_SOURCE=default (10 MHz; the session records no QPC
frequency)`.

## Kernel-coverage evidence (2026-11-07)

An unelevated 15 s system-wide PMC capture of `user=1,os=1` (requested through `collect`,
`Elevated=False`) held 21,489 samples and none in a kernel image; `Module` had no `.sys` row with a
sample and one `ntoskrnl.exe` row with zero. The elevated `kernel-2mib` capture of the same event
held 56,605 samples, 45,489 of them in 67 kernel images (66 `.sys` plus `ntoskrnl.exe` 32,078;
`dxgmms2.sys` 7,458; 247 `.sys` images are present in the module table). Translating that raw
capture from the same unelevated account reproduced the 45,489 kernel samples in the same 67
images, so the loss is at capture time. An unelevated `user=0,os=1` 2 s capture held 4 samples, all
user-mode: a kernel-only request does not fall back to user-mode sampling.

The counts come from the event column grouped by `Module.path` through DuckDB
(`SELECT m.path, sum(s."event#...") FROM UnifiedSampleSeries s JOIN Module m ON m.id = s.moduleId
GROUP BY 1`); the classifier for this measurement was basename-based. The encoded selector
(`0x03000076` = user + os + `0x76`; `user=1,os=0` gives `0x01000076`, `user=0,os=1` gives
`0x02000076`) and the raw-metadata comparison are part of the issue #17 RCA.
