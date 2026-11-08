---
name: amd-uprof
description: Profile CPU time on AMD Zen with AMD uProf cycle sampling (PMCx076 / hwId 118, CYCLES_NOT_IN_HALT), IBS OP or IBS fetch dumps; exact DeltaTick-gap time weighting from the IBS ASCII dump; llvm-symbolizer file/line and instruction attribution; per-process, module, function, source-line and instruction shares in DuckDB SQL. Windows and Linux. Use for cycle-based hot spots, spin versus compute, instruction mix, and processes that cannot be attached.
---

# AMD uProf profile

One entry point, run from any directory:

```text
python <SKILL_DIR>/tools/uprof.py <command> ...
```

`<SKILL_DIR>` is the directory containing this file. `tools/views.sql` is the only schema; the SQL files stay usable directly (see `references/reference.md`).

## Quickstart

With an existing capture, run `profile --by process` first: it already prints `METRIC`, `SANITY`, `SYMBOL_COVERAGE` and `KERNEL_COVERAGE`, so no separate discovery pass is needed. For the CPU-time question the minimum path is `profile --by process`, then `profile --by module`, then `profile --by instruction --json` (top 20 is the default); do not follow every `NEXT` step. `--by module` is the rollup: it carries `Module.isSystemModule`, `tag_to_retire_share` and `cpu_seconds` directly, so no ad-hoc `cpu.db` SQL is needed. The worked example below turns that into the module answer and the stop point.

## Commands

| Command | What it does |
|---|---|
| `collect` | capture PMCx076 cycle or IBS OP samples, system-wide or attached to PIDs; never IBS fetch |
| `translate` | copy the session, resolve symbols, optionally write the IBS ASCII dump |
| `profile` | samples and time shares by process, module, function, source line or instruction; prints SANITY and ACHIEVED |
| `anchor` | map a workload's QPC reading onto capture-relative seconds (negative = attach latency; `--freq` defaults to 10 MHz) |
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
| `--event cycles\|ibs-op` | default `cycles` = PMCx076 `CYCLES_NOT_IN_HALT`, hwId 118, `user=1,os=1`; `ibs-op` samples user and OS by default. Both request OS mode, which is applied and encoded, but kernel-mode samples require an elevated capture; IBS fetch is not a capture choice |
| `--ibs-count-control op\|cycles` | only with `--event ibs-op`; default `op` (op-count interval); `cycles` takes a time `--sample-interval`, converts it like PMC, and defaults to `1ms` |
| `--dry-run` | print the interval line and exact CLI command, then exit before capturing; does not need the CLI installed |

- `collect` prints `SAMPLE_INTERVAL=`, `EVENT=`, `COMMAND=`, `Elevated=`, `SESSION=`, `collectCommand=` and the metadata summary, and verifies the applied event, converted interval, sampling, IBS-vs-PMC and system-wide-vs-attached mode; a mismatch fails. Cycle modes convert `round(seconds × frequency)` and print `SAMPLE_INTERVAL=1ms -> <cycles> cycles at <MHz> MHz (source: ...)` before `EVENT=`. The frequency is the reported core frequency: Windows registry `HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0\~MHz`; Linux cpufreq `base_frequency`, else `/proc/cpuinfo cpu MHz`; a machine with no usable frequency errors with a remedy.
- An unelevated capture prints `WARNING: unelevated: elevated processes ... may be missing`; services and admin-started engines may be absent, so capture elevated and confirm the process appears with `--by process`. Absence is not proof of idleness. A capture of either event appends `; kernel-mode (OS) samples are omitted without elevation`: the cycle spec passes `os=1`, and the IBS event samples user and OS by default. This is a hard limitation, not a sampling quirk: the OS-mode request is applied and encoded, but an unelevated capture contains no kernel-mode samples, so kernel coverage requires an elevated capture. `profile` prints `KERNEL_COVERAGE:` for the same condition when the whole capture has no kernel-image sample (see Header lines). Both are distinct from the elevated-processes warning: that one is about user processes started elevated, this one about kernel-mode (OS) samples.
- Only an IBS OP capture that fails because AMDuProfCLI reports IBS counters unavailable falls back: it prints `SAMPLER: falling back to cycle sampling (time-based); IBS unavailable: <reason>`, then runs a cycle capture (reprinting the 1 ms conversion when the request was op-count; a cycle-controlled request keeps its converted interval). Any other IBS failure exits 2. On Windows the reason names `Win32_DeviceGuard` (`VirtualizationBasedSecurityStatus` 2 while VBS runs); on Linux it reports the `/proc/cpuinfo` hypervisor flag or loaded KVM modules.
- A raw `AMDuProfCLI collect --config ibs` capture (not a `uprof.py collect` flag) records no `event=` in `collectCommand`; read the sampler from `collection.events.ibsEvents`.

