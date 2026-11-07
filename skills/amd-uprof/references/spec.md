# amd-uprof skill — specification

This document states what the skill must do. Implementation choices are not specified here; where a
behaviour depends on one, this document says what must be true rather than how.

## 0. Purpose

Give an agent (Windows or Linux) a repeatable way to profile CPU time with AMD uProf: capture,
translate, attribute samples to processes, functions, source lines or instructions, and report
sample shares and time shares — stating what each share measures.

## 1. Capture — `collect`

- Captures PMCx076 `CYCLES_NOT_IN_HALT` cycle samples by default; supports per-PID attach.
- Supports IBS OP with the interval expressed in dispatched ops or in cycles.
- Sample rate is expressed in **time** (`--sample-interval`, e.g. `200us`, `1ms`) and converted to
  cycles using the core frequency reported for the machine; the conversion and the frequency used
  are printed. For IBS in dispatched-op mode the interval is a work count, not a time rate, and is
  stated as such.
- After the capture, verifies that what was requested is what was applied: event, interval,
  sampling enabled, IBS vs PMC, system-wide vs attached PIDs. A mismatch fails.
- OS-mode samples are requested for both capture events: cycle counting passes `user=1,os=1`, and
  IBS samples user and OS by default. The request is applied and encoded, but it is not sufficient
  for kernel coverage. An unelevated capture contains no kernel-mode samples, so kernel coverage
  requires an elevated capture.
- The tool states that limitation at both points where it can: `collect` notes the consequence
  before the capture runs, and `profile` prints `KERNEL_COVERAGE` when the applied sampler requested
  OS mode and the whole capture contains no sample in any kernel image. The profile-time check names
  the sampler's OS-mode bit (`os`, `opOs` or `fetchOs`), reports an empty capture as such, and is
  suppressed when the session records a translate `--time-filter`, because the resolved dump may
  hold only that window. This is distinct from the warning that elevated user processes may be
  missing: that one is about process coverage, this one about kernel coverage.
- An IBS capture that the machine cannot perform (no IBS counters — hypervisor/VBS active, or an
  unsupported CPU) falls back to cycle sampling **visibly**: one line naming the cause before the
  fallback capture runs.
