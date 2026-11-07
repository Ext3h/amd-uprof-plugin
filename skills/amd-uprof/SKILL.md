---
name: amd-uprof
description: Profile CPU time on AMD Zen with AMD uProf cycle sampling (PMCx076 / hwId 118, CYCLES_NOT_IN_HALT), IBS OP or IBS fetch dumps; exact DeltaTick-gap time weighting from the IBS ASCII dump; llvm-symbolizer file/line and instruction attribution; per-process, function, source-line and instruction shares in DuckDB SQL. Windows and Linux. Use for cycle-based hot spots, spin versus compute, instruction mix, and processes that cannot be attached.
---

# AMD uProf profile

One entry point, run from any directory:

```text
python <SKILL_DIR>/tools/uprof.py <command> ...
```

`<SKILL_DIR>` is the directory containing this file. `tools/views.sql` is the only schema; the SQL files stay usable directly (see `references/reference.md`).

## Commands

| Command | What it does |
|---|---|
| `collect` | capture PMCx076 cycle or IBS OP samples, system-wide or attached to PIDs; never IBS fetch |
| `translate` | copy the session, resolve symbols, optionally write the IBS ASCII dump |
| `profile` | samples and time shares by process, function, source line or instruction; prints SANITY and ACHIEVED |
| `anchor` | map a workload's QPC reading onto capture-relative seconds (negative = attach latency) |
| `schema` | live table/column/type listing for drift checking |
| `tools` | print every resolved tool path and version; fails only when a required tool is missing |

Exit codes: 0 success, 2 `error: ...` (argparse also exits 2), 130 interrupted.

## Capture — collect

```text
python <SKILL_DIR>/tools/uprof.py collect --out-dir <OUT_DIR> --seconds 60
python <SKILL_DIR>/tools/uprof.py collect --out-dir <OUT_DIR> --seconds 60 --process <PID>[,<PID>] --sample-interval 200us
python <SKILL_DIR>/tools/uprof.py collect --out-dir <OUT_DIR> --seconds 60 --event ibs-op --ibs-count-control op --sample-interval 1000000
```

| Argument | Meaning |
|---|---|
| `--out-dir` | required; a path that does not exist becomes the session; an existing path warns and gets an `AMDuProf-*` child |
| `--seconds` | capture duration; default 60; must be positive |
| `--process PID[,PID]` | attach to existing positive PIDs; absent = `-a` system-wide |
| `--sample-interval TIME\|OPS` | cycle modes: a time with a unit `ns`, `us`, `ms` or `s`, decimals and case allowed, default `1ms`. IBS op-count mode: a positive integer work count (dispatched ops), default `1000000`; a time string is rejected |
| `--event cycles\|ibs-op` | default `cycles` = PMCx076 `CYCLES_NOT_IN_HALT`, hwId 118, `user=1,os=1`; IBS fetch is not a capture choice |
| `--ibs-count-control op\|cycles` | only with `--event ibs-op`; default `op` (op-count interval); `cycles` takes a time `--sample-interval`, converts it like PMC, and defaults to `1ms` |
| `--dry-run` | print the interval line and exact CLI command, then exit before capturing; does not need the CLI installed |

- `collect` prints `SAMPLE_INTERVAL=`, `EVENT=`, `COMMAND=`, `Elevated=`, `SESSION=`, `collectCommand=` and the metadata summary, and verifies the applied event, converted interval, sampling, IBS-vs-PMC and system-wide-vs-attached mode; a mismatch fails. Cycle modes convert `round(seconds × frequency)` and print `SAMPLE_INTERVAL=1ms -> <cycles> cycles at <MHz> MHz (source: ...)` before `EVENT=`. The frequency is the reported core frequency: Windows registry `HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0\~MHz`; Linux cpufreq `base_frequency`, else `/proc/cpuinfo cpu MHz`; a machine with no usable frequency errors with a remedy.
- An unelevated capture prints `WARNING: unelevated: elevated processes ... may be missing`; services and admin-started engines may be absent, so capture elevated and confirm the process appears with `--by process`. Absence is not proof of idleness.
- Only an IBS OP capture that fails because AMDuProfCLI reports IBS counters unavailable falls back: it prints `SAMPLER: falling back to cycle sampling (time-based); IBS unavailable: <reason>`, then runs a cycle capture (reprinting the 1 ms conversion when the request was op-count; a cycle-controlled request keeps its converted interval). Any other IBS failure exits 2. On Windows the reason names `Win32_DeviceGuard` (`VirtualizationBasedSecurityStatus` 2 while VBS runs); on Linux it reports the `/proc/cpuinfo` hypervisor flag or loaded KVM modules.
- A raw `AMDuProfCLI collect --config ibs` capture (not a `uprof.py collect` flag) records no `event=` in `collectCommand`; read the sampler from `collection.events.ibsEvents`.

