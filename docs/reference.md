# Reference

Detail behind `SKILL.md`. The tools and `tools/views.sql` are the authority.

## Header order

Header fields are emitted in this fixed order, with optional fields omitted when they do not apply:
`VERSION`, `METRIC`, optional `FILTER` and `PARENT`, `COLUMNS`, `READ`, optional `NEXT`, `DUMP`,
optional `DB`, `TIME_SHARE`, `TAG_TO_RETIRE_SHARE`, `WEIGHTS`, optional `SANITY`, `ACHIEVED`,
`KERNEL_COVERAGE`, `HOT_RVA`, `SYMBOL_COVERAGE`, `WARNING`, `AGREEMENT`.

## Granularities

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
- **PMC versus IBS.** IBS when `auxiliary.ibsOpSampling` is true, or `ibsEvents.op`/`fetch` is set; otherwise a non-empty `pmcEventList` is a PMC capture. `opCycleCount` true means a cycle-controlled interval and false an op-count interval.
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
  cannot exceed the capture span.
- `(ModuleName, Offset)` maps to `Function.id` through the `UnifiedSampleSeries` rows that carry both; `Offset` is module-relative, and `ModuleName` matches `Module.path` after `module_key` (lowercase, `/` separators). The scope filter runs after weighting, so a gap is charged whole to the sample that ends it.
- `samples` for a dump is the row count (one per dump row); for PMC it is `sum(event column)`, not `count(*)`.
- `share(part, whole) = part / NULLIF(whole, 0)`: every share is a fraction 0.0-1.0, printed at full precision.
- `tag_to_retire_share = Σ LEAST(tag, 65535)` for the group divided by the same sum over the scope. Only an unsigned decimal integer cell counts; NULL, negative, fractional and exponent spellings stay NULL and are excluded from both numerator and denominator (never charged the maximum). The projection emits the column only for an IBS OP dump whose `IbsTagToRetCtr` exists and has at least one nonzero value.

### Anchoring a workload's clock

`collection.schedule.start` and `end` are QPC-derived nanoseconds (their difference equals
`elapsed` to the digit), and a sample's `DeltaTick` is `sample_ns - start`. A workload that prints
its own `QueryPerformanceCounter` reading can therefore be placed on the capture timeline with
`uprof.py anchor --session S --qpc <ticks> --freq <hz>`:

    relative_s = (qpc * 1e9 / freq - start) / 1e9

A negative value is attach latency: the reading precedes the capture start, so the capture does not
cover it. `startTs`
(local time, one-second precision) gives the UTC start where a wall-clock comparison is needed;
`--time-filter` takes the same capture-relative seconds.

The session records no QPC frequency — only the core MHz used for the collect interval conversion
— so `--freq` defaults to 10,000,000 Hz (the Windows QPC frequency) unless it is given. Pass it
when the workload's clock differs. `HZ_SOURCE=argument` means `--freq` supplied the value;
`HZ_SOURCE=default` names the 10 MHz default.

### Dump resolution and cache

1. A dump beside the session (preferred).
2. A current cached dump under `<user cache>/uprof-dump-cache/<sha256(normcase(abspath(session)))[:16]>/` (`%TEMP%`, Linux `~/.cache/amd-uprof`; a cache directory that is a link or open to other users is refused). It is reused only when `cache.json` records the same kind and the same `translateCommand --time-filter`, and the same name, size and SHA-256 of every raw `cpu/*.prd` (a digest is kept unhashed while file ID, size and mtime match).
3. Otherwise a copy of the raw session is translated with `--ascii` in a unique staging directory, renamed into place when complete; `GENERATING DUMP:` precedes it.

The cache holds no database: every `.db`/`.duckdb` (and its WAL/SHM) is removed before a cached dump is handed back, because a dump-only translate writes a degraded `cpu.db` with far fewer `Function` rows; if the removal fails it prints `WARNING: could not remove <path>: <reason>; the cache copy still contains a database`. `cpu.db` must always come from the original session.

The cache also holds a Parquet copy of the seven columns `time_share` reads:
`IbsOpDump.samples.parquet` (the dump stem plus `.samples.parquet`) with a `.json` sidecar beside
it in the same directory. The sidecar records the format version, the column list, the normalized
source path, the source size, mtime and sha256, and the copy's size and sha256. The copy is reused
only when the source size and mtime match and the copy still matches its recorded size and digest;
when the source's size or mtime changes, its sha256 decides whether the copy is reused, and a
changed dump rebuilds it. A dump without one of the seven columns (for example a fetch dump,
which has no tag column) falls back to the CSV; the failed build is recorded and not retried until
the source changes. Any failure falls back to the ASCII dump.

### Session cache

`profile` may store the symbolization artifacts — symbols, public symbols, PDB choices and
derived function ranges — in one DuckDB database per capture session. On Windows it lives under
`%LOCALAPPDATA%\Symbols\uprof-session-cache`, elsewhere under
`$XDG_CACHE_HOME/amd-uprof/session-cache` (or `~/.cache/...`); the file name is a digest of the
session path and `cpu.db` size/mtime. `UPROF_SESSION_CACHE_ROOT` relocates the root,
`UPROF_SESSION_CACHE_DISABLE=1` (also `true`/`yes`/`on`) opts out. A database is capped at
1024 MB and attach/store retries stop after 30 s.

The capture `cpu.db` stays read-only and is refused if it stores a macro, view or generated column. The cache is attached read-only for lookups and read-write
for a run that stores; DuckDB permits one writer per file, so a busy cache is waited for up to
the timeout and then the run continues uncached without disabling it. A file that cannot be
opened is moved aside as `.corrupt` and recreated once; a file written by another DuckDB storage
version or a newer cache schema is left untouched and the run continues uncached. A missing table
left by an interrupted store is repaired. Any remaining cache error disables the cache for the run and the profile
continues uncached; a cache error must not fail the profile.