- `--dry-run` validates the invocation without capturing.
- The delivered sample rate can be below the requested interval. The tool reports the achieved
  rate rather than assuming the request was met (see `profile`'s `ACHIEVED:` header).

## 2. Translation — `translate`

- Requires an output directory; copies the session and translates the copy.
- Never translates in place, and no message anywhere instructs a user to do so.
- An output directory that already holds `cpu.db`, `callstack.db` or an ASCII dump is refused
  unless `--force`; `--force` removes the generated files and the copied session inputs first, so
  a repeated translate produces the same counts as a fresh one and unrelated files are untouched.
- Prints the session path of the copy.
- The source session's files and timestamps are unchanged afterwards.

## 3. Query surface

### 3.1 Commands

| command | purpose |
|---|---|
| `profile` | what dominates, at a chosen granularity |
| `anchor` | where a workload's own clock reading sits on the capture timeline |
| `schema` | whether the capture's export is the one the SQL expects |
| `tools` | which external tools will be used, at which versions |

`interval` does not exist: its estimator is the source of every `time_share` column, and its sanity
line appears in the `profile` header, which validates the dump it consumes, whether generated on
demand or reused from the cache.

### 3.2 `profile` arguments

| argument | meaning |
|---|---|
| `--session DIR` | capture directory (required) |
| `--by process\|function\|line\|instruction` | grouping key (required) |
| `--pid N` | one process by PID; mutually exclusive with `--image` |
| `--image PATTERN` | base-image path/name filter, all PIDs of matching images |
| `--time-filter T1:T2` | optional, seconds from capture start; default the whole capture; requires a dump capture (per-sample timestamps); PMC captures carry none and the tool rejects the filter |
| `--top N\|all` | rows returned, default 20 |
| `--json` | machine-readable output instead of the default table |
| `--out-file PATH` | output destination |

### 3.3 Granularities

| `--by` | row key | columns | symbolization |
|---|---|---|---|
| `process` | process | process, pid, samples, sample_share, time_share, tag_to_retire_share | no |
| `function` | function | function, module, attribution, samples, sample_share, time_share, tag_to_retire_share | no |
| `line` | file:line | file, line, function, module, attribution, samples, sample_share, time_share, tag_to_retire_share | yes |
| `instruction` | module + RVA | module, rva, function, file, line, attribution, samples, sample_share, time_share, tag_to_retire_share | yes |

Every share is a fraction 0.0-1.0; `samples` stays a raw count. `time_share` is absent for PMC
captures (the export aggregates PMC samples per window); `tag_to_retire_share` is absent for PMC,
IBS fetch, and IBS OP dumps without a usable `IbsTagToRetCtr` column (absent, or carrying no
non-null value); every output states which columns apply and why. An all-zero `IbsTagToRetCtr`
has non-null values, so the column is present, but every `tag_to_retire_share` is NULL and the
column does not sum to 1.0 because no tag cycles were counted. A tag cell counts only when it is
an unsigned decimal integer; negative, fractional and exponent spellings are excluded.

No filter is a prerequisite for any granularity. `line` and `instruction` symbolize every module in
scope and print a uProf-vs-symbolizer agreement summary; modules without usable symbols appear as
`<no symbol>` with the agreement summary stating how much sample weight that covers. The `line`
table aggregates instructions by source position; `instruction` reports them individually, each
annotated with its function and source coordinates.

### 3.4 Time filter

Samples are selected by timestamp. The first sample per context **inside the window** carries no
weight, because its gap crosses the boundary — the same rule as the first sample of a capture.
The context is a CPU for a system-wide capture and a thread (`PID`, `TID`) for an attached one.
Filtering never re-translates.

### 3.5 Clock anchoring

A workload that prints its own `QueryPerformanceCounter` reading can be placed on the capture
timeline: `anchor` takes the session and that reading and reports seconds from capture start, the
same units `--time-filter` uses. A negative value is attach latency — the reading precedes the
capture start, and the capture does not cover it. Where the session records a start wall-clock time,
the UTC start is reported with its precision.

### 3.6 Output

- Default: a Markdown pipe table, right-aligned numerics, with the header lines (`METRIC:`, `DUMP:`,
  `SANITY:`, `ACHIEVED:`, `KERNEL_COVERAGE:`, `AGREEMENT:`) as plain lines above it. `SANITY` validates the dump against
  the capture metadata and states the coverage it is based on; the dump-wide verdict is labeled as
  such, and when `--image`/`--pid` or `--time-filter` restricts a system-wide report, a scoped
  coverage verdict follows from the scope's last sample against the end of its window. The
  dump-wide capacity is the recorded span × CPUs for a system-wide capture; an attached capture is
  judged per thread instead (never `elapsed × CPUs`), so its verdict is thread coverage and errors
  only for samples outside the span; it also reports the processes, TIDs, busiest TID's samples
  and CPUs visited. Attached captures (or a missing
  `systemWide`, which counts as attached) keep the attached verdict instead, because their
  coverage is where the attached threads ran. `ACHIEVED` states the
  samples delivered, the capture window, the per-CPU rate, the requested interval, and the busiest
  sampling context's cadence, and says so when that context delivers far below the request. The
  per-CPU rate divides by every CPU and is not the delivered cadence.
  `KERNEL_COVERAGE` appears when an OS-mode request produced no kernel-image sample in the whole
  capture — computed before `--image`/`--pid` and `--time-filter`, so a user-only scope can neither
  create nor hide it — and states that elevation is required; it is the profile-time counterpart of
  the capture-time note.
- Every output carries a short three-line legend. `COLUMNS` states the unit once (share columns are
  fractions 0.0-1.0), `samples` as raw count, `sample_share`'s mode-dependent meaning, the
  `time_share` and `tag_to_retire_share` labels, the `attribution` definition when the scope emits
  it, and any absent column with its reason; the detailed definitions stay in the `METRIC`,
  `TIME_SHARE` and `TAG_TO_RETIRE_SHARE` header lines. `READ` states which column ranks where time
  went, that `sample_share` is not a per-op cost, that `tag_to_retire_share` is total tag-to-retire
  cycles (higher = more wait), and that the `<other ...>` row is last, not ranked. `NEXT` gives
  full runnable calls joined by `&&`, each derived from the current `--by` and keeping the session,
  the `--pid`/`--image`/time-filter scope and a non-default `--top`, with the last call adding
  `--json` to `--by instruction`; the JSON `next` array holds the same commands.
- `--json`: the same rows, header information and legend semantics as structured data; the legend
  is a `legend` object with `share_unit` (`fraction`), `share_scale` (`1`), `denominators`,
  `columns`, `absent`, `impact` (`order_by`, `gap_weighted`, `estimate`), `remainder_last` and
  `next`.
- No CSV mode and no table-rendering dependency on the database library.
- Shares are fractions 0.0-1.0 at full precision; no rounding or multiplication is applied.

**Instruction context.** JSON output for `--by instruction` adds a `context` object to instruction rows and nothing else changes. Markdown bytes are identical to the output without the block. The block is JSON-only because it is a nested, variable-length structure, not a table.

- **Content.** The context names the anchor it decoded from, the build identity it verified, and the decoded instruction window with the raw sample count and raw sample share per instruction, plus optional `llvm-symbolizer` frame-0 location, `llvm-readobj` IAT import name and aggregated IBS OP fields.
- **Anchor and window.** The anchor is the nearest safe start at or below the hot RVA — a PE export, a PDB public symbol or a `.pdata` runtime function, preferring an export, then a public, then `.pdata` at the same RVA — or the containing code-section start within the distance cap. `anchor.kind` names the source. There is no backward decode and no symmetric window. The window is at most 32 decoded instructions before the hot RVA and at most 32 after it. The hot RVA must decode as an instruction start.
- **Raw share rule.** The per-instruction share is the raw sample share at that RVA. A raw sample is an operation the hardware selected for sampling, so this count is sampling/dispatch frequency, not impact: a frequently dispatched instruction can be irrelevant in cost, and the share is never a priority signal. The table's time weight (`DeltaTick` gaps), where the capture records it, remains the impact ordering, and the IBS event and latency fields are the cost evidence.
- **Row cap.** At most 20 rows carry a context, in the table's impact (time-weight) order; later rows carry the `row_cap` omission. The `<other instructions>` row carries none.
- **Build identity.** A matching nonzero PE `CheckSum` verifies the build; without a usable checksum a matching recorded `SizeOfImage` does. With no matching evidence the block is emitted with `identity: "unverified"` and a warning, never omitted as unverified; a nonzero checksum mismatch omits it.
- **Omission.** Any condition that prevents a correct block (missing module, no safe anchor, hot RVA not on a boundary, mismatched build, unavailable LLVM tool) omits the block with a stable machine-readable reason and never guesses. A missing symbolizer name or unresolved IAT target leaves that optional field absent; it does not omit the block.
- **Portability.** The block is built from PE/COFF headers and import tables, so it is produced for Windows modules only.

## 4. Columns and capture modes

| column | meaning | mode-dependent |
|---|---|---|
| `samples` | raw number of samples in the row | no |
| `sample_share` | fraction of samples; what a sample represents depends on the sampler (below) | **yes** |
| `time_share` | share of time estimated from per-sample DeltaTick gaps, partitioned by CPU for a system-wide capture and by `(PID, TID)` for an attached one; an estimate, not unqualified impact. An attached gap includes off-CPU time; for a cycle-controlled capture `sample_share` is the CPU-cycle share and the better ranking | absent for PMC captures (the export aggregates PMC samples per window); the estimator still defines its meaning |
| `tag_to_retire_share` | share of tag-to-retire cycles summed over sampled ops (AMD tag at dispatch to retirement, queue/stalls included; 16-bit counter clipped at 65535); a total, not per-op latency | present only for an IBS OP dump with a usable `IbsTagToRetCtr` (at least one non-null value) |

| capture | a sample represents | `sample_share` is | status |
|---|---|---|---|
| PMC, supported cycle event (`CYCLES_NOT_IN_HALT`, hwId 118, sampling) | fixed cycle count | busy core-cycle share (halted excluded) | verified |
| PMC, any other event (not the supported cycle event, or not sampling) | one event-column count | not established | not established |
| IBS OP, interval in cycles | selected op (cycle-controlled interval) | core-cycle share (fixed cycles per sample), not wall time, not a per-op cost | implemented; verified on an external cycle-controlled capture |
| IBS OP, interval in ops | selected op (op-count interval) | dispatched-op frequency share (fixed dispatched ops per sample), not a per-op cost | verified |
| IBS fetch | fetch event | fetch-sample frequency share (fixed fetch events per sample) | supported, unverified |

Every output states the capture's mode and what each column measures, derived from the capture's
metadata; for `time_share` it states the estimator, the partition (CPU or `(PID, TID)`) and the
window, or why `time_share` is unavailable.
An instruction or op share must never be presented as CPU time or as a per-operation cost. The
`COLUMNS` legend restates the `sample_share` reading for the mode. For the supported cycle event,
fixed cycles make the share the busy core-cycle share and the sample order the ranking; any other
PMC event leaves it not established. For op-count and fetch, a raw sample counts selected
operations or events, so the share is a frequency share; for cycle-controlled it is a core-cycle
share, not wall time. None is a per-op cost, and the cheap-often/expensive-rarely caveat applies
only to frequency shares. `READ` names `time_share` (estimated from gaps; per `(PID, TID)` for an
attached capture, where a cycle-controlled `sample_share` is named as the CPU-cycle share and the
better ranking) as
the ordering estimate; `tag_to_retire_share`, where present, is total tag-to-retire cycles, a
total, not per-op latency.

## 5. Failure behaviour

The tool must never return output that is wrong, incomplete or unrepresentative without saying so.
Any condition that would make a result misleading — incompatible capture, unusable sampler, missing
tooling, an output path colliding with an input — is detected, reported with the action that fixes
it, and exits nonzero. The specific checks are implementation choices and are deliberately not
enumerated here; the binding property is the prohibition on papering over a degraded result.

## 6. Shared addresses

When several function names share one address (COMDAT folding, aliases), a per-name share is not
attributable. The tool detects addresses shared by more than one public name, reports their share as
shared rather than attributing it to one name, and marks the row non-attributable. Detection
applies at the granularities where symbols are resolved (`line`, `instruction`); `function` rows
are labelled `unique` or `unresolved` only. A binary built
with `/OPT:ICF` with two names on one RVA is the measure of this behaviour.

## 7. Reproducibility

The same capture and arguments produce the same table and JSON on every run, except where a
documented tie order permits permutation.

## 8. Shape rules

| rule | requirement |
|---|---|
| single surface | one command with subcommands; no parallel entry points |
| division of labour | SQL answers every question about the data — joins, filters, shares, ranking, tie-breaks; Python only orchestrates, parses metadata, calls external tools and formats results |
| no optional toggles | a flag names an input or a semantic choice; a preference is decided always-on or never-on and appears as behaviour, not as a flag |
| no dead weight | no unused parameters, fields or compatibility shims; no private library APIs |
| single source | shared constants and shared SQL exist once |
| token-efficient docs | tables over prose; measured numbers stated once |

## 9. Anti-goals

| must not exist | because |
|---|---|
| aggregation-bin weighting as a time estimator for op-count captures | within-bin counts are instruction shares; measured error reached 22–23 pp at the dominant function, and per-core normalisation changed shares by ≤0.007 pp |
| translating a capture in place, or instructing a user to | destroys the original metadata |
| silent sampler fallback or silent estimator substitution | the wrong reading looks correct |
| reimplementing a query in Python | duplication, drift, and paths the database already answers |
| passing SQL parameters through environment variables | encoding and quoting hazards with no benefit |

## 10. Evidence

Measured acceptance figures, their capture, filter and estimator are recorded in
`references/validation.md` in the skill. They are a regression baseline, not requirements.