## Translate — translate

```text
python <SKILL_DIR>/tools/uprof.py translate --session <SESSION_DIR> --out-dir <COPY_DIR> \
    [--pdb-dir <dir>] [--symbol-cache <dir>] [--symbol-server <srv>] \
    [--time-filter T1:T2] [--ascii ibsop-event-dump|ibsfetch-event-dump]
```

Copies `session.uprof` plus `cpu/`, `metadata/`, `.config/`, `.log/`, `sources/` and `binaries/` to `--out-dir`, translates the copy, and prints `SESSION=` / `CPU_DB=` (`ASCII_DUMP=` when `--ascii` ran). The source is never written. An existing non-directory `--out-dir`, `--out-dir == --session`, a malformed `--time-filter` or a copy failure (permission, read-only parent, collision) exits 2 with `error: ...` naming the offending path or form and a remedy, never a traceback. Close a capture open in the GUI first: it is locked.

- Symbol cache: `--symbol-cache`, else the first `srv*<cache>*<url>` cache in `_NT_SYMBOL_PATH`, else `%LOCALAPPDATA%\Symbols` (Windows) or `$XDG_CACHE_HOME/amd-uprof/symbols` / `~/.cache/amd-uprof/symbols` (Linux). Symbol servers: `_NT_SYMBOL_PATH` entries, else Microsoft's on Windows, none on Linux; `--symbol-server` appends.
- `--pdb-dir` passes `--symbol-path`. `--ascii ibsop-event-dump` writes `IbsOpDump.csv`; `ibsfetch-event-dump` writes `IbsFetchDump.csv` for a fetch dump produced elsewhere, which profiles the same way but is unverified. `profile` generates a missing dump itself on a cached copy, so translating it yourself is optional.

## Profile

```text
python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by <granularity> [options]
```

| Argument | Meaning |
|---|---|
| `--session` | required capture directory holding `session.uprof` and `cpu.db` |
| `--by process\|function\|line\|instruction` | required grouping key |
| `--pid N` | one process by PID (non-negative); mutually exclusive with `--image` |
| `--image PATTERN` | case-insensitive ILIKE on the full image path; all PIDs of matching images; `'%'` = every process |
| `--time-filter T1:T2` | seconds from capture start, `[T1,T2)`; default the whole capture; dump captures only (rejected on PMC) |
| `--top N\|all` | default 20; `all` = no cap; returns N rows plus one `<other ...>` remainder row when truncated |
| `--json` | emit the header fields and rows as one JSON document |
| `--out-file PATH` | write exactly the bytes stdout would carry; the directory must exist |

| `--by` | Row key | Columns | Symbolization |
|---|---|---|---|
| `process` | process | process, pid, samples, pct_samples, pct_time*, pct_cycles* | no |
| `function` | Function.id | function, module, attribution, samples, pct_samples, pct_time*, pct_cycles* | no |
| `line` | file:line | file, line, function, module, attribution, samples, pct_samples, pct_time*, pct_cycles* | every module in scope |
| `instruction` | module + RVA | module, rva, function, file, line, attribution, samples, pct_samples, pct_time*, pct_cycles* | every module in scope |

`*` is present only when the capture mode supplies it (next table).

## What a share measures

| Capture or dump | Status | A sample is | `pct_samples` is | `pct_time` | `pct_cycles` |
|---|---|---|---|---|---|
| PMCx076 `CYCLES_NOT_IN_HALT` | verified | fixed cycles | time share (busy CPU time) | absent: no per-sample timestamps | absent |
| IBS OP, tagged, op-count interval (default) | verified | fixed retired ops | instruction share — **not time** | exact DeltaTick-gap time | present: clipped at 131070, never time |
| IBS OP, tagged, cycle-controlled | no capture available | fixed cycles | time share | exact DeltaTick-gap time | present: clipped at 131070, never time |
| IBS OP, tagless (either control) | op-count verified; cycle-controlled unverified | as the control says | instruction share (op-count) or time share (cycle-controlled) | exact DeltaTick-gap time | absent: the dump has no `IbsTagToRetCtr` |
| IBS fetch dump | unverified (dump only) | fetch events | fetch-sample share — not time | exact DeltaTick-gap time | absent: an IBS fetch dump has no tag-to-retire basis |

"Tagged" means the dump carries `IbsTagToRetCtr`, detected per dump rather than from the capture metadata. "Verified" means exercised on a capture; "no capture available" means the code path exists but no capture was made. A PMC capture never falls back to a dump or a fabricated time share. The row views emit `samples`, then `pct_samples`, then `pct_time` and `pct_cycles` only when the capture supplies them. For an IBS OP dump, a present column with no non-null value (including a junk-only column) omits `pct_cycles` like a tagless dump, with its own disclosure: `PCT_CYCLES` says `IbsTagToRetCtr has no non-null values`.