Artifacts are tables, not files. A run is keyed by the capture-side module identity (path, size,
checksum, 32/64-bit and the debug-info flag when recorded), the queried RVA set, the on-disk
image state, the symbolizer/pdbutil/readobj builds, the symbol and image search state (explicit
files and directories, the module directories, the symbol cache root) and the cache version. A
changed binary, tool, scope or search state misses. The resolved PDB files are fingerprinted by
size/mtime and validated on every hit, so a changed PDB misses. A run with a transient tool
failure (a missing module image, a failed readobj, or a symbolizer failure other than a
wrong-build PDB) is not stored. A stable no-symbols outcome (no recorded debug info, no PDB, or
a wrong-build PDB) is stored together with the search state, so a PDB appearing in a searched
location changes the key. A module name the capture maps to more than one image version is not
symbolized or cached: IBS dump rows carry the module name, not `Module.id`, so per-version
attribution is unavailable; the run warns and the module is reported unresolved. Function
ranges are bound to the symbolization key they consume.

On a hit the run reads the cached tables through the same artifact views and replays the
symbolization and range text, so stdout is identical to the cold run. The replayed text carries
the cold run's temporary paths and the artifact lines are not reprinted, so stderr is not
byte-identical. `views.sql` defines file-backed source views itself, so a hand-run query with the
artifact variables set works; a cache hit replaces them with table reads.

The cache is per session and never shared across sessions. A database over the size cap refuses
to store the run and warns; there is no LRU eviction. Orphaned databases whose recorded session
directory is gone are removed on startup, and a rewritten `cpu.db` (a new digest) removes the
stale sibling databases for the same live session rather than waiting for the TTL; unreadable
databases are removed only after 7 days. Cleanup touches only the
digest-named `.duckdb` files and their `.corrupt` copies, so other files in a shared root stay. If a wrong-build PDB is overwritten in place in a location whose
directory identity does not change, delete `<root>/<digest>.duckdb` or set
`UPROF_SESSION_CACHE_DISABLE=1`; probe candidates are fingerprinted, which covers the normal
staging paths.

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

Normal captures never reach that bound. The message starts with the verdict and scope in a
fixed form: `OK, system-wide` when the last sample lies within one sampling cadence of the
recorded span's end, `WARN, system-wide` when the capture window ends earlier (no CPU sampled
near the end); only the WARN reports the covered core time and the boundary gaps split
leading/trailing with the worst CPU, since numbers beside an OK were read as a defect. **Attached captures**
(`collection.target.systemWide` false) partition gaps by `(PID, TID)`, not by CPU, so the verdict
is thread coverage, never a CPU capacity: `elapsed x CPUs` was meaningless for a single-thread
process and hid the per-CPU overcount. A thread holding a sample outside the recorded span is an
error, the same out-of-span error as system-wide; the last-to-first span of an in-span thread
cannot exceed the capture span by construction. The attached `OK` line names no thread's
covered time against the span: one thread's busy time is not a capture window. No usable
duration gives a `WARN` and no comparison; an empty dump raises an error.

When `--image`/`--pid` or `--time-filter` narrows the report, `v_scope_sanity` appends a
`scoped coverage (<scope>)` verdict to the same field. It takes the last sample of `v_scoped` and
compares it with the end of the scope window — the recorded translate span clipped to the profile
`--time-filter` window — against the scope's own sampling cadence (the window width divided by the
busiest in-scope thread's sample count). The verdict says `WARN` and names the scope when the
scope's late time is uncovered, and `OK` when the scope sampled near the end of its window.

An empty scope prints one `WARNING: no samples in the selected scope` on stderr before the document is written; with `--image` it names the capture's `Process.name` values and states that `--image` filters on `Process.name`, not modules.

### Achieved sample rate

`profile` and `translate --ascii` print one `ACHIEVED:` line for every capture; the tail is present
only when per-context statistics are available.

Samples are counted over the whole capture, not the `--image`/`--pid` scope; CPUs come from the
dump's `CPU_Number`, or from `UnifiedSampleSeries.coreId` for a PMC capture. The per-CPU figure divides by every CPU, so it is not the delivered cadence. For an attached
capture whose busiest thread has a measured cadence, the line therefore leads with that thread's
rate against the request and labels the per-CPU figure as diluted.

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
running gaps are at the requested interval.

`collect` cannot print this — it has neither a dump nor a `cpu.db`; the rate appears at `profile`,
where the samples exist, or in `translate --ascii`, which counts the dump it just wrote. For an
op-count interval no time rate follows and the line says so.

### Kernel coverage

The OS-mode request is read from the applied event: PMC `os`, or an enabled IBS sampler's `opOs`/`fetchOs`; a disabled sampler's bit is ignored.

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

## Symbolization

`profile --by function|region|line|instruction` symbolizes every module in scope; there is no separate command.

