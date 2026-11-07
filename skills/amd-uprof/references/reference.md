# Reference

Detail behind `SKILL.md`. The tools and `tools/views.sql` are the authority.

## session.uprof

UTF-8 JSON (a BOM is tolerated). The keys the tools read:

| Key | Used for |
|---|---|
| `collection.events.pmcEvents.pmcEventList[]` | PMC mode: `hwId` 118, `interval`, `isSampling`, `encodedEventId` |
| `collection.events.ibsEvents.{op,opInterval,opCycleCount,fetch,fetchInterval}` | IBS mode and interval control |
| `auxiliary.ibsOpSampling` | IBS-versus-PMC detection |
| `auxiliary.eventColumnIds[]` | event name → `event#0x<encodedId>` sample column |
| `collection.target.{systemWide,attachedProcesses[].processId}` | capture mode check |
| `collection.schedule.{elapsed,profileDuration}` | capture span for the dump sanity check |
| `collection.schedule.{start,end,startTs}` | clock anchoring: `start`/`end` are QPC-derived nanoseconds; `startTs` is local time at one-second precision |
| `translateCommand` | `--time-filter` window and the dump cache key |
| `collectCommand` | printed; fallback event name when the event list records none |

- **Event-column mapping.** `auxiliary.eventColumnIds` maps registered names to `event#0x<encodedId>`. The PMC sample column is the `CYCLES_NOT_IN_HALT` mapping; the hwId 118 `pmcEventList` entry's `encodedEventId` must equal it, or `derive_sampler` errors as inconsistent metadata. Without a mapping, the entry's `encodedEventId` is used. IBS names: `IBS_ALL_OPS` → `event#0x100000000f100`, `IBS_FETCH` → `event#0x100000000f000`.
- **PMC versus IBS.** IBS when `auxiliary.ibsOpSampling` is true, or `ibsEvents.op`/`fetch` is set; otherwise a non-empty `pmcEventList` is a PMC capture. `opCycleCount` true means cycle-controlled (time-like sample share); false means op-count (instruction share).
- **Dump kinds.** `ibsop-event-dump` → `IbsOpDump.csv`; `ibsfetch-event-dump` → `IbsFetchDump.csv`. `collect` does not capture IBS fetch; a fetch dump must be produced elsewhere and is supported but unverified.
- **Verified by `collect`:** PMC `hwId` 118, the converted `interval`, `isSampling` true, `auxiliary.ibsOpSampling` false; IBS `ibsEvents.op`, `ibsOpSampling`, `opInterval`, `opCycleCount` against `--ibs-count-control`; `collection.target.systemWide` against the mode, and every requested PID in `attachedProcesses`. A mismatch exits 2.

## Estimator: exact DeltaTick gaps

`AMDuProfCLI translate --ascii ibsop-event-dump -i <copy>` writes the dump; `ibsfetch-event-dump` writes the fetch dump. One row is one sample. Used columns: `DeltaTick`, `CPU_Number`, `PID`, `Offset`, `ModuleName`. `DeltaTick` is a cumulative, start-relative nanosecond timestamp (QPC-derived, 100 ns grid).

Per Hofer, JKU PhD thesis, section 3.2.4:

1. Partition by `CPU_Number`.
2. Order each partition by `DeltaTick`.
3. Weight each sample by `DeltaTick - lag(DeltaTick)`; the first sample per CPU has no preceding interval and no weight.
4. A group's share is `sum(weight in the group) / sum(weight in scope)`.

Rules that follow:

- `--time-filter T1:T2` selects `DeltaTick ∈ [T1, T2)` **before** the lag, so the first sample per CPU inside the window is weightless; filtering never re-translates. The first-per-CPU sample still counts in `pct_samples`.
- A gap spans all work on that CPU since the previous sample, so halted/idle time is charged to the later sample: `system idle process` and `system` carry share. A process total is core time summed over CPUs (10 s on 16 CPUs is about 160 s of weight).
- `(ModuleName, Offset)` maps to `Function.id` through the `UnifiedSampleSeries` rows that carry both; `Offset` is module-relative, and `ModuleName` matches `Module.path` after `module_key` (lowercase, `/` separators). The scope filter runs after weighting, so a gap is charged whole to the sample that ends it.
- `samples` for a dump is the row count (one per dump row); for PMC it is `sum(event column)`, not `count(*)`.
- `pct_cycles = 100 × Σ LEAST(tag, 131070)` for the group divided by the same sum over the scope. The field is a 17-bit counter clipped at 131070; a NULL or non-numeric cell stays NULL and is excluded from both numerator and denominator (never charged the maximum). The projection emits the column only for an IBS OP dump whose `IbsTagToRetCtr` exists and has at least one non-null value.

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