- The `METRIC:` line states the captured mode and what a sample represents; read it before quoting a share. An instruction share is never presented as time.
- `pct_time` is exact DeltaTick-gap weighting from the IBS ASCII dump: a gap is charged whole to the later sample, so idle/halted time accrues to the next sample; totals are core time summed over CPUs. The per-CPU lag and the rule that `--time-filter` applies before it are in `references/reference.md`.
- `pct_cycles` is the share of sampled op latency (tag-to-retire), clipped at 131070; it is never elapsed time. A NULL or non-numeric cell is excluded from the denominator, not charged the maximum.
- `pct_samples` for a dump is the raw dump-row share; for PMC it is the `UnifiedSampleSeries` event-column count share (one cpu.db row may carry several samples). Each emitted share column sums to 100 within floating-point precision when it has at least one non-null value; a single-sample window can leave `pct_time` `NULL`.

## Header lines

| Field | Meaning |
|---|---|
| `METRIC` | sampler, event column, interval/control and what a sample represents |
| `DUMP` | dump path, size and provenance (`found beside the session`, cache reused/generated, or `none` for PMC) |
| `DB` | only when the dump is outside the session: which dump is valid, and that `cpu.db` must come from the original session |
| `PCT_TIME` | estimator and the window it is relative to: the whole capture or the `profile --time-filter` |
| `PCT_CYCLES` | tag-to-retire basis and the 131070 clip, the missing-column or empty-column disclosure, or `not applicable` for PMC/fetch |
| `WEIGHTS` | the aggregation key of the chosen `--by` and the weight basis |
| `SANITY` | whole-dump provenance check: dump core time against the `session.uprof` span × dump CPUs, 2% tolerance; a `WARN` means no usable duration |
| `AGREEMENT` | `line`/`instruction` only: frame rule, sample-weighted mismatch rate, address counts (the `compared` predicate is both names non-empty), `<no symbol>` sample weight |

`SANITY` validates the entire dump against the **translate** span (the `translate --time-filter` window when recorded, else `collection.schedule.elapsed`); `PCT_TIME` and `WEIGHTS` describe the **profile** `--time-filter` window. The two windows may legitimately differ.

## Frame rule and sentinels

- `line` and `instruction` take `file`, `line` and `function` from the innermost executed frame (llvm-symbolizer `Symbol[0]`); `instruction` is one row per module + RVA.
- `function` is uProf's `Function.id`, the function containing an inlined instance. `AGREEMENT` compares the containing function (`Symbol[last]`), so an inline instance is never a name mismatch.
- **Inline-overload limitation:** an inlined frame carries no signature. It keys on the short name, so when only one out-of-line signature exists for that name it is displayed under that signature even if it belongs to a different same-named overload.
- Sentinels: `<no symbol>` (no function name; groups per module), `<no source line>` (function but no file/line; groups per module + function), `<multiple>` (a line group whose function or module annotations disagree). A frame with a file/line but no function stays under `<no symbol>`, and its weight is disclosed in `AGREEMENT`.
- `attribution` per row: `unique`, `shared` (folded address) or `unresolved`; function rows are only `unique` or `unresolved`; remainder rows carry `NULL`.

## Shared addresses

`/OPT:ICF` folds identical bodies, so one RVA can carry several public names. When `llvm-pdbutil` is available on Windows, symbolization builds a per-module public-symbol cache and labels each folded start `shared: nameA | nameB` with `attribution = shared`, at `line` and `instruction` granularity only. Report that share as shared, not under one name. Without `llvm-pdbutil` the cache is absent (`FOLDS: skipped`) and every resolved row is `unique`; unresolved rows still exist. Resolved Linux DWARF symbols are all `unique` (unverified here).

## Failure behaviour