## Translate — translate

```text
python <SKILL_DIR>/tools/uprof.py translate --session <SESSION_DIR> --out-dir <COPY_DIR> \
    [--pdb-dir <dir>] [--symbol-cache <dir>] [--symbol-server <srv>] \
    [--time-filter T1:T2] [--ascii ibsop-event-dump|ibsfetch-event-dump] [--force]
```

Copies `session.uprof` plus `cpu/`, `metadata/`, `.config/`, `.log/`, `sources/` and `binaries/` to `--out-dir`, translates the copy, and prints `SESSION=` / `CPU_DB=` (`ASCII_DUMP=` when `--ascii` ran). The source is never written. An existing non-directory `--out-dir`, `--out-dir == --session`, a malformed `--time-filter` or a copy failure (permission, read-only parent, collision) exits 2 with `error: ...` naming the offending path or form and a remedy, never a traceback. An `--out-dir` that already holds `cpu.db`, `callstack.db` or an ASCII dump is refused with exit 2 because translate would otherwise append to the old database; `--force` removes those generated files and the copied session inputs first, so a repeated translate gives the same counts as a fresh one and unrelated files stay untouched. Close a capture open in the GUI first: it is locked.

- Symbol cache: `--symbol-cache`, else the first `srv*<cache>*<url>` cache in `_NT_SYMBOL_PATH`, else `%LOCALAPPDATA%\Symbols` (Windows) or `$XDG_CACHE_HOME/amd-uprof/symbols` / `~/.cache/amd-uprof/symbols` (Linux). Symbol servers: `_NT_SYMBOL_PATH` entries, else Microsoft's on Windows, none on Linux; `--symbol-server` appends.
- `--pdb-dir` passes `--symbol-path`. `--ascii ibsop-event-dump` writes `IbsOpDump.csv`; `ibsfetch-event-dump` writes `IbsFetchDump.csv` for a fetch dump produced elsewhere, which profiles the same way but is unverified. `profile` generates a missing dump itself on a cached copy, so translating it yourself is optional.

## Profile

```text
python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by <granularity> [options]
```

| Argument | Meaning |
|---|---|
| `--session` | required capture directory holding `session.uprof` and `cpu.db` |
| `--by process\|module\|function\|line\|instruction` | required grouping key |
| `--pid N` | one process by PID (non-negative); mutually exclusive with `--image` |
| `--image PATTERN` | case-insensitive ILIKE on the full image path; all PIDs of matching images; `'%'` = every process |
| `--time-filter T1:T2` | seconds from capture start, `[T1,T2)`; default the whole capture; dump captures only (rejected on PMC) |
| `--top N\|all` | default 20; `all` = no cap; returns N rows plus one `<other ...>` remainder row when truncated |
| `--json` | emit the header fields and rows as one JSON document |
| `--out-file PATH` | write exactly the bytes stdout would carry; the directory must exist |

| `--by` | Row key | Columns | Symbolization |
|---|---|---|---|
| `process` | process | process, pid, samples, sample_share, time_share*, tag_to_retire_share* | no |
| `module` | module path | module, samples, sample_share, time_share*, tag_to_retire_share*, isSystemModule, cpu_seconds* | no |
| `function` | Function.id | function, module, attribution, samples, sample_share, time_share*, tag_to_retire_share* | no |
| `line` | file:line | file, line, function, module, attribution, samples, sample_share, time_share*, tag_to_retire_share* | every module in scope |
| `instruction` | module + RVA | module, rva, function, file, line, attribution, samples, sample_share, time_share*, tag_to_retire_share* | every module in scope |

