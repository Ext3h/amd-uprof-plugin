# amd-uprof skill — specification

What the skill must do; exact keys, enums and flags are implementation vocabulary in
`references/reference.md`.

## Naming rules

Seven principles bind every output and every report derived from it.

1. **Claim only what the measurement supports.** Name the measure and its denominator.
2. **State provenance exactly.** Label synthetic and derived names, never source symbols;
   attribution is named, derived, shared or unresolved.
3. **Do not overclaim structure.** A page is an address bucket; third-party is the system flag, and
   a system-flagged vendor dependency is the dominant non-workload dependency.
4. **Absence carries cause and remedy.** Never invent evidence; a data limit is not a failure.
5. **Caller-facing names are neutral.** `--symbol-path` and `--image-path` are the contract.
6. **Contract names are verbatim and consistent** across text, JSON and legends.
7. **Name the remainder and the truncation.** `<other ...>` is unranked; "hot" is the maximum.

## 0. Purpose

Profile CPU time with AMD uProf; attribute samples to processes, functions, lines or instructions;
report sample and time shares, stating what each measures.

## 1. Capture — `collect`

- Captures cycle samples by default, supports per-PID attach and IBS OP with a dispatched-op or
  cycle interval, and converts the time rate to cycles at the core frequency, printing the
  conversion; an IBS dispatched-op interval is stated as work, not time.
- After the capture, verifies that the request matches what was applied and fails on a mismatch.
- OS mode is requested, but an unelevated capture has no kernel samples. `collect` says so;
  `profile` prints `KERNEL_COVERAGE` when OS mode was requested and no kernel sample exists,
  before any scope filter; a translate time filter suppresses the check.
- An IBS capture the machine cannot perform falls back visibly, naming the cause; `--dry-run`
  validates without capturing, and the delivered rate is reported, not assumed.

## 2. Translation — `translate`

- Requires an output directory; copies the session and translates the copy, never in place, and
  refuses existing generated artifacts unless `--force` removes them and the copied inputs.
- Prints the copy's path; the source session is unchanged.

## 3. Query surface

### 3.1 Commands

| command | purpose |
|---|---|
| `profile` | what dominates, at a chosen granularity |
| `anchor` | where a workload's own clock reading sits on the capture timeline |
| `schema` | whether the capture's export matches the contract the query surface consumes |
| `tools` | which external tools will be used, at which versions |

`interval` does not exist; its estimator and sanity and scope-coverage verdicts are part of
`profile`.

### 3.2 `profile` arguments

| argument | meaning |
|---|---|
| `--session DIR`, `--by process\|module\|function\|region\|line\|instruction` | capture directory and grouping key (both required) |
| `--pid N`, `--image PATTERN` | one PID, or every matching base image; mutually exclusive |
| `--time-filter T1:T2` | seconds from capture start; dump captures only; rejected for PMC |
| `--top N\|all` | rows returned, default 20 |
| `--function NAME`, `--module MODULE`, `--function-start RVA` | explicit parent scope; never inferred |
| `--json`, `--out-file PATH` | machine-readable output and its destination |
| `--symbol-path`, `--image-path` | repeatable; a symbol/image file or a search root |

`translate` stages symbol files; `profile` never contacts a symbol URL.

### 3.3 Granularities

`--by` groups rows by the selected granularity; `function`, `region`, `line` and `instruction`
symbolize, `process` and `module` do not. Row keys and symbolization mechanics are in `reference.md`.

Every share is a fraction and `samples` a raw count; a supplied measure is present even at zero. No
filter is a prerequisite.

`--by module` emits the rollup with the system flag, an estimated `cpu_seconds` (omitted with its
reason when not derivable) and the hot address fields; `other_samples` is the rest of the row, and
module keys are normalized so a basename and a full path classify together.

Function is the semantic unit: the tool derives boundaries from the binary, and the consumer must
not parse the image or map its function tables by hand. A synthetic range is visibly labelled and
attributed. `unresolved` is only the attribution category: a derived range is attributed, so such a
module is `derived`, never `unresolved` or "no named functions", and rows remain `unresolved` only
when neither a name nor a derived range covers them.

Where function rows remain `unresolved` and no boundary source exists, fall back to `--by region`:
region = 4 KiB address page; samples are grouped by address, not by control flow; a page may span
unrelated code; the hot RVA is the precise attribution and the page is the coarse bucket. `line`
and `instruction` symbolize every module in scope, report per-scope agreement and coverage, and
mark modules without usable symbols `<no symbol>` with the share of weight that covers.

`line` and `instruction` take an explicit parent scope, never inferred from the top row; parent
totals are a join of the capture's rows, not a second measurement and independent of `--top`; rows
and the remainder carry the same parent shares, so they sum to the parent total.