`v_dump_sanity` runs over the whole dump, unfiltered. It raises an error only on genuine
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
leading/trailing, the worst CPU, and `OK`; it says `WARN` when the capture window ends more than one
sampling cadence before the recorded span (no CPU sampled near the end). **Attached captures**
(`collection.target.systemWide` false) cover only the CPUs the attached threads ran on, so
`elapsed x CPUs` is an upper bound there and the message says `OK (attached)` without judging
coverage. No usable duration gives a `WARN` and no comparison; an empty dump raises an error.

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

## Symbolization

`profile --by line|instruction` symbolizes every module in scope; there is no separate command.

- `profile-rvas.sql` exports distinct `(module_name, module_path, rva)` with 0x-prefixed hex offsets; each module's RVAs are written to `module-NNNN/rvas.txt`.
- **Windows PDB discovery.** `llvm-readobj --coff-debug-directory` reads the image's PDB name, GUID and age. Candidates are `<pdb name>` and `<module stem>.pdb` beside the module, then for every `_NT_SYMBOL_PATH` `srv*<cache>*<url>` entry `<cache>/<pdb name>/<GUID><Age>/<pdb name>` and a recursive search. A candidate is accepted only when `llvm-pdbutil dump --summary` reports the image GUID and the internal age matches; for a canonical cache-key directory `<GUID><Age>`, the key is the age authority because the cached PDB's internal age can differ on real symbol servers. Without `llvm-pdbutil`, a PDB beside the module is used with a warning and no GUID verification. Without an image GUID (llvm-readobj missing, failed, or the image records none), no PDB is accepted; one run-level warning names the module count and the remedy, and the addresses become `<no symbol>`.
- **Symbolizer passes.** Two invocations per module, both `llvm-symbolizer --obj=<exe> [--pdb=<pdb>] --relative-address --inlines --output-style=JSON` over `rvas.txt`: `--functions=linkage` (demangled signatures) and `--functions=short` (one identity per function across inline and out-of-line copies). Exit 1 is accepted when records were emitted, with a warning.
- **Per-module Parquet** (`symbols.parquet`): `address VARCHAR, function VARCHAR, function_short VARCHAR, file VARCHAR, line INTEGER, containing_function VARCHAR`. `function`/`file`/`line` come from `Symbol[0]`, the innermost executed frame; `containing_function` comes from `Symbol[last]`, the outermost frame whose name uProf's `Function.id` carries. The combined artifact adds `module_name`; the fold cache `symbols.publics.csv` has `module_name, rva, name_count, names`.
- **Fold cache.** On Windows, `llvm-pdbutil dump -section-headers` plus `-publics` yields public function starts; `(section, offset)` becomes an RVA and names are grouped by RVA. `name_count > 1` is a folded start; `names` lists up to four sorted names then `... (+N more)`. The line and instruction views ASOF-join each sampled RVA to the nearest preceding start per module and label a folded body `shared: ...` with attribution `shared`.
- **Temporary artifacts.** The printed `ARTIFACTS=<temp dir>` holds `module-NNNN/rvas.txt`, `symbols.jsonl`, `symbols.short.jsonl`, `symbols.parquet` and, with a PDB, `module-NNNN/symbols.publics.csv`. On success it is removed and `ARTIFACTS_CLEANED=` printed; on failure it is kept and `ARTIFACTS_KEPT=... (the run failed; kept for inspection)` printed (or the cleanup failure is named).

### Agreement metric

`agreement.sql` joins scoped addresses to `v_symbols` and compares uProf's `Function.name` with the symbolizer's `containing_function`; an inline instance therefore matches its containing function by construction.

| Field | Definition |
|---|---|
| `mismatch_rate` | 100 × `mismatched_samples / compared_samples`; compared means both the uProf name and the symbolizer name are non-empty |
| `name_mismatches` | distinct `(module, RVA)` addresses among the mismatched samples |
| `compared_addresses` | distinct addresses where both names are non-empty |
| `unresolved` / `unresolved_samples` | addresses with no symbolizer name, and their sample weight |
| `unresolved_pct` / `scoped_samples` | unresolved weight as a share of the whole scoped capture |

Samples weight each distinct address (one per dump row, the event count per PMC row). A row with an empty uProf `Function.name` is not compared and is not counted as a mismatch. Names are compared as exact strings; spelling differences between demanglers are normal. `unresolved` is the signal that no symbols were found.