- `profile-rvas.sql` exports distinct `(module_name, module_path, rva)` with 0x-prefixed hex offsets; each module's RVAs are written to `module-NNNN/rvas.txt`.
- **Windows PDB discovery.** `llvm-readobj --coff-debug-directory` reads the image's PDB name, GUID and age. Candidates, in order: an explicit `--symbol-path` PDB file; each `--symbol-path` directory root, as `<dir>/<pdb name>` and `<dir>/<GUID><Age>/<pdb name>`; `<pdb name>` and `<module stem>.pdb` beside the module; the absolute PDB path the CodeView entry records, unless it is a network or device path; then `<d>/<pdb name>` per `_NT_SYMBOL_PATH` dir; in every store (each `_NT_SYMBOL_PATH` store, the default symbol cache (a non-default translate `--symbol-cache` is searched only when passed as `--symbol-path`, since the capture is what records it), and each `--symbol-path` directory holding a `<pdb name>` folder), `<store>/<pdb name>/<GUID><Age>/<pdb name>` and a recursive search under `<store>/<pdb name>`. llvm-symbolizer itself ignores `_NT_SYMBOL_PATH`: its PDB sources are only an adjacent file, the recorded CodeView path or an explicit `--pdb`. The skill's own search is what finds a cached PDB and passes it as `--pdb`; only local stores are read and the URL is never contacted, because LLVM has no Windows symbol-server client. `--image-path` supplies the image (a file, or a directory searched by module basename) when the recorded path is not on this host, and the resolved image is passed as `--obj`. A candidate is accepted only when `llvm-pdbutil dump --summary` reports the image GUID and the internal age matches; for a canonical cache-key directory `<GUID><Age>`, the key is the age authority because the cached PDB's internal age can differ on real symbol servers. Without `llvm-pdbutil` or an image GUID (llvm-readobj missing, failed, or the image records none), no PDB is used, because a PDB picked by name may be another build's: the module gets a `no_pdb` evidence line naming the tool or image to supply. A `--symbol-path` PDB no module used gets one `WARNING:` with the cause. The evidence for a non-resolved module names the image GUID/age, the debug-info record, the searched locations, every `srv*` URL not consulted, each candidate's pdbutil exit/stderr and GUID/age mismatch, and the symbolizer's exit status and JSON `Error` with the remedy. `profile` prints one evidence line per distinct cause, naming its modules (five, then a count), so a module whose cause is shared may not be named on stderr; each module's own cause also appears in `SYMBOL_COVERAGE`. A module without symbols prints no MODULE/PDB/FOLDS progress lines, and a `--function ... --module` view prints the lines of the scoped module only.
- **Symbol-path flags on `profile`.** See `SKILL.md`, Profile. `translate` keeps `--symbol-cache`, `--symbol-server` and its own `--symbol-path` for staging.
- **Symbol servers of `translate`.** A set, non-empty `_NT_SYMBOL_PATH` names exactly the servers translate passes to AMDuProfCLI: each `srv*` entry, including `srv*<url>` with the default downstream store. To use no symbol server, so no PDB name or GUID leaves the machine, set `_NT_SYMBOL_PATH` to local directories only, for example `cache*C:\symbols` or `C:\symbols`. Only an unset or empty variable gives the default: Microsoft's server on Windows, none on Linux. `--symbol-server` adds a server either way; a plain `http://` server prints a `WARNING:`, since its PDBs can be replaced in transit.
- **Stalled `translate`.** AMDuProfCLI fetches symbols during translate and can stall silently on a slow or unreachable symbol source. translate stops it when the CLI log, the out-dir and the symbol cache all stop changing for 10 min, or after 95 min in total, and fails with the cause, the symbol servers it passed and an `ALTERNATIVE:` that re-runs translate with `--force` and `_NT_SYMBOL_PATH` set to a local folder only. On a healthy translate those three signals together never paused longer than 72 s, while a cold symbol cache took 22 to 31 min.
- **Wrong-build PDB.** Empty `FunctionName`/`Line` 0 frames with exit 0 are a resolution failure tied to the PDB name (`probably for another build`).
- **Verify translate symbolization.** Translate exits 0 even when it named nothing; read the `SYMBOLIZATION:` counts, not the exit status.
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

- **uProf DB channel.** `tools/coverage.sql` reads `Function.name` from the `cpu.db` that AMDuProfCLI translate produced. A sample is named when its `Function.name` is non-empty; a NULL `function_id` (no `UnifiedSampleSeries` address mapping) and a blank name both count as unresolved. `profile`'s `--symbol-path` and `--image-path` do not change this channel: it records only what translate symbolized.
- **Profile symbolizer channel.** For `--by function|region|line|instruction`, the rows are named from the parquet that `profile`'s own llvm-symbolizer invocation wrote. `agreement.sql` joins `v_scoped` to that parquet and counts a sample unresolved in the symbolizer channel when the symbolizer produced no name for its address; the resolved share is `scoped_samples - unresolved_samples`. The emitted function/region rows reclassify a sample covered by a derived range as `derived`, so report the row attribution, not the channel count. Only the explicit symbol-path flags in the line's `flags:` list affect this channel.

For `module` and `process`, which run no symbolizer, the line keeps its single-channel form: `<named>/<total> samples (<percent>%) resolve to a named function`, followed by one `<module> <unresolved>/<samples> (<percent>% unresolved, partial|no source names)` entry per module with unresolved samples. For `line` and `instruction` the line names both whenever llvm-symbolizer ran, also when it failed and resolved nothing: `uProf DB <named>/<total> ...; profile symbolizer <resolved>/<scoped> ... (flags: <flags>)`. Each per-module entry may add `; cause: ...` from the symbolization evidence; when a cause is known, and whenever the symbolizer channel leaves samples unresolved, the line ends with `; remedy: ...` naming the flags that can change the row channel. The stderr evidence line carries the full detail.

The stop rule follows the channel that names the rows. For `function` and `region` the line reports the emitted rows as named, derived (synthetic binary range) and unresolved samples; a derived row is attributed and never counted as unresolved, so the line can reach the no-stop verdict even when the symbolizer named nothing. When the symbolizer resolves every scoped sample, the line says so and does not print the boundary text, even if the uProf DB share is 0%: those rows are drillable.

JSON carries the same distinction under `legend.symbol_coverage`, in fixed order: `uprof_db` (`source`, `named_samples`, `scoped_samples`), `flags` (the explicit symbol-path flag names used, such as `--symbol-path`, not their path values), `profile_symbolizer` (`source`, `resolved_samples`, `unresolved_samples`, `scoped_samples`, or `null` when the granularity does not symbolize) and `rows_drillable` (`true` only when the symbolizer resolved every scoped sample). For `function` and `region`, `profile_symbolizer` instead carries `named_samples`, `derived_samples`, `resolved_samples` (named plus derived), `unresolved_samples` and `scoped_samples`, and `rows_drillable` is true when the unresolved count is zero: a synthetic range is actionable at its hot RVA.

### Derived function ranges

Function is the semantic unit. A release binary often has no debug symbols, so `--by function` derives function boundaries from the image itself with the LLVM tooling already used for symbolization; the consumer does not write a PE parser, does not read `.pdata` by hand and does not map addresses itself.

