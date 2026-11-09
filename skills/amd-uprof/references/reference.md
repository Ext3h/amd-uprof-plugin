# Reference

Detail behind `SKILL.md`. The tools and `tools/views.sql` are the authority.

## Header order

Header fields are emitted in this fixed order, with optional fields omitted when they do not apply:
`VERSION`, `METRIC`, optional `FILTER` and `PARENT`, `COLUMNS`, `READ`, optional `NEXT`, `DUMP`,
optional `DB`, `TIME_SHARE`, `TAG_TO_RETIRE_SHARE`, `WEIGHTS`, optional `SANITY`, `ACHIEVED`,
`KERNEL_COVERAGE`, `HOT_RVA`, `SYMBOL_COVERAGE`, `WARNING`, `AGREEMENT`.

## Granularities

`--by` selects the row key; `function`, `region`, `line` and `instruction` symbolize.

| `--by` | row key | symbolization |
|---|---|---|
| `process` | process | no |
| `module` | module path | no |
| `function` | function (derived range when the binary supplies one) | yes |
| `region` | module + 4 KiB address page | yes |
| `line` | file:line | yes |
| `instruction` | module + RVA | yes |

## session.uprof

UTF-8 JSON (a BOM is tolerated). The keys the tools read:

| Key | Used for |
|---|---|
| `collection.events.pmcEvents.pmcEventList[]` | PMC mode: `hwId` 118, `interval`, `isSampling`, `os`, `encodedEventId` |
| `collection.events.ibsEvents.{op,opInterval,opCycleCount,fetch,fetchInterval,opOs,fetchOs}` | IBS mode, interval control and the OS-mode request bit of each sampler |
| `auxiliary.ibsOpSampling` | IBS-versus-PMC detection |
| `auxiliary.eventColumnIds[]` | event name → `event#0x<encodedId>` sample column |
| `collection.target.{systemWide,attachedProcesses[].processId}` | capture mode check |
| `collection.schedule.{elapsed,profileDuration}` | capture span for the dump sanity check |
| `collection.schedule.{start,end,startTs}` | clock anchoring: `start`/`end` are QPC-derived nanoseconds; `startTs` is local time at one-second precision |
| `translateCommand` | `--time-filter` window and the dump cache key |
| `collectCommand` | printed; fallback event name when the event list records none |

- **Event-column mapping.** `auxiliary.eventColumnIds` maps registered names to `event#0x<encodedId>`. The PMC sample column is the `CYCLES_NOT_IN_HALT` mapping; the hwId 118 `pmcEventList` entry's `encodedEventId` must equal it, or `derive_sampler` errors as inconsistent metadata. Without a mapping, the entry's `encodedEventId` is used. IBS names: `IBS_ALL_OPS` → `event#0x100000000f100`, `IBS_FETCH` → `event#0x100000000f000`.
- **PMC versus IBS.** IBS when `auxiliary.ibsOpSampling` is true, or `ibsEvents.op`/`fetch` is set; otherwise a non-empty `pmcEventList` is a PMC capture. `opCycleCount` true means a cycle-controlled interval and false an op-count interval; a sample is the operation the hardware selected, so `sample_share` is a core-cycle share for cycle-controlled, a dispatched-op frequency share for op-count, and a fetch-sample frequency share for fetch, never a per-op cost (`time_share` is the estimate from gaps; `tag_to_retire_share` is total tag-to-retire cycles, not per-op latency).
- **Dump kinds.** `ibsop-event-dump` → `IbsOpDump.csv`; `ibsfetch-event-dump` → `IbsFetchDump.csv`. `collect` does not capture IBS fetch; a fetch dump must be produced elsewhere and is supported but unverified.
- **Verified by `collect`:** PMC `hwId` 118, the converted `interval`, `isSampling` true, `auxiliary.ibsOpSampling` false; IBS `ibsEvents.op`, `ibsOpSampling`, `opInterval`, `opCycleCount` against `--ibs-count-control`; `collection.target.systemWide` against the mode, and every requested PID in `attachedProcesses`. A mismatch exits 2.

## Estimator: exact DeltaTick gaps

`AMDuProfCLI translate --ascii ibsop-event-dump -i <copy>` writes the dump; `ibsfetch-event-dump` writes the fetch dump. One row is one sample. Used columns: `DeltaTick`, `CPU_Number`, `TID`, `PID`, `Offset`, `ModuleName`. `DeltaTick` is a cumulative, start-relative nanosecond timestamp (QPC-derived, 100 ns grid).

Per Hofer, JKU PhD thesis, section 3.2.4:

1. Partition by execution context: `CPU_Number` for a system-wide capture, `(PID, TID)` for an
   attached one. A thread's samples share a partition across every CPU it ran on.
2. Order each partition by `DeltaTick`.
3. Weight each sample by `DeltaTick - lag(DeltaTick)`; the first sample per context is ignored,
   having no gap before it.
4. A group's share is `sum(weight in the group) / sum(weight in scope)`.

The partition is the difference between the capture modes. A system-wide gap is the shared CPU
timeline, so per-CPU partitions are disjoint. An attached thread that migrates leaves a gap on
each CPU it visited, and those per-CPU gaps can cover the same wall-clock interval; partitioning
by CPU would charge that interval once per CPU and inflate the total past the capture span.

Rules that follow:

- `--time-filter T1:T2` selects `DeltaTick ∈ [T1, T2)` **before** the lag, so the first sample per
  context inside the window is ignored, having no gap before it; filtering never re-translates.
  The first-per-context sample still counts in `sample_share`.
- System-wide, a gap spans all work on that CPU since the previous sample, so halted/idle time is
  charged to the later sample: `system idle process` and `system` carry share. A process total is
  core time summed over CPUs (10 s on 16 CPUs is about 160 s of weight). That normalization
  applies only to a system-wide capture.
- Attached, a gap is the thread's own interval since its previous sample and may cover time it
  was off-CPU; partitions do not overlap, so a migrating thread is counted once and its total
  cannot exceed the capture span. A single thread's total is its elapsed lifetime, not pure CPU
  time. For a cycle-controlled IBS capture every sample carries the same fixed cycle count, so
  `sample_share` is the CPU-cycle share and the better ranking; for op-count and fetch,
  neither share is CPU time.
- `(ModuleName, Offset)` maps to `Function.id` through the `UnifiedSampleSeries` rows that carry both; `Offset` is module-relative, and `ModuleName` matches `Module.path` after `module_key` (lowercase, `/` separators). The scope filter runs after weighting, so a gap is charged whole to the sample that ends it.
- `samples` for a dump is the row count (one per dump row); for PMC it is `sum(event column)`, not `count(*)`.
- `share(part, whole) = part / NULLIF(whole, 0)`: every share is a fraction 0.0-1.0, printed at full precision. The SQL denominator is the same sum over the scope, so each emitted share sums to 1.0 when it has at least one non-null value. An all-zero `IbsTagToRetCtr` is non-null but sums to 0, so every `tag_to_retire_share` is NULL and the column does not sum to 1.0.
- `tag_to_retire_share = Σ LEAST(tag, 65535)` for the group divided by the same sum over the scope. The field is a 16-bit counter, so 65535 is its maximum and the clip; it counts cycles from the op's dispatch tag to retirement, so it includes scheduler/queue occupancy and stalls. The value is a total of tag-to-retire cycles, not per-op latency. Only an unsigned decimal integer cell counts; NULL, negative, fractional and exponent spellings stay NULL and are excluded from both numerator and denominator (never charged the maximum). The projection emits the column only for an IBS OP dump whose `IbsTagToRetCtr` exists and has at least one non-null value.

### Anchoring a workload's clock

`collection.schedule.start` and `end` are QPC-derived nanoseconds (their difference equals
`elapsed` to the digit), and a sample's `DeltaTick` is `sample_ns - start`. A workload that prints
its own `QueryPerformanceCounter` reading can therefore be placed on the capture timeline with
`uprof.py anchor --session S --qpc <ticks> --freq <hz>`:

    relative_s = (qpc * 1e9 / freq - start) / 1e9

