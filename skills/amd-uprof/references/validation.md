# Validation evidence

Measured acceptance figures, their capture, filter and estimator. A regression baseline, not
requirements. The estimator itself is defined in `reference.md`; the figures below are the ones
the current `profile` surface can reproduce.

## Estimator (defined in reference.md)

The estimator, its window-before-lag rule and gap charging are in `reference.md`, "Estimator: exact
DeltaTick gaps" (Hofer, JKU PhD thesis, section 3.2.4); `pct_samples` is the raw dump sample share,
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
| `--by function --image '%strata.exe'` | `ExpertPool::worker` `pct_time` | 45.98452886467337 | process image, whole capture: 1,496,649 samples / 465,548,774,278 ns |
| same | `ExpertPool::worker` `pct_samples` | 29.659726495657967 | same |
| same | `ExpertPool::worker` `pct_cycles` | 7.919207413573721 | same |
| `--by function --image '%strata.exe' --time-filter 0:10` | `ExpertPool::worker` `pct_time` | 47.746537148319796 | process image, 0:10: 280,047 samples / 83,408,222,088 ns |
| same | `ExpertPool::worker` `pct_samples` | 29.000132120679744 | same |
| same | `ExpertPool::worker` `pct_cycles` | 7.63744218348601 | same |
| `--by function` | `ExpertPool::worker` `pct_time` | 22.318753474451373 | whole capture: 1,595,914 samples / 959,195,193,101 ns |
| `--by process` | `strata.exe` (pid 5932) `pct_time` | 48.53535314047172 | whole capture: 1,595,914 samples / 959,195,193,101 ns |
| same | `strata.exe` `pct_samples` / `pct_cycles` | 93.78005331114333 / 90.92966743904525 | same |
| `--by process --time-filter 0:10` | `strata.exe` `pct_time` | 52.26572213110555 | 0:10 window: 296,917 samples, 16 first-in-window |

Every granularity on the whole capture sums to 100: process, function and instruction
`pct_samples`, `pct_time` and `pct_cycles` are 100.0; line `pct_cycles` is 99.99999999999999
(floating-point sum). Denominators read from `v_scoped` with the same inputs: whole capture
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

| # | file | line | function | module | attribution | samples | pct_samples | pct_time |
|---:|---|---:|---|---|---|---:|---:|---:|
| 1 | `D:\Projects\strata\src\kernels\cpu\pool.cpp` | 1000 | `strata::kernels::cpu::ExpertPool::worker(int)` | strata.exe | unique | 130376 | 8.169362509508657 | 6.570961379115599 |
| 2 | `D:\Projects\strata\src\kernels\cpu\pool.cpp` | 1013 | `strata::kernels::cpu::ExpertPool::worker(int)` | strata.exe | unique | 130425 | 8.172432850391688 | 6.5628702354611885 |
| 3 | `D:\Projects\strata\src\kernels\cpu\pool.cpp` | 1001 | `strata::kernels::cpu::ExpertPool::worker(int)` | strata.exe | unique | 130746 | 8.192546716176436 | 6.55713460277707 |
| 4 | `...\include\atomic` | 917 | `std::_Atomic_storage<unsigned int, 4>::load(enum std::memory_order) const` | strata.exe | unique | 52512 | 3.2904028663198646 | 2.6435205718686334 |
| 5 | `D:\Projects\strata\src\kernels\cpu\pool.cpp` | 1014 | `strata::kernels::cpu::ExpertPool::worker(int)` | strata.exe | unique | 52007 | 3.2587595572192485 | 2.6100453829488544 |
| 6 | `...\include\atomic` | 706 | `std::_Atomic_storage<bool, 1>::load(enum std::memory_order) const` | strata.exe | unique | 51935 | 3.254248035921735 | 2.599295713565436 |
| 7 | `<no symbol>` | 0 | `<no symbol>` | claude.exe | unresolved | 2370 | 0.1485042427098202 | 2.34032598989852 |
| 8 | `<no symbol>` | 0 | `<no symbol>` | amdhip64_7.dll | unresolved | 8780 | 0.550154958224566 | 1.4193052446381997 |
| 9 | `...\include\atomic` | 916 | `std::_Atomic_storage<unsigned int, 4>::load(enum std::memory_order) const` | strata.exe | unique | 26367 | 1.6521566951602655 | 1.3299343380525852 |
| 10 | `...\include\atomic` | 708 | `std::_Atomic_storage<bool, 1>::load(enum std::memory_order) const` | strata.exe | unique | 26072 | 1.6336719898440644 | 1.3163138942743349 |
| 11 | `...\include\atomic` | 705 | `std::_Atomic_storage<bool, 1>::load(enum std::memory_order) const` | strata.exe | unique | 26158 | 1.6390607513938722 | 1.3036231511518157 |
| 12 | `<no source line>` | 0 | `KiBoundFault` | ntoskrnl.exe | unique | 1463 | 0.091671606364754 | 1.270615271079312 |
| 13 | `<no symbol>` | 0 | `<no symbol>` | libcef.dll | unresolved | 363 | 0.02274558654163069 | 1.2375362787863855 |
| 14 | `<no source line>` | 0 | `MiValidFault` | ntoskrnl.exe | unique | 855 | 0.05357431540797311 | 0.8623283101804544 |
| 15 | `<no source line>` | 0 | `KiScheduleThreadToRescheduleContext` | ntoskrnl.exe | unique | 487 | 0.030515428776237315 | 0.7706825836044 |
| 16 | `<no source line>` | 0 | `MiFlushTbList` | ntoskrnl.exe | unique | 1333 | 0.08352580402202124 | 0.7351824060128985 |
| 17 | `<no source line>` | 0 | `MiGetPage` | ntoskrnl.exe | unique | 443 | 0.027758387983312385 | 0.6995113864476481 |
| 18 | `<no source line>` | 0 | `CmpDoParseKey` | ntoskrnl.exe | unique | 1549 | 0.09706036791456181 | 0.6086308646028924 |
| 19 | `<no source line>` | 0 | `MiInsertPagesInList` | ntoskrnl.exe | unique | 9995 | 0.6262868801201067 | 0.5999875979772568 |
| 20 | `...\amd\memcpy_repmovs_amd.asm` | 21 | `memcpy_repmovs_amd` | vcruntime140.dll | unique | 3341 | 0.20934712020823176 | 0.5911077587523921 |
| 21 | `<other lines>` | NULL | NULL | NULL | NULL | 938337 | 58.79621332979095 | 57.37108703880413 |