`*` is present only when the capture mode supplies it (next table). Every share is a fraction 0.0-1.0; `samples` stays a raw count. The module row's `isSystemModule` is `Module.isSystemModule` and is `NULL` when the export has no such column; `cpu_seconds` is seconds of sampled CPU time, not a share, present only when a sample is a fixed cycle count and the core frequency is available.

## What a share measures

| Capture or dump | Status | A sample is | `sample_share` is | `time_share` | `tag_to_retire_share` |
|---|---|---|---|---|---|
| PMC, supported cycle event (`CYCLES_NOT_IN_HALT`, hwId 118, sampling) | verified | fixed cycle count | busy core-cycle share (halted excluded) | absent: this export aggregates PMC samples per window | absent: this export has no tag-to-retire counter |
| PMC, any other event (not the supported cycle event, or not sampling) | not established | one event-column count | not established | absent: this export aggregates PMC samples per window | absent: this export has no tag-to-retire counter |
| IBS OP, tagged, op-count interval (default) | verified | selected op (op-count interval) | dispatched-op frequency share (fixed dispatched ops per sample), not a per-op cost | time share estimated from gaps | present: total tag-to-retire cycles summed over sampled ops (dispatch tag to retirement, queue/stalls included), a total, not per-op latency, 16-bit counter clipped at 65535 |
| IBS OP, tagged, cycle-controlled | verified on an external cycle-controlled capture | selected op (cycle-controlled interval) | core-cycle share (fixed cycles per sample), not wall time, not a per-op cost | time share estimated from gaps | present: total tag-to-retire cycles summed over sampled ops (dispatch tag to retirement, queue/stalls included), a total, not per-op latency, 16-bit counter clipped at 65535 |
| IBS OP, tagless (either control) | op-count verified; cycle-controlled external capture | selected op (per control) | dispatched-op frequency share or core-cycle share per control (fixed work per sample), not a per-op cost | time share estimated from gaps | absent: the dump has no `IbsTagToRetCtr` |
| IBS fetch dump | unverified (dump only) | selected fetch event | fetch-sample frequency share (fixed fetch events per sample) | time share estimated from gaps | absent: this export has no tag-to-retire counter |

"Tagged" means the dump carries `IbsTagToRetCtr`, detected per dump rather than from the capture metadata. "Verified" means exercised on a capture; the cycle-controlled row cites an external cycle-controlled capture. A PMC capture never falls back to a dump or a fabricated time share. The row views emit `samples`, then `sample_share`, then `time_share` and `tag_to_retire_share` only when the capture supplies them. For an IBS OP dump, a present column with no non-null value (including a junk-only column) omits `tag_to_retire_share` like a tagless dump, with its own disclosure: `TAG_TO_RETIRE_SHARE` says `IbsTagToRetCtr has no non-null values`.

- The `METRIC:` line states the captured mode and what a sample represents; read it before quoting a share. For op-count and fetch shares, `sample_share` counts selected operations or fetch events, so the cheap-often/expensive-rarely caveat applies; a cycle-controlled share counts fixed cycles per sample and does not carry that caveat.
- `time_share` is a time share estimated from the gaps in the IBS ASCII dump: a gap is charged whole to the later sample, so idle/wait time accrues to the sample that ends it; the first sample in each partition is ignored (no gap before it) and a `NULL` sorts last. A system-wide capture partitions by `CPU_Number`, so a process total is core time summed over CPUs and the share is the impact ordering. An attached capture partitions by `(PID, TID)`, so a thread that migrates between CPUs is counted once and its gaps include off-CPU time; for a cycle-controlled capture, `sample_share` (fixed cycles per sample) is the CPU-cycle share and the better ranking. Do not read either as unqualified impact. The partition, the lag and the rule that `--time-filter` applies before the lag are in `references/reference.md`.
- `tag_to_retire_share` is the share of AMD tag-to-retire cycles summed over sampled ops: the tag at dispatch to retirement, including scheduler/queue occupancy and stalls, clipped at 65535, the 16-bit field's maximum. It is a total, not per-op latency. Only an unsigned decimal integer cell is a value; NULL, negative, fractional and exponent cells are excluded from the denominator, not charged the maximum.
- `sample_share` for a dump is the raw dump-row share; for PMC it is the `UnifiedSampleSeries` event-column count share (one cpu.db row may carry several samples). For the supported cycle event, each sample is a fixed cycle count, so the share is the busy core-cycle share (halted excluded); any other PMC event leaves it not established. Every emitted share column sums to 1.0 within floating-point precision when it has at least one non-null value; a single-sample window can leave `time_share` `NULL`, and an all-zero `IbsTagToRetCtr` leaves every `tag_to_retire_share` `NULL` rather than summing to 1.0.