A negative value is attach latency: the reading precedes the capture start, so the capture does not
cover it (measured -0.15 s system-wide and -0.70 s attached on the reference host). `startTs`
(local time, one-second precision) gives the UTC start where a wall-clock comparison is needed;
`--time-filter` takes the same capture-relative seconds.

The session records no QPC frequency — only the core MHz used for the collect interval conversion
— so `--freq` defaults to 10,000,000 Hz (the Windows QPC frequency) unless it is given. Pass it
when the workload's clock differs. `HZ_SOURCE=argument` means `--freq` supplied the value;
`HZ_SOURCE=default` names the 10 MHz default.

### Rejected: window weighting

The former `D × count / Σ count over windowId` estimator was removed; do not reintroduce it. The measured deltas that motivated the removal are in `references/validation.md`, "Window weighting (rejected)".

### Dump resolution and cache

1. A dump beside the session (preferred).
2. A current cached dump under `<OS temp>/uprof-dump-cache/<sha256(normcase(abspath(session)))[:16]>/`. It is reused only when `cache.json` records the same kind and the same `translateCommand --time-filter`, and the dump is at least as new as the newest raw `cpu/*.prd`.
3. Otherwise a copy of the raw session is translated with `--ascii` in a unique staging directory, renamed into place when complete; `GENERATING DUMP:` precedes it.

The cache holds no database: every `.db`/`.duckdb` (and its WAL/SHM) is removed before a cached dump is handed back, because a dump-only translate writes a degraded `cpu.db` with far fewer `Function` rows; if the removal fails it prints `WARNING: could not remove <path>: <reason>; the cache copy still contains a database`. `cpu.db` must always come from the original session. `DUMP:` prints the path, size, cache directory and the reuse/regeneration reason; `DB:` is added when the dump directory differs from the session directory.

### Sanity check

`v_dump_sanity` runs over the whole dump, unfiltered, and its coverage verdict is dump-wide
across all processes. It raises an error only on genuine
inconsistency: `sum(weight)` greater than the span from `session.uprof` — the
`translate --time-filter` window when `translateCommand` records one, else
`collection.schedule.elapsed` (ms), else `profileDuration` (s) — times the dump's distinct CPU
count, or any sample whose `DeltaTick` lies outside that span's bounds. A recorded translate
window is compared as `[T1, T2)`, not as the width `T2 - T1` alone: `DeltaTick` stays
capture-start-relative in a windowed dump. Either condition is a dump that does not belong to
this session.

Normal captures never reach that bound. The weight cannot include each CPU's first gap or the time
after its last sample, so `implied - sum(weight)` equals those boundary gaps exactly: 0.04-0.56 s
per busy CPU but 1.6-9.9 s per idle one, which made the former 2.0% tolerance fail a load-dependent
fraction of good captures. The message reports the covered core time, the boundary gaps split
leading/trailing, the worst CPU, and `OK (dump-wide across all processes)`; it says
`WARN (dump-wide across all processes)` when the capture window ends more than one
sampling cadence before the recorded span (no CPU sampled near the end). **Attached captures**
(`collection.target.systemWide` false) partition gaps by `(PID, TID)`, not by CPU, so the verdict
is thread coverage, never a CPU capacity: `elapsed x CPUs` was meaningless for a single-thread
process and hid the per-CPU overcount. A thread holding a sample outside the recorded span is an
error, the same out-of-span error as system-wide; the last-to-first span of an in-span thread
cannot exceed the capture span by construction. The verdict says `OK (attached; dump-wide)` and
reports the process count, the TID count, the busiest TID's sample count (`898/954`), the CPUs
visited, the busiest TID's covered gap time against the span, and the total thread time over all
TIDs. It also states that `time_share` partitions gaps by `(PID, TID)` so a migrating thread is
counted once, that a thread's gaps include off-CPU time, and that for a cycle-controlled capture
`sample_share` (fixed cycles per sample) is the CPU-cycle share and the better ranking. No usable
duration gives a `WARN` and no comparison; an empty dump raises an error.

When `--image`/`--pid` or `--time-filter` narrows the report, `v_scope_sanity` appends a
`scoped coverage (<scope>)` verdict to the same field. It takes the last sample of `v_scoped` and
compares it with the end of the scope window — the recorded translate span clipped to the profile
`--time-filter` window — against the scope's own sampling cadence (the window width divided by the
busiest in-scope thread's sample count). The verdict says `WARN` and names the scope when the
scope's late time is uncovered, and `OK` when the scope sampled near the end of its window.
Attached captures, and captures whose metadata lacks `systemWide` (which counts as attached), get
no scoped verdict: `OK (attached; dump-wide)` already states that coverage is where the attached
threads ran. An empty scope gets none either; the separate no-samples warning covers it.

An empty scope prints one `WARNING: no samples in the selected scope` on stderr before the document is written, so `--json` and `--out-file` cannot make an empty filter look successful; the JSON carries the same text in `WARNING`. With an `--image` pattern, the warning names the capture's `Process.name` values and states that `--image` selects process base images, not modules; a pattern ending in `.dll`, `.sys`, `.so` or `.dylib` adds the `--by module`/`--function ... --module` remedy. On a single-process attached capture `--image` adds nothing: run the view once unscoped rather than repeating `--by function --image`.

### Achieved sample rate

`profile` and `translate --ascii` print one `ACHIEVED:` line for every capture; the tail is present
only when per-context statistics are available:

```
ACHIEVED: 9936 samples, 45.069 s, 16 CPUs, 13.78/s per CPU (requested 4700000 cycles at 4700 MHz
= 1 ms; ideal 1,000/s per CPU); busiest CPU 1,110/s (median gap 901 us); 12 active CPUs
```

Samples are counted over the whole capture, not the `--image`/`--pid` scope; CPUs come from the
dump's `CPU_Number`, or from `UnifiedSampleSeries.coreId` for a PMC capture. The per-CPU figure
divides every sample by **every** CPU in the capture, so idle CPUs and threads that ran only part
of it share the denominator: on the reference host a 1 ms system-wide request with 8 busy and 8
idle CPUs reads about 14/s per CPU although each busy CPU receives the requested cadence. That
figure is kept for continuity; it is not the delivered cadence.

The busiest context is the one with the most samples: a CPU for a system-wide dump, a `TID` for an
attached dump, a core for PMC. For a dump the line reports the median `DeltaTick` gap between
consecutive samples no more than 10x the requested interval apart (a larger gap is time the context
did not run) and the rate it implies; PMC carries no per-sample timestamps, so the busiest core's
samples/s is reported instead. "Active" counts contexts holding at least 10% of the busiest
context's samples.

`WARN: achieved sample rate is below 10% of the requested interval` fires only when the busiest
context itself is that sparse: its median running gap exceeds 10x the requested interval (or no
such gap exists) on the dump path, or its samples/s is below 10% of the ideal on the PMC path. An
average diluted by idle CPUs never warns, and neither does an intermittent attached thread whose
running gaps are at the requested interval. The two issue captures that motivated this rule: a
45 s system-wide capture delivered about 1,110/s on its busiest CPU while reading 13.78/s per CPU,
and an attached capture delivered about 1,070/s in each running burst (gaps near 0.94 ms) while its
all-gap median of 2.44 ms read 409/s. The collector's acceptance bounds the **request**, not the
delivery: IBS accepts 50,000 to 134,217,727 cycles and PMC at least 25,000 cycles, with no
millisecond-scale floor.

`collect` cannot print this — it has neither a dump nor a `cpu.db`; the rate appears at `profile`,
where the samples exist, or in `translate --ascii`, which counts the dump it just wrote. For an
op-count interval no time rate follows and the line says so.

### Kernel coverage

An OS-mode request is necessary but not sufficient for kernel coverage: an unelevated capture
contains no kernel-mode samples, and an elevated capture is required. The request is applied and
encoded — the PMC selector carries `0x03000076` for `user=1,os=1`, and IBS records `opOs`/`fetchOs`
— but the kernel samples are lost at capture time, before the raw dump is written. The raw `.ri`
metadata has no elevation or kernel-mode toggle, so the capture never records that it was degraded;
only a privileged capture fills the kernel ring.