- **Boundary sources**, strongest first: a PE export (`llvm-readobj --coff-exports`), a PDB public start (`llvm-pdbutil dump --publics` and the `symbols.publics.csv` fold cache), then a `.pdata` runtime-function start (`llvm-readobj --unwind`). At one RVA the stronger source keeps its name; a `.pdata` start with no name is a boundary but no name.
- **Range.** A code section's selected starts are sorted and each range runs from a start to the next start, or to the section end. A section with no boundary source contributes no range, so the tool never invents one whole-section function. `function_source` is `export`, `pdb_public`, `pdata` or `none`; `function_derived` is true only for a range with no export or public name.
- **Name.** An export or PDB public supplies its name. A boundary with no name is labelled visibly synthetic: `pdata_0x<start>` for a `.pdata` range and `sub_0x<start>` otherwise. A sample inside a range groups under it even when uProf's `Function.id` or the symbolizer assigned another name; a sample outside every range keeps its `Function.id` row, or `<unresolved>` with a `function_reason`.
- **Attribution.** `unique` means a named range or a named uProf `Function.id`; `derived` means a range with a synthetic name; `unresolved` means neither. `unresolved` is only this attribution category: a module whose boundaries came from a derived range is `derived`, even when every name is synthetic, and must be called "derived" or "unnamed at source level", never "unresolved" or "no named functions". `legend.function_ranges` reports `source`, `derived_modules`, `ranges`, `absent_by_module` (module → why no range was derived: no path, missing file, `llvm-readobj` missing, not a PE image, no code section, or no boundary source) and the `fallback` (a fully named module is skipped and says so; a module with no derived range keeps `Function.id` and `function_reason` says why).
- **Cost.** Only modules with at least one empty `Function.name` are read; a fully named module keeps its `Function.id` rows, costs no extra tool run, and its `function_reason` says `fully named from the uProf DB; function-range derivation skipped`.

### Function-scoped instruction and line views

`profile --by instruction|line --function NAME --module MODULE [--function-start RVA]` filters the rows to one `--by function` row. Copy `function`, `module` and, when present, `function_start` from that row; there is no name-only or module-only scope and no implicit "current function". `--function` is rejected with any other `--by`.

- **Scope.** A derived range filters by address: `module + RVA in [function_start, function_end)`. A row with no derived range filters by uProf `Function.id`, so `--function` still works for a fully named module, where derivation was skipped. When the name matches more than one row, `--function-start` selects one; two no-range rows with the same name cannot be separated and are reported as such. A synthetic range keeps its visible `pdata_0x<start>`/`sub_0x<start>` label.
- **Parent measurement.** The parent totals come from `v_function_measured`, the same uncapped function aggregation that feeds `--by function`, so they are a sum/join of the function's own rows, never a second measurement. The header carries `FILTER:` with the explicit scope and `PARENT:` with the parent's `samples`, `sample_share` of the scoped capture, `time_share`/`tag_to_retire_share` (or the reason each is absent) and `cpu_seconds` when the mode supplies it.
- **Two denominators.** A row keeps the ordinary capture-level shares and adds `parent_sample_share`, `parent_time_share` and `parent_tag_to_retire_share` (when applicable): its samples, gap weight or tag cycles as a share of the parent function. The `--top` remainder row carries the same parent shares, so the truncated rows and the remainder sum to the parent total. `JSON` carries the same measurement under `legend.function_scope`, with `range_kind` (`derived_range` or `uprof_function_id`), a `parent` object (`samples`, `sample_share`, `time_share`, `tag_to_retire_share`, `cpu_seconds`, each share `of the scoped capture`), the row columns' `denominators`, `measurement` and `child_shares`.
- **Line view.** The line view needs source positions; a function whose samples resolve to no source line appears as one `<no source line>` row relative to the parent, not as an empty table. The function name and range come from the binary, so the row still names the function when it has no source position.

### Region fallback — 4 KiB address pages

`--by region` is the fallback only when the function rows are in the `unresolved` attribution and `function_source` is `none`, or when an explicitly requested page-level view needs it. region = 4 KiB address page; samples are grouped by address, not by control flow; a page may span unrelated code; the hot RVA is the most precise attribution and the page is the coarse bucket. The row keeps `sampled_addresses` so a page with many scattered addresses is distinguishable from a dense one, and `hot_rva`/`hot_samples`/`hot_sample_share` carry the sharpest address. When the hot RVA falls in a derived range, the row names that function and its source; otherwise `function_source` is `none`.

### Ranking and the hot row

The sample-hot address is the `hot_rva` with the largest `hot_samples`, and it is not necessarily inside the top-ranked row: a function may rank first by `time_share` while a different function's address has the most samples. `--by module` carries each module's `hot_rva`/`hot_samples`/`other_samples`/`hot_sample_share`; function rows carry the same four. `other_samples` is `samples - hot_samples`: the rest of that row, not a second hot address. `--by function` prints one `HOT_RVA:` summary computed as the maximum `hot_samples` across all function rows, not the top row's `hot_rva`, and reports that row's `other_samples` with it. Use those for a sample-hot claim. The instruction table follows the same mode measure: it is sample-hot for cycle/op/PMC captures, but for a time-based capture it is `time_share`-ordered and must not be used to claim the sample-hot RVA or that no single instruction exceeds a sample threshold.

## Module and CPU-time questions

### Module rollup

`profile --by module [--json]` is the supported rollup: one row per normalized module path with `samples`, `sample_share`, `time_share` (when the mode supplies it), `tag_to_retire_share` (a dump with a nonzero tag column), `isSystemModule`, `cpu_seconds` and the module's highest-sample address as `hot_rva`/`hot_samples`/`other_samples`/`hot_sample_share` (`other_samples` = `samples - hot_samples`, the rest of the module). Use these fields; do not roll functions up in ad-hoc SQL or query `cpu.db` for them. One text call answers the module question and names its sample-hot address; do not also run `--by module --json` to sum third-party rows from the same capture. Drill into the module with `profile --by function`, which derives boundaries when translate named nothing.

**Third-party** is the rule `Module.isSystemModule = false`. `true` is uProf's system-module flag; it covers OS components and some vendor drivers (for example `amdvlk64.dll`), so it is neither an OS-shipped predicate nor a kernel predicate. The flag marks user modules such as `ntdll.dll`/`kernelbase.dll`, and a kernel image (`.sys`/`.ko`, a kernel basename, or a `ModuleInstance.loadAddress` in the kernel half) is expected to be system but that is not measured on the cycle-controlled capture. The `module` key is the normalized dump `ModuleName` or, for a PMC capture, the normalized module path; exact module matches take precedence over basename matches, and an ambiguous basename whose catalog versions disagree resolves to NULL with that cause stated in the column description. The column is absent from older exports, and the output then says so and leaves `isSystemModule` NULL. A system-flagged driver can still dominate the capture: report the strict third-party total and the dominant non-workload dependency separately rather than inspecting the path or re-deriving the rule by hand.

### Absolute CPU time