## Worked example: where did CPU time go?

This path is an **attached, cycle-controlled IBS** capture (`collect --event ibs-op --ibs-count-control cycles --process <PIDs>`). For op-count IBS, IBS fetch or a system-wide capture, use the mode matrix above instead of translating this recipe: the ranking column changes with what a sample represents.

1. **Confirm the mode and scope.** `METRIC:` names the sampler and what a sample is. `VERSION:` is authoritative: it names the producing plugin/skill copy and its version, so read the semantics in this document rather than re-deriving them. `SANITY:` starting `OK (attached; ...)` states thread coverage and names `sample_share` as the better ranking for a cycle-controlled capture; a `WARN` means the capture is short or late, not that the weights are wrong.
2. **Rank the processes.** Run `profile --session <SESSION_DIR> --by process` and take the largest `sample_share`. For an attached cycle-controlled IBS capture, rank by `sample_share`, not `time_share`: cycle-controlled samples carry a fixed cycle count, so `sample_share` is the CPU-cycle share, while `time_share` is elapsed thread time, including off-CPU gaps.
3. **Roll the processes up to modules.** Use the module granularity; do not query `cpu.db` or write ad-hoc SQL:

   ```text
   python <SKILL_DIR>/tools/uprof.py profile --session <SESSION_DIR> --by module
   ```

   Each row is one module path with `samples`, `sample_share`, `time_share`, `tag_to_retire_share`, `isSystemModule` and `cpu_seconds`. `sample_share` names the hottest module. **Third-party** is the rule `Module.isSystemModule = false`; `true` means an OS-shipped module. A driver flagged as a system module can still dominate, so report the strict third-party total and the dominant non-workload dependency separately rather than re-deriving the rule.
4. **Stop at the symbol boundary.** Read `SYMBOL_COVERAGE:` before drilling down. When a module has no named functions, `--by line` and `--by instruction` cannot attribute below module/RVA, and the line says so. Report the hot module (with its `isSystemModule` classification and `cpu_seconds`) and its hot RVA, then stop. Do **not** write a PE parser, map `.pdata` by hand, convert RVAs to VAs or disassemble to recover function boundaries. Symbolization is the supported path; for the instructions under a resolved hot RVA, `profile --by instruction --json` (top 20) adds the documented `context` block. A third-party module with no symbols is a finding, not a blocker.

**Deliverable and stop.** Stop when you have the module rollup (`isSystemModule`, `cpu_seconds`), `SYMBOL_COVERAGE` and the hot RVA; then write the report. `profile` only reads the capture, so there is nothing further to probe, install or verify, and following every `NEXT` step is not part of this question.

**Absolute CPU time.** `--by module` emits `cpu_seconds` = `samples × interval / core_frequency` for a cycle-mode capture; the interval is the requested cycle count converted at this machine's reported core frequency, which `ACHIEVED:` prints. The session records no frequency, so a capture from other hardware is approximate. This is sampled CPU time, not wall time: a system-wide capture sums cores and halted cycles are excluded, and an attached thread's off-CPU time is not sampled, so do not multiply by the CPU count. The shares answer "where did it go"; the seconds answer "how much".

## Header lines