The tool reports the condition at both points where it is observable. `collect` states the
consequence before the capture runs for either event it applies: the cycle spec passes `user=1,os=1`,
and the IBS event samples user and OS by default with no toggle exposed, so both request OS mode.
An unelevated capture of either appends `; kernel-mode (OS) samples are omitted without elevation`
to its unelevated warning, worded separately from the warning about missing elevated processes.

`profile` prints `KERNEL_COVERAGE` only when the applied sampler requested OS-mode samples and the
whole capture contains no sample in a kernel image. The request is read from the applied event in
`session.uprof`: the PMC `pmcEventList` entry's `os`, or an enabled IBS sampler's `opOs`/`fetchOs`
(a disabled sampler's recorded bit is ignored, and a session recording both samplers takes the bit
from either). The warning states what was observed — the sampler's bit name and the absence of a
kernel-image sample — and names elevation only conditionally, as how kernel-mode samples are
obtained. When the whole capture holds no sample of any kind, it reports the empty capture instead:
re-capturing is the remedy and elevation is not mentioned. The field is about kernel coverage, not
elevated user processes; a user-only scope neither creates nor hides it.

The check shares the whole-capture scan the `ACHIEVED` line already runs: `v_capture_modules` holds
one row per sample with its module, producing context (CPU for a dump, `coreId` for PMC), sample
count and kernel-image flag, so neither value costs a second query. It covers the whole capture,
before the `--image`/`--pid` and `--time-filter` selections. It is suppressed when `session.uprof`
records a `translate --time-filter`: the resolved dump may have been produced by that window and
need not hold the whole capture, so it cannot testify about kernel coverage. The check is
content-based, not privilege-based: it looks for a kernel-image sample in the capture, so an
elevated capture profiled unelevated emits no field, and a translate of an elevated raw capture
retains its kernel samples even when the translate runs unelevated.

A kernel image is classified from its module catalog entry: a module whose
`ModuleInstance.loadAddress` lies in the kernel half of the x64 address space
(`>= 0xFFFF800000000000`) is a kernel image. That covers kernel DLLs such as `ci.dll` that carry no
`.sys` suffix without relying on `Module.isSystemModule`, which is true for user-mode
`ntdll.dll`/`kernelbase.dll`. When the export has no `ModuleInstance` table, or a module has no
instance, the **basename** fallback applies: ends in `.sys` or `.ko`, or equals `ntoskrnl.exe`,
`ntkrnlmp.exe`, `ntkrnlpa.exe`, `ntkrpamp.exe`, `hal.dll`, `vmlinux`, `vmlinuz` or
`[kernel.kallsyms]`. The basename rule stays load-bearing in both directions: a path-suffix test
for `hal.dll` also matches user-mode `marshal.dll`, and `Module.isSystemModule` is not a kernel
predicate.

The measured case: an unelevated PMC capture with `user=1,os=1` had 21,489 samples and none in a
kernel image, while an elevated capture of the same event had 45,489 kernel samples across 67
images; an unelevated translate of that elevated raw capture reproduced the kernel samples, so the
loss is in capture, not translation. The numbers are recorded in `references/validation.md`.

## Symbolization

`profile --by function|region|line|instruction` symbolizes every module in scope; there is no separate command.