`--by module` emits `cpu_seconds` as `samples × interval / core_frequency`: `samples` is the row count and `interval` is the requested cycle count converted at the core frequency `ACHIEVED` reports, the capture's nominal execution clock. The session records the interval in cycles, not the frequency of the capture machine, so a capture from other hardware gives an approximate figure and the seconds are an estimate at that clock, not a measured cycle count. The conversion does not apply to op-count or fetch samples: their interval is work, not time, and `cpu_seconds` is omitted with that reason.

## Instruction context — `--by instruction --json`

`uprof_query` calls `uprof_context.instruction_contexts()` before rendering JSON for instruction scope; Markdown never builds or receives the block. The block answers "what instructions surround this hot address" without replacing the row's own numbers.

**Anchor.** The hot RVA must lie in an executable PE section (`IMAGE_SCN_CNT_CODE` or `IMAGE_SCN_MEM_EXECUTE`). Safe starts are the `v_public_starts` PDB publics, the PE export table (`llvm-readobj --coff-exports`) and the `.pdata` runtime functions (`llvm-readobj --unwind`), all inside that section at or below the hot RVA. The nearest start wins; at the same RVA an export is preferred, then a PDB public, then a `.pdata` start. When it is within 1 MiB it is the anchor; otherwise the section start is used if it is within 1 MiB; otherwise `anchor_too_far`. `anchor.kind` is the source of the chosen start: `export`, `pdb_public`, `pdata` or `section`. The anchor is where decoding begins — x86 has no backward decode, so an anchor after the hot RVA cannot help.

**Window.** `llvm-objdump -d --start-address=image_base+anchor --stop-address=... --no-show-raw-insn --no-symbolize-operands` decodes forward; the stop address reserves 32 instructions after the hot RVA and is clamped to the containing section's end. The window is the last up to 32 decoded instructions before the hot RVA and up to 32 after it, plus the hot instruction, sorted by RVA in the JSON. A hot RVA that never appears as a decoded boundary is `hot_rva_not_instruction_start`; it is never snapped to the preceding boundary. Objdump's nearest-symbol label is discarded — only address and instruction text survive.

**Per-instruction fields.**

| Field | Definition |
|---|---|
| `rva` | module-relative address, `0x` hex |
| `instruction` | mnemonic and operands as `llvm-objdump` prints them, raw bytes and trailing comments removed; it may retain objdump's nearest-symbol `<...>` decoration, which is not the frame-0 name — use the run's `function` |
| `samples` | raw scoped samples at this exact RVA |
| `sample_share` | `samples / v_scope_totals.total_samples`, a fraction 0.0-1.0 like the row's own `sample_share`; the mode-specific meaning is the one in `legend.columns.sample_share` — sampled frequency for op-count and fetch, core-cycle share for cycle-controlled — not impact and never gap-weighted time; `null` when the scope is empty |
| `import` | optional `{dll, name, slot_rva}` when the instruction is a `call`/`callq` whose operand is a `*disp(%rip)` or absolute IAT slot; register and indirect non-IAT targets stay raw; an ordinal-only symbol is `#<ordinal>` |
| `ibs` | optional aggregated IBS OP labels, present when the dump has a row at the RVA and supplies at least one whitelisted field; the summary may carry only `samples` |

**Frame 0.** `function`, `file` and `line` come from `llvm-symbolizer` frame 0 (`Symbol[0]`) using the PDB recorded by the symbolization pass, stated per run, not per instruction. The context names the `function` and `file` most of the window shares (the first seen on a tie); each run of consecutive instructions with the same frame gives its `line` and names `function`/`file` only where they differ. `null` means the frame has none (`line` 0 is `null`); an empty or errored record is a run with all three `null`.

`samples` counts how often the hardware selected the instruction for sampling: it is sampling/dispatch frequency, not impact, and a high count does not make the instruction costly. The per-instruction IBS labels are the evidence for the instruction's cost.

When every context is omitted with `symbolizer_failed`, stderr prints one `CONTEXT:` line naming the shared detail, the count and the remedy; that is one missing-PDB finding, not one failure per row, and `uprof_context.py`/`context_help` need not be read to confirm it. Each omitted row also carries `omitted: true` and its `reason` (for example `symbolizer_failed`) in the JSON, so a single unscoped instruction run can be filtered by `module` and read row by row; do not grep stderr to identify failed rows.

**IAT resolution.** `llvm-readobj --coff-imports` is parsed as text — its JSON style is not valid JSON. Each `Import` block's `ImportAddressTableRVA` plus `symbol index * slot width` (8 bytes for PE32+, 4 for PE32) gives the slot RVA; symbols keep the block's order and the parenthesized hint/ordinal is not part of the name. A `callq *disp(%rip)` slot is `next_rva + disp`, using the following decoded instruction for the RIP value; an absolute operand is converted to an RVA by subtracting `image_base` before the lookup. `DelayImport` blocks are not IAT slots.

**IBS labels.** Only fields present as dump columns are emitted, in this order: `DataSrc`, `CacheHitSt`, `IbsDcMiss`, `IbsOpBrnMisp`, `IbsDcLockedOp`, `IbsOpMemWidth`, `IbsTagToRetCtr`. The labels are the hardware field names, not semantic classes, and the values are AMD-encoded counts or histograms, not decoded meanings.

| Field | Aggregate |
|---|---|
| `DataSrc`, `CacheHitSt`, `IbsOpMemWidth` | `{code: sample count}`, keys sorted numerically as printed by the dump |
| `IbsDcMiss`, `IbsOpBrnMisp`, `IbsDcLockedOp` | `{"set": n, "of": samples}` — count of nonzero values |
| `IbsTagToRetCtr` | `{"samples", "sum", "max"}` over the RVA's dump rows with the `tag_to_retire_share` policy: only an unsigned decimal integer cell counts, each clipped at 65535, the 16-bit field's maximum; NULL, negative, fractional and exponent cells are excluded, and `samples` counts the qualifying cells, not dump rows; omitted when no cell qualifies |

`ibs.samples` is the dump-row count at the RVA, counted over the same PID, image and time window as the row's samples. For a PMC capture, or an IBS dump without these columns, no `ibs` field is emitted.