| Field | Meaning |
|---|---|
| `VERSION` | the skill version from the nearest `.claude-plugin/plugin.json` and the skill directory that produced the output; a loose copy reports the built-in version. It is authoritative for which docs apply to the output |
| `METRIC` | sampler, event column, interval/control and what a sample represents |
| `COLUMNS` | the unit stated once (share columns are fractions 0.0-1.0), `samples` as raw count, each emitted share column by name, the `attribution` definition when the scope emits it, `isSystemModule` and `cpu_seconds` for `--by module`, and each absent column with its reason; definitions stay in the header lines |
| `READ` | which column ranks where time went — per thread for an attached capture, with `sample_share` named as the CPU-cycle share and the better ranking when it is cycle-controlled — that `sample_share` is not a per-op cost, that `tag_to_retire_share` is total tag-to-retire cycles (higher = more wait), and that the `<other ...>` row is last, not ranked |
| `NEXT` | full runnable calls joined by `&&`, each keeping `--session`, the `--pid`/`--image` and time-filter scope and a non-default `--top`, derived from the current `--by`; the last call adds `--json` to `--by instruction`. The CPU-time path is two calls: from `process` it is `module` then the instruction JSON call, and from `module` it is the instruction JSON call alone |
| `DUMP` | dump path, size and provenance (`found beside the session`, cache reused/generated, or `none` for PMC) |
| `DB` | only when the dump is outside the session: which dump is valid, and that `cpu.db` must come from the original session |
| `TIME_SHARE` | estimator, partition (CPU for system-wide, `(PID, TID)` for attached) and the window it is relative to: the whole capture or the `profile --time-filter`; few samples make the share coarse, so it says to check `sample_share`; for an attached cycle-controlled capture it names `sample_share` as the CPU-cycle share and the better ranking |
| `TAG_TO_RETIRE_SHARE` | tag-to-retire basis and the 65535 clip, that a higher share means more total cycles rather than a higher per-op cost, the missing-column or empty-column disclosure, or `not applicable` for PMC/fetch |
| `WEIGHTS` | the aggregation key of the chosen `--by` and the weight basis: exact DeltaTick gaps per CPU for a system-wide capture, per `(PID, TID)` for an attached one |
| `SANITY` | provenance check. Dump-wide across all processes. System-wide: covered core time against the recorded span (`translate --time-filter` window when recorded, else `collection.schedule.elapsed`) × dump CPUs, plus the boundary gaps; errors only when core time exceeds that capacity or a sample lies outside the span. Attached: gaps are partitioned by `(PID, TID)`, so the verdict is thread coverage rather than CPU capacity — never `elapsed × CPUs` — and the message reports the processes, TIDs, the busiest TID's samples and covered gap time, the CPUs visited and the thread-time total, saying `OK (attached; dump-wide)` and naming `sample_share` as the CPU-cycle share and the better ranking for a cycle-controlled capture. Errors only when a thread holds a sample outside the recorded span. A `WARN (dump-wide across all processes)` means no usable duration or uncovered late time. When `--image`/`--pid` or `--time-filter` narrows a system-wide report, a `scoped coverage (<scope>)` verdict follows: the scope's last sample against the end of the scope window (the time-filter window, with its end clipped to the recorded span), `WARN` when that gap exceeds the scope's own sampling cadence; attached captures (including a missing `systemWide`) keep only the attached verdict |
| `ACHIEVED` | whole-capture samples, window, CPUs and delivered samples/s per CPU against the requested interval, then the busiest sampling context's cadence and the number of active contexts (at least 10% of the busiest one's samples; a context is a CPU, or a `(PID, TID)` thread for an attached capture); the per-CPU figure divides by every CPU, so it is not the delivered cadence; warns only when the busiest context is below 10% of the requested rate, never because idle CPUs dilute the average; `translate --ascii` prints the same line counted from its dump |
| `KERNEL_COVERAGE` | only when the applied sampler requested OS-mode samples (PMC event `os: true`; an enabled IBS sampler's `opOs`/`fetchOs`) and the **whole capture, before `--image`/`--pid`/`--time-filter`**, has no sample in a kernel image; it reports the sampler's OS-mode bit and the absence of a kernel-image sample, names elevation only conditionally, and reports an empty capture as such without an elevation remedy; a kernel image is a module whose `ModuleInstance.loadAddress` is in the kernel address range, else a basename match (`.sys`/`.ko`, `ntoskrnl.exe`/`ntkrnlmp.exe`/`ntkrnlpa.exe`/`ntkrpamp.exe`, `hal.dll`, `vmlinux`/`vmlinuz`/`[kernel.kallsyms]`); suppressed when `session.uprof` records a `translate --time-filter`, because the resolved dump may hold only that window; a user-only scope can neither create nor hide it, and it never refers to elevated user processes; `collect` prints the same limitation before the capture runs |
| `SYMBOL_COVERAGE` | scoped samples that resolve to a non-empty `Function.name`, then each module with unresolved samples and its unresolved share, marked `partial` or `no named functions`; a module with no names cannot be resolved below module/RVA, and the line says to stop there rather than write a PE parser, map `.pdata` by hand or convert RVAs to VAs |
| `AGREEMENT` | `line`/`instruction` only: frame rule, sample-weighted mismatch rate, address counts (the `compared` predicate is both names non-empty), `<no symbol>` sample weight |

`SANITY` validates the entire dump against the **translate** span (the `translate --time-filter` window when recorded, else `collection.schedule.elapsed`); `TIME_SHARE` and `WEIGHTS` describe the **profile** `--time-filter` window. The two windows may legitimately differ. The scoped coverage verdict uses the **profile** `--time-filter` end when one is given, else the translate span; it, not the dump-wide verdict, answers whether the profiled population was covered.

## Frame rule and sentinels

- `line` and `instruction` take `file`, `line` and `function` from the innermost executed frame (llvm-symbolizer `Symbol[0]`); `instruction` is one row per module + RVA.
- `function` is uProf's `Function.id`, the function containing an inlined instance. `AGREEMENT` compares the containing function (`Symbol[last]`), so an inline instance is never a name mismatch.
- **Inline-overload limitation:** an inlined frame carries no signature. It keys on the short name, so when only one out-of-line signature exists for that name it is displayed under that signature even if it belongs to a different same-named overload.
- Sentinels: `<no symbol>` (no function name; groups per module), `<no source line>` (function but no file/line; groups per module + function), `<multiple>` (a line group whose function or module annotations disagree). A frame with a file/line but no function stays under `<no symbol>`, and its weight is disclosed in `AGREEMENT`.
- `attribution` per row: `unique`, `shared` (folded address) or `unresolved`; function rows are only `unique` or `unresolved`; remainder rows carry `NULL`.

## Instruction context (JSON only)

`profile --by instruction --json` adds a `context` object to the first 20 non-aggregate instruction rows in the table's impact order (time weight where the capture records it); a row that omits still consumes one of those 20 slots. Markdown output is unchanged. The block is built only from LLVM answers and the capture; when no safe answer exists the row carries an omission object instead of guessed data.

- **Anchor.** Decode forward from the nearest safe start at or below the hot RVA inside its executable PE section. Safe starts are PE exports (`llvm-readobj --coff-exports`), PDB public symbols and `.pdata` runtime functions (`llvm-readobj --unwind`); at the same RVA an export wins, then a PDB public, then a `.pdata` start. Without any of them the section start is used. The distance cap is 1 MiB. The hot RVA must appear as an instruction start; there is no backward decode. `anchor.kind` names the source that supplied the start: `export`, `pdb_public`, `pdata` or `section`.
- **Window.** Up to 32 decoded instructions before the hot RVA and up to 32 after it, plus the hot instruction. Decoding stops at the containing section's end.
- **Share.** Each instruction's `samples` count how often the hardware selected that RVA for sampling — sampling/dispatch frequency, not impact, and never a priority signal; `sample_share` is that raw share as a fraction of `v_scope_totals.total_samples`, read with the same mode-specific meaning as the row (`legend.columns.sample_share`), never the gap-weighted time share. The table's time weight (`DeltaTick` gaps), where the capture records it, is the impact ordering, and the per-instruction IBS fields are the cost evidence.
- **Document help.** `--json` emits one top-level `context_help` object after `legend` and before `rows`, in a fixed key order: `scope`, `share`, `fields`, `omission_reasons`, `read`, `next`. It names the emitted fields (`anchor`, `build`, `instructions` with `rva`, `instruction`, `samples`, `sample_share`, `symbol`, `import`, `ibs`; `attribution` only when the table emits it) and every omission reason. It does not restate the unit: `share` points to `legend.share_unit`, `legend.share_scale`, `legend.denominators.sample_share` and `legend.columns.sample_share`, so the context cannot drift from the table's meaning. It says the 20-row cap counts row slots (an omitted row still consumes one), that `instruction` may retain objdump's nearest-symbol `<...>` decoration while `symbol` is the frame-0 name, and that IBS field values are AMD-encoded counts or histograms, not decoded meanings. `read` says the sampled instruction is the entry whose `rva` equals the row's, that `sample_share` is the mode-specific raw share and not a per-op cost or a ranking, and that rows are ordered by `legend.impact.order_by`; `next` defers to `legend.next`.
- **Annotations.** `symbol` is `llvm-symbolizer` frame 0 (`function`, `file`, `line`) and never an objdump nearest-symbol label; it uses the PDB that symbolization accepted for the module. `import` names a `callq *IAT` target from `llvm-readobj --coff-imports`; unresolved or non-IAT targets stay raw, and an ordinal-only import is named `#N`. `ibs` aggregates the dump's whitelisted IBS OP fields per RVA over the same PID, image and time window as the row's samples; it is absent for PMC and for dumps without whitelisted columns, and `{"samples": n}` alone is a valid summary.
- **Cost.** Each anchor is decoded once for all rows under it, and samples, symbols and IBS are aggregated once per module.
- **Build identity.** The PE `ImageBase`, `TimeDateStamp`, `CheckSum` and `SizeOfImage` are read from the module. A nonzero on-disk `CheckSum` equal to the capture's `Module.checksum` verifies the build (`identity: "checksum"`); a nonzero mismatch omits it (`build_mismatch`). Without a usable checksum, an equal `SizeOfImage` verifies it (`identity: "size"`). Without matching evidence the block is emitted with `verified: false`, `identity: "unverified"` and a `warning` naming what was missing or disagreed — it never claims verification it cannot show.

```json
{
  "anchor": {"kind": "export", "rva": "0x1010", "name": "hot", "distance": 11},
  "build": {"verified": true, "identity": "checksum", "image_base": "0x140000000",
            "time_date_stamp": "0x6AEF5E36", "checksum": "0x1234ABCD",
            "capture_checksum": "0x1234ABCD", "size_of_image": "0x4000",
            "capture_size": "0x4000"},
  "instructions": [
    {"rva": "0x1010", "instruction": "addl\t$0x3, %eax", "samples": 3,
     "sample_share": 0.05, "symbol": {"function": "main", "file": "main.cpp", "line": 12},
     "ibs": {"samples": 3, "DataSrc": {"0x3": 3}, "IbsDcMiss": {"set": 0, "of": 3}}}
  ]
}
```

Omission: `{"omitted": true, "reason": "<code>", "detail": "<text>"}`. `detail` is present only when it adds information; reason codes are stable. A row that omits still consumes one of the 20 slots. An omission made after the module was examined carries the same `build` object as a successful block, so an unverified build or a `SizeOfImage` mismatch stays visible on the omitted row; `build_mismatch` reports the mismatching checksum in `detail` and `warning`.

| Reason | Meaning |
|---|---|
| `module_missing` | the capture records no path, or the module file is absent on this host |
| `readobj_missing`, `readobj_failed` | `llvm-readobj` absent, or header/section output unavailable |
| `metadata_incomplete` | the PE headers lack `ImageBase`, `TimeDateStamp`, `CheckSum` or `Magic` (for example an ELF module) |
| `no_code_section` | the hot RVA is not inside an executable PE section |
| `anchor_too_far` | the nearest safe start is beyond the 1 MiB cap |
| `hot_rva_not_instruction_start` | decoding from the anchor never reaches the hot RVA on a boundary |
| `objdump_missing`, `objdump_failed` | `llvm-objdump` absent, or it produced no instructions |
| `build_mismatch` | a nonzero on-disk `CheckSum` differs from the capture's `Module.checksum` |
| `context_failed` | an LLVM tool timed out or a scoped query failed while building this block |
| `row_cap` | this row is past the 20-row context cap |

The aggregate `<other instructions>` row carries no `context`.

## Shared addresses

`/OPT:ICF` folds identical bodies, so one RVA can carry several public names. When `llvm-pdbutil` is available on Windows, symbolization builds a per-module public-symbol cache and labels each folded start `shared: nameA | nameB` with `attribution = shared`, at `line` and `instruction` granularity only. Report that share as shared, not under one name. Without `llvm-pdbutil` the cache is absent (`FOLDS: skipped`) and every resolved row is `unique`; unresolved rows still exist. Resolved Linux DWARF symbols are all `unique` (unverified here).

## Failure behaviour

| Condition | Behaviour | Exit |
|---|---|---|
| dump core time exceeds the `session.uprof` span × dump CPUs, or a sample lies outside that span's bounds (for either capture mode) | error naming the dump and the arithmetic or the outside samples, with the remedy: delete the dump or its cache directory and re-run `profile` so it is regenerated on a copy | 2 |
| `session.uprof` missing | error before any query; `time_share` needs it for the sampler and the span check | 2 |
| `cpu.db` missing | error naming the file and the `translate --session ... --out-dir ...` step | 2 |
| translate `--out-dir` is an existing non-directory or equals `--session`; a copy fails (permission, read-only parent); a profile dump cache or symbol-artifact temp path cannot be created or written | `error:` with the offending path or rule and a permissions/free-space remedy; no traceback | 2 |
| translate `--out-dir` already holds `cpu.db`, `callstack.db` or an ASCII dump and `--force` was not given | error naming the files and the remedy: choose another `--out-dir` or pass `--force`; `--force` replaces the generated files and copied session inputs, never unrelated files | 2 |
| no matching PDB, missing binary, no image GUID, or `llvm-readobj` missing/failed | warning; addresses become `<no symbol>`; `AGREEMENT` reports the weight | 0 |
| required tool missing (uProf CLI, DuckDB 2.x, llvm-symbolizer) | `tools` prints paths and versions and `error: MISSING ...`; a symbolized profile fails at symbolization | 2 |
| `--sample-interval` is a time in IBS op-count mode, a bare integer in a cycle mode, or a non-positive work count | error naming the required form and the mode | 2 |
| `--time-filter` on a PMC capture | error: this export aggregates PMC samples per window and carries no per-sample timestamps | 2 |
| IBS OP capture where AMDuProfCLI reports IBS counters unavailable | explicit one-line `SAMPLER: falling back to cycle sampling ...`, then a cycle capture; any other IBS failure is an error | 0 on the fallback, 2 otherwise |
| capture export missing tables/columns or a bad event column | schema guard names every missing item and exits before querying | 2 |
| `--out-file` equals `cpu.db`, `session.uprof` or the dump; directory missing; path is a directory | error; the input is never overwritten | 2 |
| empty scope | `WARNING: no samples in the selected scope` plus an empty table | 0 |
| applied sampler requested OS mode but the whole capture (before scope and time filters) has no kernel-image sample | `KERNEL_COVERAGE` line above the table stating the sampler's OS-mode bit and that the capture has no kernel-image sample, with elevation named conditionally; an empty capture is reported as such instead. The table is still emitted; `collect` printed the same limitation before the capture ran | 0 |

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
- `--json`: one object with the same header fields in order, then `"legend"` — the machine-readable `share_unit`, `share_scale`, `denominators`, `columns`, `absent`, `impact`, `remainder_last` and `next` — then `"rows"`, an array keyed by the table's column names. `columns` gives each column's measure, `absent` gives the omitted columns and their reasons, `impact` gives the ordering estimate, and `next` gives the drill-down calls. Values are identical modulo display escaping; only Markdown escaping differs.
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

`references/reference.md`: metadata keys, estimator derivation, dump cache, symbolizer invocations and artifact schemas, the 16 SQL views, the `tag_to_retire_share` formula, known limitations, tie order and troubleshooting. `references/validation.md`: measured acceptance figures (do not restate them here).