- `profile-rvas.sql` exports distinct `(module_name, module_path, rva)` with 0x-prefixed hex offsets; each module's RVAs are written to `module-NNNN/rvas.txt`.
- **Windows PDB discovery.** `llvm-readobj --coff-debug-directory` reads the image's PDB name, GUID and age. Candidates, in order: the explicit `--pdb-file`; each `--pdb-dir` root, as `<dir>/<pdb name>` and `<dir>/<GUID><Age>/<pdb name>`; `<pdb name>` and `<module stem>.pdb` beside the module; then, for every `_NT_SYMBOL_PATH` `srv*<cache>*<url>` entry, `<cache>/<pdb name>/<GUID><Age>/<pdb name>` and a recursive search under `<cache>/<pdb name>`. llvm-symbolizer itself ignores `_NT_SYMBOL_PATH`: its PDB sources are only an adjacent file, the recorded CodeView path or an explicit `--pdb`. The skill's own search is what finds a cached PDB and passes it as `--pdb`; the `srv*` entry contributes only local cache paths and the URL is never contacted, because LLVM has no Windows symbol-server client. `--obj` selects the image; there is no separate image-search directory. A candidate is accepted only when `llvm-pdbutil dump --summary` reports the image GUID and the internal age matches; for a canonical cache-key directory `<GUID><Age>`, the key is the age authority because the cached PDB's internal age can differ on real symbol servers. Without `llvm-pdbutil`, the first existing candidate (beside the module, an explicit `--pdb-dir` entry or the local `srv*` cache) is used with a warning and no GUID verification. Without an image GUID (llvm-readobj missing, failed, or the image records none), an explicit `--pdb-file` is used unverified with a warning and no other PDB is accepted. One evidence line per non-resolved module names the image GUID/age, the debug-info record, the searched locations, every `srv*` URL not consulted, each candidate's pdbutil exit/stderr and GUID/age mismatch, and the symbolizer's exit status and JSON `Error` with the remedy.
- **Symbol-path flags on `profile`.** `--pdb-file` passes an explicit PDB to llvm-symbolizer as `--pdb`; repeatable `--pdb-dir` adds search roots (flat and `<GUID><age>`); repeatable `--debug-file-dir` and `--fallback-debug-path` become `--debug-file-directory` and `--fallback-debug-path` for DWARF or separate debug files. `translate` keeps `--symbol-cache`, `--symbol-server` and its own `--pdb-dir` for staging a fetched PDB; profile the session afterwards. `translate` performs the fetch with AMD uProf's bundled DbgHelp/SymSrv, not WinDbg. On Linux, `llvm-symbolizer` reads DWARF from the module itself and the image/debug-file flags are the only separate-debug-file route; `_NT_SYMBOL_PATH` has no role.
- **Wrong-build PDB.** A PDB that is valid but built for another image makes llvm-symbolizer exit 0, emit no `Error` object and produce `Symbol` records whose frames all have empty `FunctionName` and `Line` 0. Both symbolization paths treat that as a resolution failure tied to the PDB name (`probably for another build`) instead of silently emitting `<no symbol>`. `llvm-pdbutil` GUID/age validation catches the same case earlier when it is installed.
- **Verify translate symbolization.** AMDuProfCLI translate exits 0 and prints a generic `Translation finished` even when it resolved no module, wrote an empty cache and logged nothing. After translate, the produced `cpu.db` is inspected: `SYMBOLIZATION: <withDebugInfo>/<modules> modules with debug info, <N> Function rows`, followed by a `WARNING:` naming `SYMBOL_CACHE=` and `SYMBOL_SERVERS=` and the `--symbol-cache`/`--symbol-server`/`--pdb-dir` remedy when either count is zero. Do not read the exit status as symbolization success; re-run into a new `--out-dir` after staging the PDB/symbol cache. Windows kernel symbols come from AMDuProfCLI's bundled DbgHelp/SymSrv during translate.
- **Symbolizer passes.** Two invocations per module, both `llvm-symbolizer --obj=<exe> [--pdb=<pdb>] --relative-address --inlines --output-style=JSON` over `rvas.txt`: `--functions=linkage` (demangled signatures) and `--functions=short` (one identity per function across inline and out-of-line copies). Exit 1 is accepted when records were emitted, with a warning.
- **Per-module Parquet** (`symbols.parquet`): `address VARCHAR, function VARCHAR, function_short VARCHAR, file VARCHAR, line INTEGER, containing_function VARCHAR`. `function`/`file`/`line` come from `Symbol[0]`, the innermost executed frame; `containing_function` comes from `Symbol[last]`, the outermost frame whose name uProf's `Function.id` carries. The combined artifact adds `module_name`; the fold cache `symbols.publics.csv` has `module_name, rva, name_count, names`.
- **Fold cache.** On Windows, `llvm-pdbutil dump -section-headers` plus `-publics` yields public function starts; `(section, offset)` becomes an RVA and names are grouped by RVA. Every start carries its names, so a single-name start keeps its name (the instruction context's anchor); `name_count > 1` is a folded start, whose `names` lists up to four sorted names then `... (+N more)`. The line and instruction views ASOF-join each sampled RVA to the nearest preceding start per module and label a folded body `shared: ...` with attribution `shared`.
- **Temporary artifacts.** The printed `ARTIFACTS=<temp dir>` holds `module-NNNN/rvas.txt`, `symbols.jsonl`, `symbols.short.jsonl`, `symbols.parquet` and, with a PDB, `module-NNNN/symbols.publics.csv`. On success it is removed and `ARTIFACTS_CLEANED=` printed; on failure it is kept and `ARTIFACTS_KEPT=... (the run failed; kept for inspection)` printed (or the cleanup failure is named).

### Agreement metric

`agreement.sql` joins scoped addresses to `v_symbols` and compares uProf's `Function.name` with the symbolizer's `containing_function`; an inline instance therefore matches its containing function by construction. For `function` and `region`, the AGREEMENT text quotes the emitted rows' sample attribution instead of the symbolizer's unresolved count: a derived range is an attribution, so counting it as unresolved would contradict the rows. The mismatch statistics still use this join.

| Field | Definition |
|---|---|
| `mismatch_rate` | 100 × `mismatched_samples / compared_samples`; compared means both the uProf name and the symbolizer name are non-empty |
| `name_mismatches` | distinct `(module, RVA)` addresses among the mismatched samples |
| `compared_addresses` | distinct addresses where both names are non-empty |
| `unresolved` / `unresolved_samples` | addresses with no symbolizer name, and their sample weight |
| `unresolved_pct` / `scoped_samples` | unresolved weight as a share of the whole scoped capture |

Samples weight each distinct address (one per dump row, the event count per PMC row). A row with an empty uProf `Function.name` is not compared and is not counted as a mismatch. Names are compared as exact strings; spelling differences between demanglers are normal. `unresolved` is the attribution category for an address with neither a symbolizer name nor a derived range. The agreement metric's `unresolved` fields count the symbolizer's no-name addresses before derived ranges are applied, so do not quote them for a function/region row: a derived range is attributed, not unresolved.

### Symbol coverage

`SYMBOL_COVERAGE` reports two symbolization channels over the same `v_scoped` rows as the table, so `--pid`, `--image` and `--time-filter` narrow both.

- **uProf DB channel.** `tools/coverage.sql` reads `Function.name` from the `cpu.db` that AMDuProfCLI translate produced. A sample is named when its `Function.name` is non-empty; a NULL `function_id` (no `UnifiedSampleSeries` address mapping) and a blank name both count as unresolved. `profile`'s `--pdb-file`, `--pdb-dir`, `--debug-file-dir` and `--fallback-debug-path` do not change this channel: it records only what translate symbolized.
- **Profile symbolizer channel.** For `--by function|region|line|instruction`, the rows are named from the parquet that `profile`'s own llvm-symbolizer invocation wrote. `agreement.sql` joins `v_scoped` to that parquet and counts a sample unresolved in the symbolizer channel when the symbolizer produced no name for its address; the resolved share is `scoped_samples - unresolved_samples`. The emitted function/region rows reclassify a sample covered by a derived range as `derived`, so report the row attribution, not the channel count. Only the explicit symbol-path flags in the line's `flags:` list affect this channel.

Without a disagreement and without an explicit symbol path the line keeps its single-channel form: `<named>/<total> samples (<percent>%) resolve to a named function`, followed by one `<module> <unresolved>/<samples> (<percent>% unresolved, partial|no source names)` entry per module with unresolved samples. When the channels disagree, or an explicit symbol path was passed, the line names both: `uProf DB <named>/<total> ...; profile symbolizer <resolved>/<scoped> ... (flags: <flags>)`. Each per-module entry may add `; cause: ...` from the symbolization evidence; when a cause is known, and whenever the symbolizer channel leaves samples unresolved, the line ends with `; remedy: ...` naming the flags that can change the row channel. The stderr evidence line carries the full detail.

The stop rule follows the channel that names the rows. For `function` and `region` the line reports the emitted rows as named, derived (synthetic binary range) and unresolved samples; a derived row is attributed and never counted as unresolved, so the line can reach the no-stop verdict even when the symbolizer named nothing. When the symbolizer resolves every scoped sample, the line says so and does not print the boundary text, even if the uProf DB share is 0%: those rows are drillable. When the symbolizer leaves samples unnamed, the line names the derived-range remedy: use `profile --by function`, which derives boundaries from the binary (`.pdata`, PE exports, PDB publics) with LLVM; do not write a PE parser, map `.pdata` by hand, convert RVAs to VAs or disassemble. `--by region` is the 4 KiB page fallback when no boundary source exists. A module with no source names is a finding to report, not a task to work around.

JSON carries the same distinction under `legend.symbol_coverage`, in fixed order: `uprof_db` (`source`, `named_samples`, `scoped_samples`), `flags` (the explicit symbol-path flag names used, such as `--pdb-file`, not their path values), `profile_symbolizer` (`source`, `resolved_samples`, `unresolved_samples`, `scoped_samples`, or `null` when the granularity does not symbolize) and `rows_drillable` (`true` only when the symbolizer resolved every scoped sample). For `function` and `region`, `profile_symbolizer` instead carries `named_samples`, `derived_samples`, `resolved_samples` (named plus derived), `unresolved_samples` and `scoped_samples`, and `rows_drillable` is true when the unresolved count is zero: a synthetic range is actionable at its hot RVA.

### Derived function ranges

Function is the semantic unit. A release binary often has no debug symbols, so `--by function` derives function boundaries from the image itself with the LLVM tooling already used for symbolization; the consumer does not write a PE parser, does not read `.pdata` by hand and does not map addresses itself.

- **Boundary sources**, strongest first: a PE export (`llvm-readobj --coff-exports`), a PDB public start (`llvm-pdbutil dump --publics` and the `symbols.publics.csv` fold cache), then a `.pdata` runtime-function start (`llvm-readobj --unwind`). At one RVA the stronger source keeps its name; a `.pdata` start with no name is a boundary but no name.
- **Range.** A code section's selected starts are sorted and each range runs from a start to the next start, or to the section end. A section with no boundary source contributes no range, so the tool never invents one whole-section function. `function_source` is `export`, `pdb_public`, `pdata` or `none`; `function_derived` is true only for a range with no export or public name.
- **Name.** An export or PDB public supplies its name. A boundary with no name is labelled visibly synthetic: `pdata_0x<start>` for a `.pdata` range and `sub_0x<start>` otherwise. A sample inside a range groups under it even when uProf's `Function.id` or the symbolizer assigned another name; a sample outside every range keeps its `Function.id` row, or `<unresolved>` with a `function_reason`.
- **Attribution.** `unique` means a named range or a named uProf `Function.id`; `derived` means a range with a synthetic name; `unresolved` means neither. `unresolved` is only this attribution category: a module whose boundaries came from a derived range is `derived`, even when every name is synthetic, and must be called "derived" or "unnamed at source level", never "unresolved" or "no named functions". `legend.function_ranges` reports `source`, `derived_modules`, `ranges`, `absent_by_module` (module → why no range was derived: no path, missing file, `llvm-readobj` missing, not a PE image, no code section, or no boundary source) and the `fallback` (a fully named module is skipped and says so; a module with no derived range keeps `Function.id` and `function_reason` says why).
- **Cost.** Only modules with at least one empty `Function.name` are read; a fully named module keeps its `Function.id` rows, costs no extra tool run, and its `function_reason` says `fully named from the uProf DB; function-range derivation skipped`. A module that was read but produced no range says why instead (no path, missing file, `llvm-readobj` missing/failed, not a PE image, no code section, or no boundary source).

### Function-scoped instruction and line views

`profile --by instruction|line --function NAME --module MODULE [--function-start RVA]` filters the rows to one `--by function` row. Copy `function`, `module` and, when present, `function_start` from that row; there is no name-only or module-only scope and no implicit "current function". `--function` is rejected with any other `--by`.

- **Scope.** A derived range filters by address: `module + RVA in [function_start, function_end)`. A row with no derived range filters by uProf `Function.id`, so `--function` still works for a fully named module, where derivation was skipped. When the name matches more than one row, `--function-start` selects one; two no-range rows with the same name cannot be separated and are reported as such. A synthetic range keeps its visible `pdata_0x<start>`/`sub_0x<start>` label.
- **Parent measurement.** The parent totals come from `v_function_measured`, the same uncapped function aggregation that feeds `--by function`, so they are a sum/join of the function's own rows, never a second measurement. The header carries `FILTER:` with the explicit scope and `PARENT:` with the parent's `samples`, `sample_share` of the scoped capture, `time_share`/`tag_to_retire_share` (or the reason each is absent) and `cpu_seconds` when the mode supplies it.
- **Two denominators.** A row keeps the ordinary capture-level shares and adds `parent_sample_share`, `parent_time_share` and `parent_tag_to_retire_share` (when applicable): its samples, gap weight or tag cycles as a share of the parent function. The `--top` remainder row carries the same parent shares, so the truncated rows and the remainder sum to the parent total. `JSON` carries the same measurement under `legend.function_scope`, with `range_kind` (`derived_range` or `uprof_function_id`), a `parent` object (`samples`, `sample_share`, `time_share`, `tag_to_retire_share`, `cpu_seconds`, each share `of the scoped capture`), the row columns' `denominators`, `measurement` and `child_shares`.
- **Line view.** The line view needs source positions; a function whose samples resolve to no source line appears as one `<no source line>` row relative to the parent, not as an empty table. The function name and range come from the binary, so the row still names the function when it has no source position.
- **NEXT.** A filtered instruction view offers `--by line` and a filtered line view offers `--by instruction --json` with the same `--function`/`--module`/`--function-start` and `--top`; that is its only drill-down. The unscoped instruction view still stops, and the unscoped line view keeps its own line -> instruction call.

### Region fallback — 4 KiB address pages

`--by region` is the fallback only when the function rows are in the `unresolved` attribution and `function_source` is `none`, or when an explicitly requested page-level view needs it. region = 4 KiB address page; samples are grouped by address, not by control flow; a page may span unrelated code; the hot RVA is the precise attribution and the page is the coarse bucket. The row keeps `sampled_addresses` so a page with many scattered addresses is distinguishable from a dense one, and `hot_rva`/`hot_samples`/`hot_sample_share` carry the sharpest address. When the hot RVA falls in a derived range, the row names that function and its source; otherwise `function_source` is `none`.

### Ranking and the hot row

`--by process`, `--by module`, `--by function`, `--by region` and `--by instruction` order rows and choose `--top` by the mode measure: `sample_share` for a cycle-controlled, op-count or supported PMC cycle capture, `time_share` for an IBS fetch or gap-weighted capture, and raw `samples` when a PMC event leaves `sample_share` unestablished. `--top` therefore selects the top rows in the measure that the row's own `READ` and `WEIGHTS` lines name. `--top all` is not needed to see a head row; `--sort` does not exist.

The sample-hot address is the `hot_rva` with the largest `hot_samples`, and it is not necessarily inside the top-ranked row: a function may rank first by `time_share` while a different function's address has the most samples. `--by module` carries each module's `hot_rva`/`hot_samples`/`other_samples`/`hot_sample_share`; function rows carry the same four. `other_samples` is `samples - hot_samples`: the rest of that row, not a second hot address. `--by function` prints one `HOT_RVA:` summary computed as the maximum `hot_samples` across all function rows, not the top row's `hot_rva`, and reports that row's `other_samples` with it. Use those for a sample-hot claim. The instruction table follows the same mode measure: it is sample-hot for cycle/op/PMC captures, but for a time-based capture it is `time_share`-ordered and must not be used to claim the sample-hot RVA or that no single instruction exceeds a sample threshold.

## Module and CPU-time questions

### Module rollup

`profile --by module [--json]` is the supported rollup: one row per normalized module path with `samples`, `sample_share`, `time_share` (when the mode supplies it), `tag_to_retire_share` (a dump with a non-null tag column), `isSystemModule`, `cpu_seconds` and the module's highest-sample address as `hot_rva`/`hot_samples`/`other_samples`/`hot_sample_share` (`other_samples` = `samples - hot_samples`, the rest of the module). Use these fields; do not roll functions up in ad-hoc SQL or query `cpu.db` for them. One text call answers the module question and names its sample-hot address; do not also run `--by module --json` to sum third-party rows from the same capture. Drill into the module with `profile --by function`, which derives boundaries when translate named nothing.

Rank by `sample_share` for a cycle-controlled or PMC cycle capture, where each sample carries fixed cycles; read `time_share` under the mode's meaning otherwise. `--top all` keeps the remainder row out of the total; otherwise `<other modules>` carries the rest and has a NULL `isSystemModule`.

**Third-party** is the rule `Module.isSystemModule = false`. `true` is uProf's system-module flag; it covers OS components and some vendor drivers (for example `amdvlk64.dll`), so it is neither an OS-shipped predicate nor a kernel predicate. The flag marks user modules such as `ntdll.dll`/`kernelbase.dll`, and a kernel image (`.sys`/`.ko`, a kernel basename, or a `ModuleInstance.loadAddress` in the kernel half) is expected to be system but that is not measured on the cycle-controlled capture. The `module` key is the normalized dump `ModuleName` or, for a PMC capture, the normalized module path; `v_module_flags` maps both the full path and its basename, so a dump that carries a short name still classifies. The column is absent from older exports, and the output then says so and leaves `isSystemModule` NULL. A system-flagged driver can still dominate the capture: report the strict third-party total and the dominant non-workload dependency separately rather than inspecting the path or re-deriving the rule by hand.

### Absolute CPU time

`--by module` emits `cpu_seconds` as `samples × interval / core_frequency`: `samples` is the row count and `interval` is the requested cycle count converted at the core frequency `ACHIEVED` reports, the capture's nominal execution clock. The session records the interval in cycles, not the frequency of the capture machine, so a capture from other hardware gives an approximate figure and the seconds are an estimate at that clock, not a measured cycle count. This is sampled CPU time, not wall time. A system-wide capture sums CPU time across cores and halted cycles are excluded; an attached thread's samples cover only the time it ran, so its off-CPU intervals are not counted and the total is not multiplied by the CPU count. The conversion does not apply to op-count or fetch samples: their interval is work, not time, and `cpu_seconds` is omitted with that reason.

## Instruction context — `--by instruction --json`

`uprof_query` calls `uprof_context.instruction_contexts()` before rendering JSON for instruction scope; Markdown never builds or receives the block. The block answers "what instructions surround this hot address" without replacing the row's own numbers.

**Anchor.** The hot RVA must lie in an executable PE section (`IMAGE_SCN_CNT_CODE` or `IMAGE_SCN_MEM_EXECUTE`). Safe starts are the `v_public_starts` PDB publics, the PE export table (`llvm-readobj --coff-exports`) and the `.pdata` runtime functions (`llvm-readobj --unwind`), all inside that section at or below the hot RVA. The nearest start wins; at the same RVA an export is preferred, then a PDB public, then a `.pdata` start. When it is within 1 MiB it is the anchor; otherwise the section start is used if it is within 1 MiB; otherwise `anchor_too_far`. `anchor.kind` is the source of the chosen start: `export`, `pdb_public`, `pdata` or `section`. The anchor is where decoding begins — x86 has no backward decode, so an anchor after the hot RVA cannot help.

**Window.** `llvm-objdump -d --start-address=image_base+anchor --stop-address=... --no-show-raw-insn --no-symbolize-operands` decodes forward; the stop address reserves 32 instructions after the hot RVA and is clamped to the containing section's end. The window is the last up to 32 decoded instructions before the hot RVA and up to 32 after it, plus the hot instruction, sorted by RVA in the JSON. A hot RVA that never appears as a decoded boundary is `hot_rva_not_instruction_start`; it is never snapped to the preceding boundary. Objdump's nearest-symbol label is discarded — only address and instruction text survive.

**Per-instruction fields.**

| Field | Definition |
|---|---|
| `rva` | module-relative address, `0x` hex |
| `instruction` | mnemonic and operands as `llvm-objdump` prints them, raw bytes and trailing comments removed; it may retain objdump's nearest-symbol `<...>` decoration, which is not the frame-0 name — use `symbol` |
| `samples` | raw scoped samples at this exact RVA |
| `sample_share` | `samples / v_scope_totals.total_samples`, a fraction 0.0-1.0 like the row's own `sample_share`; the mode-specific meaning is the one in `legend.columns.sample_share` — sampled frequency for op-count and fetch, core-cycle share for cycle-controlled — not impact and never gap-weighted time; `null` when the scope is empty |
| `symbol` | optional `{function, file, line}` from `llvm-symbolizer` frame 0 (`Symbol[0]`) using the PDB recorded by the symbolization pass; `line` omitted when 0; an empty or errored record leaves the field absent |
| `import` | optional `{dll, name, slot_rva}` when the instruction is a `call`/`callq` whose operand is a `*disp(%rip)` or absolute IAT slot; register and indirect non-IAT targets stay raw; an ordinal-only symbol is `#<ordinal>` |
| `ibs` | optional aggregated IBS OP labels, present when the dump has a row at the RVA and supplies at least one whitelisted field; the summary may carry only `samples` |

`samples` counts how often the hardware selected the instruction for sampling: it is sampling/dispatch frequency, not impact, and a high count does not make the instruction costly. The table's row order — and therefore `--top` and the row cap below — is the mode's primary measure: `sample_share` for cycle/op/PMC captures and the time weight (`DeltaTick` gaps) for a time-based capture, falling back to `samples` when neither is available. The first row is sample-hot only in the sample-ordered case; a time-ordered table must not be used to claim the sample-hot RVA or that no single instruction exceeds a sample threshold — `HOT_RVA:`/function/region `hot_rva` is that attribution. The per-instruction IBS labels are the evidence for the instruction's cost.

When every context is omitted with `symbolizer_failed`, stderr prints one `CONTEXT:` line naming the shared detail, the count and the remedy; that is one missing-PDB finding, not one failure per row, and `uprof_context.py`/`context_help` need not be read to confirm it. Each omitted row also carries `omitted: true` and its `reason` (for example `symbolizer_failed`) in the JSON, so a single unscoped instruction run can be filtered by `module` and read row by row; do not grep stderr to identify failed rows.

**IAT resolution.** `llvm-readobj --coff-imports` is parsed as text — its JSON style is not valid JSON. Each `Import` block's `ImportAddressTableRVA` plus `symbol index * slot width` (8 bytes for PE32+, 4 for PE32) gives the slot RVA; symbols keep the block's order and the parenthesized hint/ordinal is not part of the name. A `callq *disp(%rip)` slot is `next_rva + disp`, using the following decoded instruction for the RIP value; an absolute operand is converted to an RVA by subtracting `image_base` before the lookup. `DelayImport` blocks are not IAT slots. Import failure is not fatal: imports stay absent.

**IBS labels.** Only fields present as dump columns are emitted, in this order: `DataSrc`, `CacheHitSt`, `IbsDcMiss`, `IbsOpBrnMisp`, `IbsDcLockedOp`, `IbsOpMemWidth`, `IbsTagToRetCtr`. The labels are the hardware field names, not semantic classes, and the values are AMD-encoded counts or histograms, not decoded meanings.

| Field | Aggregate |
|---|---|
| `DataSrc`, `CacheHitSt`, `IbsOpMemWidth` | `{code: sample count}`, keys sorted numerically as printed by the dump |
| `IbsDcMiss`, `IbsOpBrnMisp`, `IbsDcLockedOp` | `{"set": n, "of": samples}` — count of nonzero values |
| `IbsTagToRetCtr` | `{"samples", "sum", "max"}` over the RVA's dump rows with the `tag_to_retire_share` policy: only an unsigned decimal integer cell counts, each clipped at 65535, the 16-bit field's maximum; NULL, negative, fractional and exponent cells are excluded, and `samples` counts the qualifying cells, not dump rows; omitted when no cell qualifies |

`ibs.samples` is the dump-row count at the RVA, counted over the same PID, image and time window as the row's samples. For a PMC capture, or an IBS dump without these columns, no `ibs` field is emitted.

**Build identity.** `build` carries `verified`, `identity`, `image_base`, `time_date_stamp`, `checksum`, `capture_checksum`, `size_of_image` and `capture_size`, all hex where numeric. A nonzero on-disk `CheckSum` equal to the capture's `Module.checksum` verifies the build (`identity: "checksum"`); a nonzero mismatch omits the block (`build_mismatch`). Without a usable checksum, an equal recorded `SizeOfImage` verifies it (`identity: "size"`). Without matching evidence the block is still emitted with `verified: false`, `identity: "unverified"` and a `warning` that names the missing or disagreeing evidence; verification is never claimed without it. This prevents decoding a different build than the one captured.

**Omission shape.** `{"omitted": true, "reason": "<code>", "detail": "<text>"}`; `detail` is present only when non-empty. An omission made after the module was examined also carries the same `build` object as a successful block, so an unverified build or a `SizeOfImage` mismatch is visible on the omitted row; `build_mismatch` puts the mismatching checksum in `detail` and `build.warning`. A row that omits still consumes one of the 20 slots. Reasons: `module_missing`, `readobj_missing`, `readobj_failed`, `metadata_incomplete` (headers lack `ImageBase`/`TimeDateStamp`/`CheckSum`/`Magic`, e.g. ELF), `no_code_section`, `anchor_too_far`, `hot_rva_not_instruction_start`, `objdump_missing`, `objdump_failed`, `symbolizer_missing`, `symbolizer_failed`, `imports_failed`, `build_mismatch`, `context_failed` (an LLVM tool timed out or a scoped query failed), `row_cap`. Symbolizer failures (nonzero exit, JSON `Error`, or a PDB that resolves no address) and an unreadable import table omit the block with their evidence; an unresolved individual IAT target leaves only that optional field absent. A build without matching checksum or size evidence is emitted unverified, not omitted. The `<other instructions>` row (`rva` null) has no context.

**Document help.** A single top-level `context_help` object is emitted once per JSON document, after `legend` and before `rows`. Its keys are always in this order: `scope`, `share`, `fields`, `omission_reasons`, `read`, `next`. It is built from constant text plus the document's legend, so repeated runs produce identical bytes.

| Key | Content |
|---|---|
| `scope` | one context per instruction row; the 20-row cap counts row slots in the table's row order, so a row that omits for another reason still consumes a slot; rows past the cap carry `row_cap`, and the `<other instructions>` row carries none |
| `share` | `sample_share` is a fraction; its unit and denominator are `legend.share_unit`, `legend.share_scale` and `legend.denominators.sample_share`, and its mode-specific meaning is `legend.columns.sample_share` — the help states no second scale |
| `fields` | `anchor` (`kind`, `rva`, `name` when named, `distance` in bytes from the anchor to the row's rva), `build` (`verified`, `identity`, hex identities and sizes, `warning` when present), `instructions` (static stream in ascending rva; `rva`, `instruction`, `samples`, `sample_share` fraction, optional `symbol`, `import`, `ibs`; `instruction` may retain objdump's nearest-symbol `<...>` decoration, so use `symbol` for the frame-0 name), `instructions[].symbol`, `instructions[].import`, `instructions[].ibs` (including the 65535 clip, the unsigned-decimal-integer policy and the note that values are AMD-encoded counts/histograms, not decoded meanings), `omitted` (carrying `build` when the module was examined), and `row.attribution` only when the legend carries that column |
| `omission_reasons` | every reason code with its meaning, in the order listed above |
| `read` | the sampled instruction is the entry whose `rva` equals the row's; `sample_share` is the mode-specific raw share, not a per-op cost and not a ranking; rows are ordered by `legend.impact.order_by`; imports and symbol frames identify the work kind; when the legend carries `tag_to_retire_share`, it and `ibs.IbsTagToRetCtr` are total tag-to-retire dwell evidence, not per-op latency |
| `next` | defer to `legend.next`; rank by `legend.impact.order_by`; a high `sample_share` with a low `time_share` and no tag-to-retire evidence is frequency, not an impact finding |

The help is JSON-only. Markdown output has no `context_help` and is byte-identical to a run without the context feature.

**Limits and caching.** The 20-row cap counts row slots in the table's order (the mode's primary measure); a row that omits for another reason still consumes one, and later rows carry `row_cap`. Module metadata, imports, public starts and the symbolization pass's PDB choice are cached per module across rows; each anchor is decoded once for all rows under it, and samples, symbols and IBS are aggregated once per module. Each external call has a five-minute timeout. The feature is PE/COFF-only; ELF modules omit with `metadata_incomplete`.