Module names are the short forms the output prints; `...\` abbreviates the MSVC 14.51 include path
and the vcruntime amd64 asm path. The full paths are in the `file` column on the left.

The pre-fix `iq_avx512.cpp:174` row (111,806 samples, `pct_time` 1.4741379847085119) is absent.
Line 174 is the `Fmt<T>::decode` call site inside the `row_dot<T>` body: for hot
`row_dot<21,2>` addresses, `Symbol[0]` is `Fmt<21>::decode` at `iq_avx512.cpp:135/148` and
`Symbol[last]` is `row_dot<21,2>` at line 174, so those samples move to the inlinee's executed
lines. The remainder arithmetic confirms it: 938,337 = 829,872 (pre-fix remainder) + 111,806
(that row) - 3,341 (`memcpy_repmovs_amd` moved into rank 20). Do not use the old row as a
regression number.

## Capture-mode fixtures

Commands: `python tools/uprof.py profile --session <path> --by <granularity> --top all`. Row counts
are the uncapped views.

| Fixture | Path | Capture | Rows (process/function/line/instruction) | Shares | Column set |
|---|---|---|---:|---|
| `icf-session` | `%TEMP%\uprof-fixture\icf-session` | IBS OP, no tag column | 1/8/9/13 | `pct_samples`, `pct_time` 100 | samples, pct_samples, pct_time; `PCT_CYCLES` names the missing `IbsTagToRetCtr` |
| `pmc-session` | `%TEMP%\uprof-fixture\pmc-session` | PMC `CYCLES_NOT_IN_HALT` | 1/6/12/27 | `pct_samples` 100 | samples, pct_samples only; PCT_TIME/PCT_CYCLES "not applicable" |
| `ibs-session` | `%TEMP%\uprof-fixture\ibs-session` | IBS OP, tagged | 57/4249/1606/6915 | all three shares 100 | adds `pct_cycles` |
| `fetch-session` | `%TEMP%\t1-round3\fetch-session` | IBS fetch | 1/8/9/13 | `pct_samples`, `pct_time` 100 | no `pct_cycles`; `PCT_CYCLES` says IBS fetch has no tag counter |
| `t1-fold-session` | `%TEMP%\t1-fold-session` | IBS OP, no tag | 1/1/1/1 | `pct_samples`, `pct_time` 100 | line row `shared: fold_alpha \| fold_beta`, attribution `shared` |
| `t1-nopdb-session` | `%TEMP%\t1-nopdb-session` | IBS OP, no tag, no PDB | 1/1/1/1 | `pct_samples`, `pct_time` 100 | `<no symbol>` rows, attribution `unresolved`; AGREEMENT `unresolved=1 addresses, 3 samples (100.0% of 3 scoped)` |

`icf-session` line detail: `mixed.cpp:8` and `:9` are shared-fold rows
(`shared: fold_alpha \| fold_beta`, 2 samples each); `mixed.cpp:20` is the standalone
`mix_inline(int)` copy (8 samples) and `mixed.cpp:12/13/14` are `plain_mul` / `plain_xor` /
`plain_volatile`, all `unique`. Instruction detail: `0x1140/0x1145/0x1155` are inline instances
(`mix_inline`, line 20), `0x1070/0x1075` the standalone `mix_inline(int)` copy, `0x1150`
`call_inline_b`, and `0x1160/0x1163` shared fold rows. AGREEMENT:
`mismatch_rate=16.666666666666668% (4/24); name_mismatches=2 addresses of 13 compared; unresolved=0`.
`edge.cpp:1` and `overload.cpp:5` line rows carry `<multiple>` for the overload set.

### Tagged IBS and `pct_cycles`

`ibs-session` has 59,243 samples on 16 CPUs. The default (top 20) run returns 20 rows plus one
`<other processes|functions|lines|instructions>` remainder whose identifying columns are NULL;
the three shares still sum to 100 at every granularity. `pct_cycles` is the share of
`min(IbsTagToRetCtr, 131070)`; NULL and non-integer cells are excluded from its denominator.

- Clip: a copy of `ibs-session` with one `IbsTagToRetCtr` forced to 200000 gives PID 5932
  `pct_cycles=91.524932128492`; independent CSV arithmetic with `min(tag, 131070)` gives the same
  value, while the unclipped alternative would be 91.692033796397.
- NULL tag: appending an all-blank `IbsTagToRetCtr` to `icf-session` keeps exit 0, omits
  `pct_cycles`, and prints `IbsTagToRetCtr has no non-null values`. Appending mixed junk and
  numeric cells emits `pct_cycles`; instruction `0x1170` is 0.0030511060259344014, whose
  denominator 262,200 = 5 + 7 + 131070 + 131070 + 16 × 3, so `abc`, `1.5`, `-4` and `1e5` are
  excluded rather than charged the maximum.

### PMC behavior

`pmc-session` has 15,323 samples: the four granularities above print `samples` and `pct_samples`
only, totals 100, `PCT_TIME` / `PCT_CYCLES` say "not applicable", and `--time-filter` exits 2
with `--time-filter cannot be applied to a PMC capture`.

Live unelevated path, reproduced 2026-11-07 on `fold_busy.exe`:

1. `collect --out-dir <dir> --process <pid> --seconds 1 --sample-interval 200us` printed
   `SAMPLE_INTERVAL=200us -> 940000 cycles at 4700 MHz` and `Elevated=False`; the session records
   `hwId=118 interval=940000`.
2. `translate --session <live> --out-dir <copy>` wrote `cpu.db`.
3. `profile --session <copy> --by process|function` printed `METRIC: ... interval 940000`, PMC
   column set only, and 5,726 samples for the captured PID.

IBS fallback on this host: `collect --event ibs-op --seconds 1` printed
`SAMPLER: falling back to cycle sampling (time-based); IBS unavailable: hypervisor/VBS is active
(VBS running; check Win32_DeviceGuard: VirtualizationBasedSecurityStatus is 2 when running)`,
then captured PMC at the 1 ms default (`interval=4700000`); after `translate`, `profile` showed
the PMC column set and 1,143 samples. The fallback attaches to the target process, so the target
must still be alive when the fallback collect runs.

### Dump generation and the cache

Cache key: `sha256(normcase(abspath(session)))[:16]` under `<OS temp>\uprof-dump-cache`.
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

| capture | mode | samples | covered vs implied | verdict |
|---|---|---:|---|---|
| `loop-2mib-swwide` | system-wide 45 s | 9936 | 712.079 / 721.104 s | OK, boundary 9.0 s |
| `loop-16mib-swwide` | system-wide 45 s | 5784 | 704.696 / 720.944 s | OK, boundary 16.2 s |
| `loop-16mib-swwide2` | system-wide 75 s | 7947 | 1150.309 / 1200.464 s | OK, boundary 50.2 s |
| `loop-2mib-nohash` | system-wide 45 s | 3373 | 683.356 / 720.304 s | OK, boundary 36.9 s |
| `loop-2mib-ibs` | attached 39.682 s | 954 | 454.203 vs upper bound 634.912 s | OK (attached; dump-wide) |

Previously these five verdicts were −1.25% (pass), −2.25%, −4.18%, −5.13% and −28.46% (four
rejected). Each verdict was reproduced through `uprof_db.open_query` with a synthetic
`session.uprof`.

Synthetic DuckDB fixtures cover the branches directly: a dump whose weight stays below the
capacity bound but whose samples run outside the recorded elapsed (0.010-1.600 s against 1.000 s)
errors with `1 CPU(s) hold samples outside the recorded elapsed [0.000, 1.000) s`; 45 s of samples
against a 90 s span prints the late-window `WARN`; a session with no duration warns; an empty dump
errors; and a recorded `--time-filter 10:12` on a 60 s capture is judged against `[10, 12) s`, not
the 2 s width, reporting `OK` with leading 0.300 s and trailing 1.200 s gaps.

The `--by function --time-filter 1:39.5 --image '%storage_test%' --top 25` rows on
`loop-2mib-swwide` are byte-identical to the pre-change baseline (`baseline-loop2-function.txt`);
only the SANITY and ACHIEVED header lines differ.

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