## SQL schema — `tools/views.sql`

One schema file; every object is TEMP because `cpu.db` is opened read-only. `uprof.py` binds eleven inputs as prepared parameters (`SET VARIABLE name = ?`, never text splicing), loads `views.sql` once, then runs one projection.

| Bound input | Meaning |
|---|---|
| `uprof_image` | process base-image ILIKE pattern; `'%'` selects every process |
| `uprof_pid` | one PID as text; empty selects every process |
| `uprof_top` | row cap bound from `--top`: a positive `N`, or `0` for `--top all` (no cap) |
| `uprof_time_start`, `uprof_time_end` | window in ns; `[start, end)` |
| `uprof_ibs_dump` | absolute dump path; empty for PMC |
| `uprof_event_column` | PMC sample column in `UnifiedSampleSeries`; empty for a dump capture |
| `uprof_session_meta` | `session.uprof` path when it exists, else empty |
| `uprof_tag_available` | `true` only for an IBS OP dump whose `IbsTagToRetCtr` exists and has a non-null value, so a fetch dump cannot enable `pct_cycles` even with the column; Python derives it from the sampler kind and `v_dump_sanity` and rebinds it before the projection |
| `uprof_symbols` | combined `symbols.parquet`; empty when absent |
| `uprof_aliases` | combined `symbols.publics.csv`; empty when absent |

| Macro | Definition |
|---|---|
| `module_key(p)` | `replace(lower(p), chr(92), '/')` — the module-path match key |
| `pct(part, whole)` | `100.0 * part / NULLIF(whole, 0)` |

| Mode flag / guard | Purpose |
|---|---|
| `has_dump` | `uprof_ibs_dump` is non-empty |
| `schema_check` | fails once, naming every missing table/column and a bad `event#` column |
| `schema_check_pid` | PMC only: requires `UnifiedSampleSeries.processId`; dump captures do not |
| `dump_guard` | the dump exists and `time_start < time_end` |
| `dump_columns_guard` | one `DESCRIBE`; names missing required dump columns (`DeltaTick`, `CPU_Number`, `PID`, `Offset`, `ModuleName`) |
| `has_tag` | the dump header carries `IbsTagToRetCtr`; `v_dump_sanity.tag_values` counts non-null values |
| `top_n`, `time_start`, `time_end` | typed row cap and window |

The 11 views:

| View | Role |
|---|---|
| `v_dump` | typed dump rows (`delta_tick`, `cpu`, `pid`, `rva`, `module_name`, `tag_cycles`); tagged/untagged branch; an empty typed relation for PMC |
| `v_dump_weighted` | applies `[time_start, time_end)` then adds `weight = delta_tick - lag(delta_tick) OVER (PARTITION BY cpu ORDER BY delta_tick)` |
| `v_dump_sanity` | whole-dump provenance check: samples outside the recorded span bounds, or core time above span × CPUs, error; attached captures report `OK (attached)`; no usable duration or uncovered late time warns; exposes `tag_values` for the empty-tag disclosure |
| `v_symbols` | combined symbolizer artifact: `module_name, rva, function, function_short, containing_function, file, line` |
| `v_public_starts` | public-symbol fold cache: `module_name, rva, name_count, names` |
| `v_scoped` | the one scope: dump branch weights `v_dump_weighted` and maps RVA to `Function.id`; PMC branch counts the event column and leaves `weight` NULL; `--pid`/`--image` apply after weighting |
| `v_scope_totals` | `total_samples` (0 when empty); feeds the empty-scope warning |
| `v_process_rows` | aggregation, ranking, totals and the `<other processes>` remainder |
| `v_function_rows` | by `Function.id`; `<unresolved>` row; attribution `unique`/`unresolved`; `<other functions>` |
| `v_line_rows` | symbolizer join, short-name/overload identity, `<no symbol>`/`<no source line>`/`<multiple>` buckets, fold labels; `<other lines>` |
| `v_instruction_rows` | one row per module + RVA with innermost annotation; `<other instructions>` |