## SQL schema — `tools/views.sql`

One schema file; every object is TEMP because `cpu.db` is opened read-only. `uprof.py` binds sixteen inputs as prepared parameters (`SET VARIABLE name = ?`, never text splicing), loads `views.sql` once, then runs one projection.

| Bound input | Meaning |
|---|---|
| `uprof_image` | process base-image ILIKE pattern; `'%'` selects every process |
| `uprof_pid` | one PID as text; empty selects every process |
| `uprof_top` | row cap bound from `--top`: a positive `N`, or `0` for `--top all` (no cap) |
| `uprof_time_start`, `uprof_time_end` | window in ns; `[start, end)` |
| `uprof_ibs_dump` | absolute dump path; empty for PMC |
| `uprof_event_column` | PMC sample column in `UnifiedSampleSeries`; empty for a dump capture |
| `uprof_session_meta` | `session.uprof` path when it exists, else empty |
| `uprof_tag_available` | `true` only for an IBS OP dump whose `IbsTagToRetCtr` exists and has a non-null value, so a fetch dump cannot enable `tag_to_retire_share` even with the column; Python derives it from the sampler kind and `v_dump_sanity` and rebinds it before the projection |
| `uprof_symbols` | combined `symbols.parquet`; empty when absent |
| `uprof_aliases` | combined `symbols.publics.csv`; empty when absent |
| `uprof_pdbs` | combined `symbols.pdbs.csv`: the PDB symbolization chose per module; empty when absent |
| `uprof_functions` | derived function ranges CSV (`module_name,function_start,function_end,function_source,function_name`); empty when no module in scope needs derivation |
| `uprof_range_attempted` | chr(2)-joined modules for which derivation ran; a fully named module is absent, so `function_reason` says the derivation was skipped rather than failed |
| `uprof_primary_measure` | `sample_share` or `time_share`, the mode measure that orders the row views and selects their `--top` and hot RVA |
| `uprof_sample_interval_s` | requested cycle interval in seconds, bound for `--by module|function|region`; empty when the interval is a work/fetch count or the core frequency is unavailable, which hides `cpu_seconds` |