**Build identity.** `build` carries `verified`, `identity`, `image_base`, `time_date_stamp`, `checksum`, `capture_checksum`, `size_of_image` and `capture_size`, all hex where numeric.

**Omission shape.** `{"omitted": true, "reason": "<code>", "detail": "<text>"}`; `detail` is present only when non-empty. An omission made after the module was examined also carries the same `build` object as a successful block, so an unverified build or a `SizeOfImage` mismatch is visible on the omitted row; `build_mismatch` puts the mismatching checksum in `detail` and `build.warning`. Reasons: `module_missing`, `readobj_missing`, `readobj_failed`, `metadata_incomplete` (headers lack `ImageBase`/`TimeDateStamp`/`CheckSum`/`Magic`, e.g. ELF), `no_code_section`, `anchor_too_far`, `hot_rva_not_instruction_start`, `objdump_missing`, `objdump_failed`, `symbolizer_missing`, `symbolizer_failed`, `imports_failed`, `build_mismatch`, `ambiguous_module_version` (the capture maps the module name to more than one image version), `context_failed` (an LLVM tool timed out or a scoped query failed), `row_cap`. Symbolizer failures (nonzero exit, JSON `Error`, or a PDB that resolves no address) and an unreadable import table omit the block with their evidence; an unresolved individual IAT target leaves only that optional field absent. A build without matching checksum or size evidence is emitted unverified, not omitted. The `<other instructions>` row (`rva` null) has no context.

**Document help.** A single top-level `context_help` object is emitted once per JSON document, after `legend` and before `rows`. Its keys are always in this order: `scope`, `share`, `fields`, `omission_reasons`, `read`, `next`. It is built from constant text plus the document's legend, so repeated runs produce identical bytes.