| Condition | Behaviour | Exit |
|---|---|---|
| dump total core time more than 2% from the `session.uprof` span × dump CPUs | error naming the dump and the arithmetic, with the remedy: delete the dump or its cache directory and re-run `profile` so it is regenerated on a copy | 2 |
| `session.uprof` missing | error before any query; `pct_time` needs it for the sampler and the span check | 2 |
| `cpu.db` missing | error naming the file and the `translate --session ... --out-dir ...` step | 2 |
| translate `--out-dir` is an existing non-directory or equals `--session`; a copy fails (permission, read-only parent); a profile dump cache or symbol-artifact temp path cannot be created or written | `error:` with the offending path or rule and a permissions/free-space remedy; no traceback | 2 |
| no matching PDB, missing binary, no image GUID, or `llvm-readobj` missing/failed | warning; addresses become `<no symbol>`; `AGREEMENT` reports the weight | 0 |
| required tool missing (uProf CLI, DuckDB 2.x, llvm-symbolizer) | `tools` prints paths and versions and `error: MISSING ...`; a symbolized profile fails at symbolization | 2 |
| `--sample-interval` is a time in IBS op-count mode, a bare integer in a cycle mode, or a non-positive work count | error naming the required form and the mode | 2 |
| `--time-filter` on a PMC capture | error: PMC samples carry no per-sample timestamps | 2 |
| IBS OP capture where AMDuProfCLI reports IBS counters unavailable | explicit one-line `SAMPLER: falling back to cycle sampling ...`, then a cycle capture; any other IBS failure is an error | 0 on the fallback, 2 otherwise |
| capture export missing tables/columns or a bad event column | schema guard names every missing item and exits before querying | 2 |
| `--out-file` equals `cpu.db`, `session.uprof` or the dump; directory missing; path is a directory | error; the input is never overwritten | 2 |
| empty scope | `WARNING: no samples in the selected scope` plus an empty table | 0 |

## Tools

Run `uprof.py tools` first; install only when a required path prints `MISSING`. Each row is `<NAME>=<path>; version=<version>`; optional absent tools print `MISSING (not found)`, and a uProf or LLVM version that cannot be read prints `unavailable` or the install-path version.

| Tool | Windows | Linux |
|---|---|---|
| uProf CLI | `AMDUPROF_CLI`, `PATH`, then `C:\Program Files\AMD\AMDuProf\bin\AMDuProfCLI.exe` | `AMDUPROF_CLI`, `PATH`, then `/opt/AMDuProf_*/bin`, `/opt/AMD/uProf*/bin`, `/usr/local/bin` |
| DuckDB | Python package `duckdb` 2.x | the same |
| llvm-symbolizer | `LLVM_SYMBOLIZER`, `PATH`, then `C:\Program Files\LLVM\bin` | `LLVM_SYMBOLIZER`, `PATH`, then `/usr/lib/llvm-*/bin`, `/usr/bin` |
| llvm-pdbutil | beside `llvm-symbolizer` or on `PATH`; fold cache (Windows only) | not used |
| llvm-readobj | beside `llvm-symbolizer` or on `PATH`; image GUID (Windows only) | not used |

## Output

- Default: a Markdown pipe table; numeric columns are right-aligned (`---:`), text columns `---`. Header fields are plain lines above it.
- `--json`: one object with the same header fields in order, then `"rows"`, an array keyed by the table's column names. Values are identical modulo display escaping; only Markdown escaping differs.
- Cells: `NULL` (JSON `null`); `&`, `<`, `>` entity-escaped; backslashes doubled; `|` escaped. Full precision, no rounding. CR, LF, TAB, NUL, other C0 controls and DEL are shown as `\n`, `\r`, `\t` or two-digit uppercase `\xNN` in table cells and header values (a header value containing controls also doubles backslashes); JSON keeps the true characters.
- `--out-file` writes UTF-8 without a BOM and LF newlines, exactly the stdout bytes, through `<path>.tmp` and rename; stdout stays empty. `schema` keeps `--out-file` but has no `--json`: `--json` is scoped to `profile`, so the asymmetry is intentional.
- The same capture and arguments produce byte-identical output; rows rank by weight, then samples, and the per-granularity tie order is in `references/reference.md`. The denominator is the whole scoped capture, so unresolved addresses keep their sample weight.

## Pitfalls

- The capture is DuckDB; query through `uprof.py` or `tools/views.sql`, never a hand-rolled parser. Run `schema` only when the capture may come from a different uProf version or the guard reports a mismatch.
- A system-wide capture ranks whatever is busy, often the shell or agent that ran it; use `--process` for one program and omit it for whole-machine questions.
- The sample count is far below `duration × cores × clock / interval`: halted and idle cores emit no samples, so do not size a capture from that product.
- `AMDuProfCLI --help` can block on "Press any key" on Windows; read the installed `Help/text/*.txt` instead.
- `Srcfile` is empty; AMD's `report --detail --disasm` is a report, not a joinable table.
- On Linux, `_NT_SYMBOL_PATH`, `llvm-pdbutil` and `llvm-readobj` are not part of the flow: symbolization reads DWARF from the binary. The Linux branch is kept but unverified on the reference host.

## References

`references/reference.md`: metadata keys, estimator derivation, dump cache, symbolizer invocations and artifact schemas, the 11 SQL views, the `pct_cycles` formula, known limitations, tie order and troubleshooting. `references/validation.md`: measured acceptance figures (do not restate them here).