| Macro | Definition |
|---|---|
| `module_key(p)` | `replace(lower(p), chr(92), '/')` — the module-path match key |
| `share(part, whole)` | `part / NULLIF(whole, 0)` — a fraction 0.0-1.0, the unit stated once in the output |

| Mode flag / guard | Purpose |
|---|---|
| `has_dump` | `uprof_ibs_dump` is non-empty |
| `schema_check` | fails once, naming every missing table/column and a bad `event#` column |
| `schema_check_pid` | PMC only: requires `UnifiedSampleSeries.processId`; dump captures do not |
| `dump_guard` | the dump exists and `time_start < time_end` |
| `dump_columns_guard` | one `DESCRIBE`; names missing required dump columns (`DeltaTick`, `CPU_Number`, `PID`, `Offset`, `ModuleName`) |
| `has_tag` | the dump header carries `IbsTagToRetCtr`; `v_dump_sanity.tag_values` counts non-null values |
| `has_system_module` | `Module` has an `isSystemModule` column; older exports leave the module classification NULL |
| `top_n`, `time_start`, `time_end` | typed row cap and window |

The 24 views:

| View | Role |
|---|---|
| `v_dump` | typed dump rows (`delta_tick`, `cpu`, `pid`, `rva`, `module_name`, `tag_cycles`); tagged/untagged branch; an empty typed relation for PMC |
| `v_dump_context` | `v_dump` plus the weighting context: `context_pid` 0 and `context` = `cpu` for a system-wide capture, `context_pid` = `pid` and `context` = `tid` for an attached one; defined once so weighting and the sanity check cannot disagree |
| `v_dump_weighted` | applies `[time_start, time_end)` then adds `weight = delta_tick - lag(delta_tick) OVER (PARTITION BY context_pid, context ORDER BY delta_tick)` from `v_dump_context`; the helper columns are excluded |
| `v_capture_span` | the span recorded in `session.uprof`: the translate `--time-filter` window when recorded, else `collection.schedule.elapsed`, else `profileDuration`; `start_ns`/`end_ns`/`expected_ns`/`span_label`/`system_wide`, shared by the dump-wide and scoped sanity views |
| `v_dump_sanity` | whole-dump provenance check: samples outside the recorded span bounds, or core time above span × CPUs when system-wide, error; attached captures report `OK (attached; dump-wide)` with processes, TIDs, active TID and CPUs visited, and error only for samples outside the span; no usable duration or uncovered late time warns dump-wide; exposes `tag_values` for the empty-tag disclosure |
| `v_symbols` | combined symbolizer artifact: `module_name, rva, function, function_short, containing_function, file, line` |
| `v_public_starts` | public-symbol fold cache: `module_name, rva, name_count, names` |
| `v_function_ranges` | derived code ranges from `uprof_functions`, typed to `module_name, function_start, function_end, function_source, function_name`; empty when no derivation ran |
| `v_pdbs` | PDB chosen by symbolization per module (`module_name`, `pdb`); empty when absent, so the instruction context reuses it instead of discovering a PDB again |
| `v_scoped` | the one scope: dump branch weights `v_dump_weighted` and maps RVA to `Function.id`; PMC branch counts the event column and leaves `weight` NULL; `--pid`/`--image` apply after weighting |
| `v_scope_sanity` | scoped late-coverage verdict appended to `SANITY` when `--image`/`--pid`/`--time-filter` is active: last sample of `v_scoped` against the scope window end, `WARN` when the gap exceeds the scope's own sampling cadence |
| `v_scope_totals` | `total_samples` (0 when empty); feeds the empty-scope warning |
| `v_scope_measures` | the whole scoped capture's `total_samples`/`total_weight`/`total_tag_cycles`; line and instruction rows use it as their denominator so a `--function` filter cannot silently shrink the capture shares |
| `v_module_catalog` | one row per module path with its highest `ModuleInstance.loadAddress` and kernel-image flag; empty when the export has no `ModuleInstance` |
| `v_module_flags` | `Module.isSystemModule` keyed by the full normalized path and its basename, so a short dump `ModuleName` still classifies; empty when the export has no such column |
| `v_capture_modules` | one row per whole-capture sample: module, context (CPU for a dump, `coreId` for PMC), sample count and kernel-image flag; before scope and time filters; feeds `ACHIEVED` and `KERNEL_COVERAGE` from one scan |
| `v_process_rows` | aggregation, ranking, totals and the `<other processes>` remainder |
| `v_module_rows` | module path aggregation, ranking, totals, `isSystemModule`, the module's `hot_rva`/`hot_samples` and the `<other modules>` remainder |
| `v_function_measured` | the uncapped function aggregation behind `v_function_rows`: derived range when `uprof_functions` covers the RVA, else `Function.id`; `function_start`/`function_end`/`function_source`/`function_derived`; `<unresolved>` row; attribution `unique`/`derived`/`unresolved`; hot RVA by the mode measure; total samples/weight/tag; `--function` reads its parent row here so `--top` cannot hide it |
| `v_function_rows` | `v_function_measured` with `--top` applied and one `<other functions>` remainder |
| `v_function_scope` | the single parent row selected by `uprof_function`/`uprof_function_module`/`uprof_function_start`; empty unless a function-scoped line or instruction view was requested |
| `v_region_rows` | one row per module and 4 KiB address page; `page_start`/`page_end`, `sampled_addresses`, hot RVA and the function that owns it; attribution `unique`/`shared`/`derived`/`unresolved`; `<other regions>` |
| `v_line_rows` | symbolizer join, short-name/overload identity, `<no symbol>`/`<no source line>`/`<multiple>` buckets, fold labels; filtered to `v_function_scope` when `--function` is set; `<other lines>` |
| `v_instruction_rows` | one row per module + RVA with innermost annotation, ordered by the mode's primary measure; filtered to `v_function_scope` when `--function` is set; `<other instructions>` |