- **Projection files** are one final SELECT over the views: `profile-process.sql`, `profile-function.sql`, `profile-line.sql`, `profile-instruction.sql`; they apply the `pct()` macros and hide internal columns. `profile-rvas.sql` exports the symbolization scope, `agreement.sql` computes the metric, and `schema.sql` lists the live schema. There are no optional schema files: `views.sql` is the only schema.
- **Load order.** Bind all inputs, load `views.sql`, then (for `line`/`instruction`) run `profile-rvas.sql` on the same connection, symbolize, rebind `uprof_symbols`/`uprof_aliases`, and run the projection. `query(CASE ...)` re-binds per query, so the symbol inputs may be bound after the views are created. A hand run writes a UTF-8 `SET VARIABLE` file, `.read`s `views.sql`, then the projection, and always uses the original session's `cpu.db`.
- **Guard order.** The schema and dump guards run while `views.sql` loads, before any query, so an incomplete export or dump fails once with the missing item names. The schema guards read the bound path strings (empty means absent) so no identifier is spliced; the dump guard checks that the dump exists. A conditional `query(CASE ...)` avoids binder-eager branch evaluation.

## Reproducibility and tie order

The same capture and arguments produce byte-identical output on every run. Each row view ranks by weight, then samples, then a stable key; the remainder row is last:

| `--by` | ORDER BY |
|---|---|
| `process` | `weight DESC NULLS LAST, samples DESC, pid, process` |
| `function` | `weight DESC NULLS LAST, samples DESC, function_id NULLS LAST` |
| `line` | `weight DESC NULLS LAST, samples DESC, file, line, function, module` |
| `instruction` | `weight DESC NULLS LAST, samples DESC, module, rva NULLS LAST, function` |

For PMC every weight is NULL, so ranking falls through to `samples`; the share-sum caveats are in `SKILL.md`, What a share measures.

**Display escaping.** Table cells and header values escape controls as described in `SKILL.md`, Output; JSON carries the true characters, so table/JSON parity is modulo that display escaping. `--out-file` receives the same bytes as stdout.

## Known limitations

- **Inline overloads.** The display rule is in `SKILL.md`, Frame rule and sentinels; a true overload set (two or more out-of-line signatures) shows `<multiple>` on a shared source line.
- **Empty or junk tag column.** `has_tag` tests column presence; `uprof_tag_available` requires an IBS OP dump and at least one non-null value. An all-NULL or non-numeric `IbsTagToRetCtr` hides `pct_cycles` and prints the empty-column disclosure; a non-numeric cell is NULL, never the clipped maximum.
- **Module paths are lowercased.** `module_key` lowercases and replaces `\` with `/`; the module column shows that normalized spelling, and two paths differing only in case or separator collide.
- **Fold detection is Windows/PDB only.** Resolved Linux DWARF symbols are all `unique` (unverified here); unresolved rows still exist. Without `llvm-pdbutil` the cache is skipped and a note is printed.
- **Duplicate module copies.** Two copies of one DLL (for example two driverstore paths) are
  separate `module_name`s; merge them with an ILIKE pattern or aggregate by basename.
- **Nearest-export mapping is not valid for a symbol-less DLL with a sparse export table.**
  Joining a hot RVA to the nearest preceding export names an unrelated small function; treat that
  as no attribution and use RVA-level samples plus disassembly.
- **`--json --out-file` writes no file when the query fails** (including the dump sanity error);
  the reason is on stderr and the exit code is 2, so check the exit code before reading the file.
- **`<multiple>` is coarse.** A source line carrying several overload signatures is reported as one ambiguous annotation, not split per signature.

## Troubleshooting

The failure table in `SKILL.md` covers dump sanity, missing `session.uprof`/`cpu.db`, PDB misses, empty scope, and output/filesystem errors. This table adds the remaining remedies:

| Symptom | Cause | Command or remedy |
|---|---|---|
| `<no symbol>` rows, `PDB=none` | no PDB, no matching GUID/age, missing binary, or missing llvm-readobj | install LLVM; add the vendor `srv*<cache>*<url>` to `_NT_SYMBOL_PATH` and re-run `translate`; check the module path |
| `MISSING ...` from `tools` | uProf CLI, DuckDB 2.x or llvm-symbolizer not found | install or set `AMDUPROF_CLI` / `LLVM_SYMBOLIZER`; `python -m pip install duckdb` |
| `uProf export incomplete: missing ...` | uProf export differs from the expected schema | run `schema --session <dir>` and compare; use a matching uProf version |
| `error: argument --image: not allowed with argument --pid` | both scope flags given | pass one |
| capture directory locked during `translate` | open in the GUI | close it and retry |
| `--out-dir already holds a previous translation` | a reused output directory still has `cpu.db`, `callstack.db` or an ASCII dump | pass `--force` to replace the generated files and copied inputs, or choose another `--out-dir` |
| `SAMPLER: falling back ...` | VBS or hypervisor blocks IBS counters | disable VBS/Hyper-V, or accept cycle sampling |
