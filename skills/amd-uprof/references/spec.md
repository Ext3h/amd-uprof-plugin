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

`interval` does not exist: its estimator is the source of every `pct_time` column, and its sanity
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
| `process` | process | process, pid, samples, pct_samples, pct_time, pct_cycles | no |
| `function` | function | function, module, attribution, samples, pct_samples, pct_time, pct_cycles | no |
| `line` | file:line | file, line, function, module, attribution, samples, pct_samples, pct_time, pct_cycles | yes |
| `instruction` | module + RVA | module, rva, function, file, line, attribution, samples, pct_samples, pct_time, pct_cycles | yes |

`pct_time` is absent for PMC captures (no per-sample timestamps); `pct_cycles` is absent for PMC,
IBS fetch, and IBS OP dumps without a usable `IbsTagToRetCtr` column (absent, or carrying no non-null
value); every output states which columns apply and why.

No filter is a prerequisite for any granularity. `line` and `instruction` symbolize every module in
scope and print a uProf-vs-symbolizer agreement summary; modules without usable symbols appear as
`<no symbol>` with the agreement summary stating how much sample weight that covers. The `line`
table aggregates instructions by source position; `instruction` reports them individually, each
annotated with its function and source coordinates.

### 3.4 Time filter

Samples are selected by timestamp. The first sample per CPU **inside the window** carries no weight,
because its gap crosses the boundary — the same rule as the first sample of a capture. Filtering
never re-translates.

### 3.5 Clock anchoring

A workload that prints its own `QueryPerformanceCounter` reading can be placed on the capture
timeline: `anchor` takes the session and that reading and reports seconds from capture start, the
same units `--time-filter` uses. A negative value is attach latency — the reading precedes the
capture start, and the capture does not cover it. Where the session records a start wall-clock time,
the UTC start is reported with its precision.

### 3.6 Output

- Default: a Markdown pipe table, right-aligned numerics, with the header lines (`METRIC:`, `DUMP:`,
  `SANITY:`, `ACHIEVED:`, `AGREEMENT:`) as plain lines above it. `SANITY` validates the dump against
  the capture metadata and states the coverage it is based on; the dump-wide verdict is labeled as
  such, and when `--image`/`--pid` or `--time-filter` restricts the report, a scoped coverage
  verdict follows from the scope's last sample against the end of its window. `ACHIEVED` states the
  samples delivered, the capture window, the per-CPU rate, the requested interval, and the busiest
  sampling context's cadence, and says so when that context delivers far below the request. The
  per-CPU rate divides by every CPU and is not the delivered cadence.
- `--json`: the same rows and header information as structured data.
- No CSV mode and no table-rendering dependency on the database library.
- Percentages are emitted at full precision; no rounding is applied.

## 4. Columns and capture modes

| column | meaning | mode-dependent |
|---|---|---|
| `samples` | raw number of samples in the row | no |
| `pct_samples` | share of samples; what a sample represents depends on the sampler (below) | **yes** |
| `pct_time` | share of elapsed CPU time, from per-sample timestamp gaps | absent for PMC captures (no per-sample timestamps); the estimator still defines its meaning |
| `pct_cycles` | share of sampled op latency (tag-to-retire), clipped; never time | present only for an IBS OP dump with a usable `IbsTagToRetCtr` (at least one non-null value) |

| capture | a sample represents | `pct_samples` is | status |
|---|---|---|---|
| PMC `CYCLES_NOT_IN_HALT` | fixed cycles | share of CPU time | verified |
| IBS OP, interval in cycles | fixed cycles | share of CPU time | implemented, no capture available to verify |
| IBS OP, interval in ops | fixed retired ops | share of instructions — not time | verified |
| IBS fetch | fetch events | fetch share | supported, unverified |

Every output states the capture's mode and what each column measures, derived from the capture's
metadata; for `pct_time` it states the estimator and window, or why `pct_time` is unavailable.
An instruction share must never be presented as CPU time.

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