- **Projection files** are one final SELECT over the views: `profile-process.sql`, `profile-module.sql`, `profile-function.sql`, `profile-region.sql`, `profile-line.sql`, `profile-instruction.sql`; they apply the `share()` macro and hide internal columns. `profile-rvas.sql` exports the symbolization scope, `profile-function-modules.sql` names the modules whose samples need derived ranges, `agreement.sql` computes the metric, `coverage.sql` reports per-module symbol coverage, and `schema.sql` lists the live schema. There are no optional schema files: `views.sql` is the only schema.
- **Load order.** Bind all inputs, load `views.sql`, then (for `function`/`region`/`line`/`instruction`) run `profile-rvas.sql` on the same connection, symbolize, rebind `uprof_symbols`/`uprof_aliases`; for `function`/`region`, and for `line`/`instruction` with `--function`, run `profile-function-modules.sql`, derive ranges from the binary with LLVM and rebind `uprof_functions` (the filter may name a derived range); then run the projection. `query(CASE ...)` re-binds per query, so the symbol and range inputs may be bound after the views are created. A hand run writes a UTF-8 `SET VARIABLE` file, `.read`s `views.sql`, then the projection, and always uses the original session's `cpu.db`.
- **Guard order.** The schema and dump guards run while `views.sql` loads, before any query, so an incomplete export or dump fails once with the missing item names. The schema guards read the bound path strings (empty means absent) so no identifier is spliced; the dump guard checks that the dump exists. A conditional `query(CASE ...)` avoids binder-eager branch evaluation.