`--symbol-path` and `--image-path` affect the symbolized rows and the profile symbolizer share, not
the share of samples the capture itself named; a source needs verified identity or, if
unverifiable, the module's PDB name and a warning; another build's source fails.

### 3.4 Time filter

Samples are selected by timestamp; the first sample per context inside the window carries no weight
because its gap crosses the boundary. Filtering never re-translates.

### 3.5 Clock anchoring

`anchor` reports seconds from capture start for a workload's own clock reading, the units
`--time-filter` uses; a negative value is attach latency, outside the capture. A recorded start
wall-clock time is reported as UTC with its precision.

### 3.6 Output

- Default: a Markdown pipe table, right-aligned numerics, named header lines in a fixed order. The
  legend states the share unit and mode-specific meaning, absent columns with reasons, what to
  read, and the runnable next calls. Next calls descend process -> module -> function, stop at the
  derived function, and offer region only while function rows remain `unresolved`.
- `--json` carries the same rows and semantics; exact keys and fixed orders are in `reference.md`.
  The function view reports the hot address summary: the maximum per-function hot sample count,
  not the top row's address. The impact order is the mode's primary measure; a time-ordered table
  must not be used to claim the sample-hot address.
- A scope with no samples prints one warning on stderr even with `--json`/`--out-file`; JSON also
  carries it. A process-image filter names the process images and the module remedy.
- No CSV mode and no table-rendering dependency on the database library.

**Instruction context.** The instruction JSON adds one context per row; Markdown bytes are
identical. It names the anchor, the build identity and the decoded window with raw samples and
per-instruction shares, defining no second share scale; the anchor is the nearest safe start at or
below the hot address, with no backward decode. Anything that prevents a correct block omits it
with a stable reason and never guesses; an unverified build is emitted unverified. The row cap
counts emitted slots in the mode's primary order; an omitted row consumes a slot, and the remainder
row carries none. Windows modules only.

## 4. Columns and capture modes

`sample_share` depends on the sampler; `time_share` is a gap-weighted estimate and is absent for PMC
captures; `tag_to_retire_share` is the share of tag-to-retire cycles, not per-op latency, and is
absent without a usable tag column; parent shares are the row as a share of the selected parent
function.

| capture | a sample represents | `sample_share` is |
|---|---|---|
| supported cycle event | fixed cycle count | busy core-cycle share |
| other PMC event | one event count | not established |
| IBS OP, cycle interval | selected operation | core-cycle share, not wall time |
| IBS OP, op interval | selected operation | dispatched-op frequency share |
| IBS fetch | fetch event | fetch-sample frequency share |

Every output states the capture mode, each column's meaning and any absent column with its cause;
an instruction or op share is never presented as CPU time.

## 5. Failure behaviour

Never return output that is wrong, incomplete or unrepresentative without saying so. A misleading
condition — incompatible capture, unusable sampler, missing tooling, an output path colliding with
an input — is reported with the fix and exits nonzero; no degraded result is papered over.

Symbolization artifacts may be cached per capture session. The cache must never change the result
a cold run would produce: transient failures are not stored, the resolved PDBs and the symbol
search state are validated, and a module the capture maps to more than one image version is not
served from it. A cache failure leaves `profile` working, uncached, and is opt-out.

## 6. Shared addresses

When several function names share one address, a per-name share is not attributable: report it as
shared and mark the row non-attributable at every symbolized granularity.

## 7. Reproducibility

The same capture and arguments produce the same table and JSON on every run, except where a
documented tie order permits permutation.

## 8. Shape rules

| rule | requirement |
|---|---|
| single surface | one command with subcommands; no parallel entry points |
| division of labour | SQL answers every data question; Python orchestrates and formats |
| no optional toggles | a flag names an input or a semantic choice; a preference is always-on or never-on |
| no dead weight | no unused parameters, fields or compatibility shims; no private library APIs |
| single source | shared constants and shared SQL exist once |
| token-efficient docs | tables over prose; numbers stated once |

## 9. Anti-goals

| must not exist | because |
|---|---|
| aggregation-bin weighting for op-count captures | within-bin counts are instruction shares, not time shares |
| translating a capture in place, or instructing a user to | destroys the original metadata |
| silent sampler fallback or silent estimator substitution | the wrong reading looks correct |
| reimplementing a query in Python | duplication, drift, and paths the database already answers |
| passing SQL parameters through environment variables | encoding and quoting hazards with no benefit |

## 10. Evidence

Measured acceptance figures, their capture, filter and estimator are recorded in
`references/validation.md`; they are a regression baseline, not requirements.