| Key | Content |
|---|---|
| `scope` | one context per instruction row; the 20-row cap counts row slots in the table's row order, so a row that omits for another reason still consumes a slot; rows past the cap carry `row_cap`, and the `<other instructions>` row carries none |
| `share` | `sample_share` is a fraction; its unit and denominator are `legend.share_unit`, `legend.share_scale` and `legend.denominators.sample_share`, and its mode-specific meaning is `legend.columns.sample_share` — the help states no second scale |
| `fields` | `anchor` (`kind`, `rva`, `name` when named, `distance` in bytes from the anchor to the row's rva), `build` (`verified`, `identity`, hex identities and sizes, `warning` when present), `function, file` (the frame-0 function and file most of the window shares), `runs` (static stream in ascending rva, grouped into runs of consecutive instructions with the same frame 0; each run has `line` and `instructions`, and `function`/`file` only where they differ from the context, `null` meaning none), `runs[].instructions` (`rva`, `instruction`, `samples`, `sample_share` fraction, optional `import`, `ibs`; `instruction` may retain objdump's nearest-symbol `<...>` decoration, so use the run's `function` for the frame-0 name), `runs[].instructions[].import`, `runs[].instructions[].ibs` (including the 65535 clip, the unsigned-decimal-integer policy and the note that values are AMD-encoded counts/histograms, not decoded meanings), `omitted` (carrying `build` when the module was examined), and `row.attribution` only when the legend carries that column |
| `omission_reasons` | every reason code with its meaning, in the order listed above |
| `read` | the sampled instruction is the entry whose `rva` equals the row's; `sample_share` is the mode-specific raw share, not a per-op cost and not a ranking; rows are ordered by `legend.impact.order_by`; imports and symbol frames identify the work kind; when the legend carries `tag_to_retire_share`, it and `ibs.IbsTagToRetCtr` are total tag-to-retire dwell evidence, not per-op latency |
| `next` | defer to `legend.next`; rank by `legend.impact.order_by`; a high `sample_share` with a low `time_share` and no tag-to-retire evidence is frequency, not an impact finding |

**Limits and caching.** Each external call has a five-minute timeout.

## SQL schema — `tools/views.sql`

One schema file; every object is TEMP because `cpu.db` is opened read-only. `uprof.py` binds twenty-two inputs as prepared parameters (`SET VARIABLE name = ?`, never text splicing), loads `views.sql` once, then runs one projection. The table below is the full bound set: the twenty-one `uprof_*` keys of `uprof_query._variables` plus `uprof_ibs_parquet`, which the profile call binds after locating the dump's Parquet copy; `views.sql`'s header lists nineteen of the twenty-two.

| Bound input | Meaning |
|---|---|
| `uprof_image` | ILIKE pattern on the process image's file name or full path (`Process.name`); `'%'` selects every process |
| `uprof_pid` | one PID as text; empty selects every process |
| `uprof_top` | row cap bound from `--top`: a positive `N`, or `0` for `--top all` (no cap) |
| `uprof_time_start`, `uprof_time_end` | window in ns; `[start, end)` |
| `uprof_ibs_dump` | absolute dump path; empty for PMC |
| `uprof_ibs_parquet` | absolute path of the cached Parquet copy of the dump's sample columns; empty when absent, so the views read the ASCII dump |
| `uprof_event_column` | PMC sample column in `UnifiedSampleSeries`; empty for a dump capture |
| `uprof_session_meta` | `session.uprof` path when it exists, else empty |
| `uprof_tag_available` | `true` only for an IBS OP dump whose `IbsTagToRetCtr` exists and has a nonzero value, so a fetch dump cannot enable `tag_to_retire_share` even with the column; Python derives it from the sampler kind and `v_dump_sanity` and rebinds it before the projection |
| `uprof_symbols` | combined `symbols.parquet`; empty when absent |
| `uprof_aliases` | combined `symbols.publics.csv`; empty when absent |
| `uprof_pdbs` | combined `symbols.pdbs.csv`: the PDB symbolization chose per module; empty when absent |
| `uprof_functions` | derived function ranges CSV (`module_name,function_start,function_end,function_source,function_name`); empty when no module in scope needs derivation |
| `uprof_range_attempted` | chr(2)-joined modules for which derivation ran; a fully named module is absent, so `function_reason` says the derivation was skipped rather than failed |
| `uprof_ambiguous_modules` | chr(2)-joined module names the capture maps to more than one image version; their dump samples get no `Function.id` |
| `uprof_primary_measure` | `sample_share`, `time_share` or `samples`: orders the row views and their `--top` |
| `uprof_detail_views` | `'true'` binds the full `v_line_rows` and `v_instruction_rows` bodies (line and instruction scopes); `'false'` binds them as an empty typed relation, so every other mode skips that bind cost |
| `uprof_sample_interval_s` | requested cycle interval in seconds, bound for `--by module|function|region`; empty when the interval is a work/fetch count or the core frequency is unavailable, which hides `cpu_seconds` |
| `uprof_function` | parent function name selected by `--function` for a line or instruction view; empty otherwise, which leaves `v_function_scope` empty |
| `uprof_function_module` | module name of that parent, from `--module`; matched exactly alongside the name |
| `uprof_function_start` | optional RVA from `--function-start` as decimal text, disambiguating same-named functions; empty matches the name and module only |

| Macro | Definition |
|---|---|
| `module_key(p)` | `replace(lower(p), chr(92), '/')` — the module-path match key |
| `module_ambiguous(name)` | whether `uprof_ambiguous_modules` lists `name` |
| `share(part, whole)` | `part / NULLIF(whole, 0)` — a fraction 0.0-1.0, the unit stated once in the output |

| Mode flag / guard | Purpose |
|---|---|
| `has_dump` | `uprof_ibs_dump` is non-empty |
| `schema_check` | fails once, naming every missing table/column and a bad `event#` column |
| `schema_check_pid` | PMC only: requires `UnifiedSampleSeries.processId` and `coreId`; dump captures do not |
| `dump_guard` | the dump exists and `time_start < time_end` |
| `dump_columns_guard` | one `DESCRIBE`; names missing required dump columns |
| `has_tag` | the dump header carries `IbsTagToRetCtr`; `v_dump_sanity.tag_values` counts non-null values |
| `has_system_module` | `Module` has an `isSystemModule` column; older exports leave the module classification NULL |
| `top_n`, `time_start`, `time_end` | typed row cap and window |

The views and the materialized `v_dump` TEMP TABLE:

| View | Role |
|---|---|
| `v_dump_source` | the dump's raw text columns: the cached Parquet copy (`uprof_ibs_parquet`) when bound and validated, else `read_csv` with `sample_size = 1`; an empty typed relation for PMC |
| `v_dump` | `CREATE OR REPLACE TEMP TABLE`: typed dump rows (`delta_tick`, `cpu`, `tid`, `pid`, `rva`, `module_name`, `tag_cycles`); tagged/untagged branch; materialized once per run and scanned by every downstream view; an empty typed relation for PMC |
| `v_dump_context` | `v_dump` plus the weighting context: `context_pid` 0 and `context` = `cpu` for a system-wide capture, `context_pid` = `pid` and `context` = `tid` for an attached one; defined once so weighting and the sanity check cannot disagree |
| `v_dump_weighted` | applies `[time_start, time_end)` then adds `weight = delta_tick - lag(delta_tick) OVER (PARTITION BY context_pid, context ORDER BY delta_tick)` from `v_dump_context`; the helper columns are excluded |
| `v_capture_span` | the span recorded in `session.uprof`: the translate `--time-filter` window when recorded, else `collection.schedule.elapsed`, else `profileDuration`; `start_ns`/`end_ns`/`expected_ns`/`span_label`/`system_wide`, shared by the dump-wide and scoped sanity views |
| `v_dump_sanity` | whole-dump provenance check: samples outside the recorded span bounds, or core time above span × CPUs when system-wide, error; attached captures report `OK, attached` with processes, TIDs, active TID and CPUs visited, and error only for samples outside the span; no usable duration or uncovered late time warns dump-wide; exposes `tag_values` for the empty-tag disclosure |
| `v_symbols` | combined symbolizer artifact: `module_name, rva, function, function_short, containing_function, file, line` |
| `v_public_starts` | public-symbol fold cache: `module_name, rva, name_count, names` |
| `v_function_ranges` | derived code ranges from `uprof_functions`, typed to `module_name, function_start, function_end, function_source, function_name`; empty when no derivation ran |
| `v_pdbs` | PDB chosen by symbolization per module (`module_name`, `pdb`); empty when absent, so the instruction context reuses it instead of discovering a PDB again |
| `v_scoped` | the one scope: dump branch weights `v_dump_weighted` and maps RVA to `Function.id`; PMC branch counts the event column and leaves `weight` NULL; `--pid`/`--image` apply after weighting |
| `v_scope_sanity` | scoped late-coverage verdict appended to `SANITY` when `--image`/`--pid`/`--time-filter` is active: last sample of `v_scoped` against the scope window end, `WARN` when the gap exceeds the scope's own sampling cadence |
| `v_scope_totals` | `total_samples` (0 when empty); feeds the empty-scope warning |
| `v_scope_measures` | the whole scoped capture's `total_samples`/`total_weight`/`total_tag_cycles`; line and instruction rows use it as their denominator so a `--function` filter cannot silently shrink the capture shares |
| `v_module_catalog` | one row per module path with its highest `ModuleInstance.loadAddress` and kernel-image flag; empty when the export has no `ModuleInstance` |
| `v_module_flags_exact` | `Module.isSystemModule` keyed by the full normalized path; empty when the export has no such column |
| `v_module_flags_basename` | `Module.isSystemModule` keyed by basename with ambiguous basenames resolved to NULL; empty when the export has no such column |
| `v_capture_modules` | one row per whole-capture sample: module, context (CPU for a dump, `coreId` for PMC), sample count and kernel-image flag; before scope and time filters; feeds `ACHIEVED` and `KERNEL_COVERAGE` from one scan |
| `v_process_rows` | aggregation, ranking, totals and the `<other processes>` remainder |
| `v_module_rows` | module path aggregation, ranking, totals, `isSystemModule`, the module's `hot_rva`/`hot_samples` and the `<other modules>` remainder |
| `v_function_measured` | the uncapped function aggregation behind `v_function_rows`: derived range when `uprof_functions` covers the RVA, else `Function.id`; `function_start`/`function_end`/`function_source`/`function_derived`; `<unresolved>` row; attribution `unique`/`shared` (range start with folded public names)/`derived`/`unresolved`; hot RVA by samples; total samples/weight/tag; `--function` reads its parent row here so `--top` cannot hide it |
| `v_function_rows` | `v_function_measured` with `--top` applied and one `<other functions>` remainder |
| `v_function_scope` | the single parent row selected by `uprof_function`/`uprof_function_module`/`uprof_function_start`; empty unless a function-scoped line or instruction view was requested |
| `v_region_rows` | one row per module and 4 KiB address page; `page_start`/`page_end`, `sampled_addresses`, hot RVA and the function that owns it; attribution `unique`/`shared`/`derived`/`unresolved`; `<other regions>` |
| `v_line_rows` | symbolizer join, short-name/overload identity, `<no symbol>`/`<no source line>`/`<multiple>` buckets, fold labels; filtered to `v_function_scope` when `--function` is set; `<other lines>`; full body only when `uprof_detail_views` is `'true'`, else an empty typed relation |
| `v_instruction_rows` | one row per module + RVA with innermost annotation, ordered by the mode's primary measure; filtered to `v_function_scope` when `--function` is set; `<other instructions>`; full body only when `uprof_detail_views` is `'true'`, else an empty typed relation |

- **Projection files** are one final SELECT over the views: `profile-process.sql`, `profile-module.sql`, `profile-function.sql`, `profile-region.sql`, `profile-line.sql`, `profile-instruction.sql`; they apply the `share()` macro and hide internal columns. `profile-rvas.sql` exports the symbolization scope, `profile-function-modules.sql` names the modules whose samples need derived ranges, `agreement.sql` computes the metric, `coverage.sql` reports per-module symbol coverage, and `schema.sql` lists the live schema. There are no optional schema files: `views.sql` is the only schema.
- **Load order.** Bind all inputs, load `views.sql` (which materializes `v_dump` once), then (for `function`/`region`/`line`/`instruction`) run `profile-rvas.sql` on the same connection, symbolize, rebind `uprof_symbols`/`uprof_aliases`; for `function`/`region`, and for `line`/`instruction` with `--function`, run `profile-function-modules.sql`, derive ranges from the binary with LLVM and rebind `uprof_functions` (the filter may name a derived range); then run the projection. `query(CASE ...)` re-binds per query, so the symbol and range inputs may be bound after the views are created. A hand run writes a UTF-8 `SET VARIABLE` file, `.read`s `views.sql`, then the projection, and always uses the original session's `cpu.db`.
- **Guard order.** The schema and dump guards run while `views.sql` loads, before any query, so an incomplete export or dump fails once with the missing item names. The schema guards read the bound path strings (empty means absent) so no identifier is spliced; the dump guard checks that the dump exists. A conditional `query(CASE ...)` avoids binder-eager branch evaluation.

## Reproducibility and tie order

The same capture and arguments produce byte-identical output on every run. Each row view ranks by weight, then samples, then a stable key; the remainder row is last:

| `--by` | ORDER BY |
|---|---|
| `process` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, pid, process` |
| `module` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, module` |
| `function` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, function_name, module_name` |
| `region` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, page_start, module` |
| `line` | `weight DESC NULLS LAST, samples DESC, file, line, function, module` |
| `instruction` | `CASE uprof_primary_measure WHEN 'time_share' THEN weight END DESC NULLS LAST, samples DESC, module, rva NULLS LAST, function` |

The JSON `legend.impact` (`order_by`, `gap_weighted`, `estimate`, `primary_measure`, `primary_measure_reason`) and the `READ` line state this ordering.

**Display escaping.** Table cells and header values escape controls as described in `SKILL.md`, Output; JSON carries the true characters, so table/JSON parity is modulo that display escaping. `--out-file` receives the same bytes as stdout.

## Known limitations

- **Inline overloads.** The display rule is in `SKILL.md`, Frame rule and sentinels; a true overload set (two or more out-of-line signatures) shows `<multiple>` on a shared source line.
- **Empty or junk tag column.** `has_tag` tests column presence; `uprof_tag_available` requires an IBS OP dump and at least one nonzero value. An all-NULL or junk-only `IbsTagToRetCtr` hides `tag_to_retire_share` and prints the empty-column disclosure; only an unsigned decimal integer cell counts, so a negative, fractional or exponent cell is NULL, never the clipped maximum. An all-zero column is hidden the same way.
- **Module paths are lowercased.** `module_key` lowercases and replaces `\` with `/`; the module column shows that normalized spelling, and two paths differing only in case or separator collide.
- **`--json --out-file` writes no file when the query fails** (including the dump sanity error);
  the reason is on stderr and the exit code is 2, so check the exit code before reading the file.

## Troubleshooting

The failure table in `SKILL.md` covers dump sanity, missing `session.uprof`/`cpu.db`, PDB misses, empty scope, and output/filesystem errors. This table adds the remaining remedies:

| Symptom | Cause | Command or remedy |
|---|---|---|
| `MISSING ...` from `tools` | uProf CLI, DuckDB 2.x or llvm-symbolizer not found | install or set `AMDUPROF_CLI` / `LLVM_SYMBOLIZER` (an elevated `collect` ignores `AMDUPROF_CLI` and `PATH`: install uProf to its default location); `python -m pip install duckdb` |
| `uProf export incomplete: missing ...` | uProf export differs from the expected schema | run `schema --session <dir>` and compare; use a matching uProf version |
| `error: argument --image: not allowed with argument --pid` | both scope flags given | pass one |
| capture directory locked during `translate` | open in the GUI | close it and retry |
| `translate failed ...: the session has no translatable sample category` | the session holds no samples; AMDuProfCLI crashes (0xC0000005) instead of failing cleanly | run one of the two `collect` commands the error prints, then translate that session |
| `translate failed ... (AMDuProfCLI crashed: access violation (0xC0000005))` without a named cause | AMDuProfCLI crashed for a reason the skill does not recognise | its last lines are in the AMDuProfCLI log the error names |
| `--out-dir already holds a previous translation` | a reused output directory still has files translate copied or generated | pass `--force` to replace the generated files and copied inputs, or choose another `--out-dir` |
| `SAMPLER: falling back ...` | VBS or hypervisor blocks IBS counters | accept the cycle capture, or ask the user whether to turn off VBS/Hyper-V, a security change they make themselves |
| `WARNING: running elevated; only collect needs elevation ...` | `translate` or `profile` runs with administrator or root rights | run them from a normal shell; only `collect` needs elevation |