## Reproducibility and tie order

The same capture and arguments produce byte-identical output on every run. Each row view ranks by weight, then samples, then a stable key; the remainder row is last:

| `--by` | ORDER BY |
|---|---|
| `process` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, pid, process` |
| `module` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, module` |
| `function` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, function_name, module_name` |
| `region` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, module, page_start` |
| `line` | `weight DESC NULLS LAST, samples DESC, file, line, function, module` |
| `instruction` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, module, rva NULLS LAST, function` |

For a time-based capture the `time_share` weight orders the rows; for a cycle-controlled, op-count or
supported PMC cycle capture the sample count orders them, which is the same order as
`sample_share`. For any other PMC event the weight is NULL and the sample count is the only
available order, with no established ordering claim beyond it. The JSON `legend.impact` (`order_by`,
`gap_weighted`, `estimate`, `primary_measure`, `primary_measure_reason`) and the `READ` line state
this ordering, with `gap_weighted` false when the capture records no DeltaTick gaps. For IBS,
`sample_share` is the raw sample share: a core-cycle share, a dispatched-op frequency share or a
fetch-sample frequency share as the mode states, never a per-op cost; `tag_to_retire_share`, where
the dump supplies it, is total tag-to-retire cycles, not per-op latency.

**Display escaping.** Table cells and header values escape controls as described in `SKILL.md`, Output; JSON carries the true characters, so table/JSON parity is modulo that display escaping. `--out-file` receives the same bytes as stdout.

## Known limitations

- **Attached `time_share` is elapsed thread time, not CPU time.** Gaps are partitioned by
  `(PID, TID)`, so a migrating thread is counted once and the total cannot exceed the span, but a
  gap includes time the thread was off-CPU and is charged to the sample that ends it. For a
  cycle-controlled IBS capture `sample_share` (fixed cycles per sample) is the CPU-cycle share and
  the better ranking; for op-count and fetch, no emitted share is CPU time. Attached coverage is
  wherever the attached threads ran, so attached captures get no scoped late-coverage verdict.

- **Unelevated captures have no kernel coverage.** The OS-mode request is applied and encoded, but an unelevated uProf capture contains no kernel-mode samples; elevate the capture. `collect` notes this before the capture runs and `profile` prints `KERNEL_COVERAGE` when the whole capture confirms no kernel-image sample (see Kernel coverage).
- **Inline overloads.** The display rule is in `SKILL.md`, Frame rule and sentinels; a true overload set (two or more out-of-line signatures) shows `<multiple>` on a shared source line.
- **Empty or junk tag column.** `has_tag` tests column presence; `uprof_tag_available` requires an IBS OP dump and at least one non-null value. An all-NULL or junk-only `IbsTagToRetCtr` hides `tag_to_retire_share` and prints the empty-column disclosure; only an unsigned decimal integer cell counts, so a negative, fractional or exponent cell is NULL, never the clipped maximum. An all-zero column is present with every share NULL, not 1.0.
- **Module paths are lowercased.** `module_key` lowercases and replaces `\` with `/`; the module column shows that normalized spelling, and two paths differing only in case or separator collide.
- **Fold detection is Windows/PDB only.** Resolved Linux DWARF symbols are all `unique` (unverified here); unresolved rows still exist. Without `llvm-pdbutil` the cache is skipped and a note is printed.
- **Duplicate module copies.** Two copies of one DLL (for example two driverstore paths) are
  separate `module_name`s; merge them with an ILIKE pattern or aggregate by basename.
- **Nearest-export mapping is not valid for a symbol-less DLL with a sparse export table.**
  Joining a hot RVA to the nearest preceding export names an unrelated small function; treat that
  as no attribution and use RVA-level samples plus disassembly.
- **Instruction context is PE/COFF-only and JSON-only.** It reads PE headers, sections and the COFF
  import table; an ELF module omits with `metadata_incomplete`. Markdown output never carries it,
  and only the first 20 instruction rows — the rows in the mode's primary-measure order — are expanded.
- **`--json --out-file` writes no file when the query fails** (including the dump sanity error);
  the reason is on stderr and the exit code is 2, so check the exit code before reading the file.
- **`<multiple>` is coarse.** A source line carrying several overload signatures is reported as one ambiguous annotation, not split per signature.

## Troubleshooting

The failure table in `SKILL.md` covers dump sanity, missing `session.uprof`/`cpu.db`, PDB misses, empty scope, and output/filesystem errors. This table adds the remaining remedies:

| Symptom | Cause | Command or remedy |
|---|---|---|
| `<no symbol>` rows, `PDB=none` | no PDB beside the module, at the recorded CodeView path or in a local `srv*` cache, a candidate GUID/age mismatch, a PDB for another build (empty `Symbol` frames), no debug-info record, missing binary, or missing/failed llvm-readobj | install LLVM; the per-module evidence line names the searched locations, the `srv*` URLs that were not contacted and each rejection; pass `profile --pdb-file <file>`/`--pdb-dir <dir>` or `--debug-file-dir`/`--fallback-debug-path`; or place the matching PDB beside the module or in a local `srv*` cache (stage it with `translate --pdb-dir`/`--symbol-cache`/`--symbol-server`), then re-run `profile`; llvm-symbolizer ignores `_NT_SYMBOL_PATH` |
| `MISSING ...` from `tools` | uProf CLI, DuckDB 2.x or llvm-symbolizer not found | install or set `AMDUPROF_CLI` / `LLVM_SYMBOLIZER`; `python -m pip install duckdb` |
| `uProf export incomplete: missing ...` | uProf export differs from the expected schema | run `schema --session <dir>` and compare; use a matching uProf version |
| `error: argument --image: not allowed with argument --pid` | both scope flags given | pass one |
| `WARNING: no samples in the selected scope` with exit 0 | the scope filter matched no sample; the empty document is still written | read the warning's `Process.name` list; `--image` matches process base images, not modules, so use `--by module` or `--function ... --module`; on a single-process attached capture omit `--image` |
| capture directory locked during `translate` | open in the GUI | close it and retry |
| `--out-dir already holds a previous translation` | a reused output directory still has `cpu.db`, `callstack.db` or an ASCII dump | pass `--force` to replace the generated files and copied inputs, or choose another `--out-dir` |
| `SAMPLER: falling back ...` | VBS or hypervisor blocks IBS counters | disable VBS/Hyper-V, or accept cycle sampling |
