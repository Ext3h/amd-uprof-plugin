# amd-uprof skill — specification

What the skill must do; exact keys, enums and flags are implementation vocabulary in
`reference.md`.

Every normative statement carries one ID (`SPEC-nn`), assigned once. An amended statement
keeps its ID; a removed statement's ID is retired, listed under "Retired IDs" and never
reused.

## Naming rules

**SPEC-01** Seven principles bind every output and every report derived from it.

1. **SPEC-02** **Claim only what the measurement supports.** Name the measure and its denominator.
2. **SPEC-03** **State provenance exactly.** Label synthetic and derived names, never source symbols;
   **SPEC-04** attribution is named, derived, shared or unresolved.
3. **SPEC-05** **Do not overclaim structure.** A page is an address bucket; **SPEC-06** third-party is the system flag, and
   a system-flagged vendor dependency is the dominant non-workload dependency.
4. **SPEC-07** **Absence carries cause and remedy.** Never invent evidence; **SPEC-08** a data limit is not a failure.
5. **SPEC-09** **Caller-facing names are neutral.** `--symbol-path` and `--image-path` are the contract.
6. **SPEC-10** **Contract names are verbatim and consistent** across text, JSON and legends.
7. **SPEC-11** **Name the remainder and the truncation.** `<other ...>` is unranked; **SPEC-12** "hot" is the maximum.

## 0. Purpose

**SPEC-13** Profile CPU time with AMD uProf; **SPEC-14** attribute samples to processes, functions, lines or instructions;
**SPEC-15** report sample and time shares, stating what each measures.

## 1. Capture — `collect`

- **SPEC-16** Captures cycle samples by default, supports per-PID attach and IBS OP with a dispatched-op or
  cycle interval, and converts the time rate to cycles at the core frequency, printing the
  conversion; **SPEC-17** an IBS dispatched-op interval is stated as work, not time.
- **SPEC-18** After the capture, verifies that the request matches what was applied and fails on a mismatch.
- **SPEC-19** OS mode is requested, but an unelevated capture has no kernel samples. **SPEC-20** `collect` says so;
  **SPEC-21** `profile` prints `KERNEL_COVERAGE` when OS mode was requested and no kernel sample exists,
  before any scope filter; **SPEC-22** a translate time filter suppresses the check.
- **SPEC-23** An IBS capture the machine cannot perform falls back visibly, naming the cause; **SPEC-24** `--dry-run`
  validates without capturing, and the delivered rate is reported, not assumed.

## 2. Translation — `translate`

- **SPEC-25** Requires an output directory; **SPEC-26** copies the session and translates the copy, never in place, and
  refuses existing generated artifacts unless `--force` removes them and the copied inputs.
- **SPEC-27** Prints the copy's path; **SPEC-28** the source session is unchanged.

## 3. Query surface

### 3.1 Commands

| command | purpose |
|---|---|
| **SPEC-29** `profile` | what dominates, at a chosen granularity |
| **SPEC-30** `anchor` | where a workload's own clock reading sits on the capture timeline |
| **SPEC-31** `schema` | whether the capture's export matches the contract the query surface consumes |
| **SPEC-32** `tools` | which external tools will be used, at which versions |

**SPEC-33** `interval` does not exist; **SPEC-34** its estimator and sanity and scope-coverage verdicts are part of
`profile`.

### 3.2 `profile` arguments

| argument | meaning |
|---|---|
| **SPEC-35** `--session DIR`, `--by process\|module\|function\|region\|line\|instruction` | capture directory and grouping key (both required) |
| **SPEC-36** `--pid N`, `--image PATTERN` | one PID, or every matching base image; **SPEC-37** mutually exclusive |
| **SPEC-38** `--time-filter T1:T2` | seconds from capture start; **SPEC-39** dump captures only; **SPEC-40** rejected for PMC |
| **SPEC-41** `--top N\|all` | rows returned, default 20 |
| **SPEC-42** `--function NAME`, `--module MODULE`, `--function-start RVA` | explicit parent scope; **SPEC-43** never inferred |
| **SPEC-44** `--json`, `--out-file PATH` | machine-readable output and its destination |
| **SPEC-45** `--symbol-path`, `--image-path` | repeatable; **SPEC-46** a symbol/image file or a search root |

**SPEC-47** `translate` stages symbol files; **SPEC-48** `profile` never contacts a symbol URL.

### 3.3 Granularities

**SPEC-49** `--by` groups rows by the selected granularity; **SPEC-50** `function`, `region`, `line` and `instruction`
symbolize, `process` and `module` do not. Row keys and symbolization mechanics are in `reference.md`.

**SPEC-51** Every share is a fraction and `samples` a raw count; **SPEC-52** a supplied measure is present even at zero. **SPEC-53** No
filter is a prerequisite.

**SPEC-54** `--by module` emits the rollup with the system flag, an estimated `cpu_seconds` (omitted with its
reason when not derivable) and the hot address fields; **SPEC-55** `other_samples` is the rest of the row, and
module keys are normalized so a basename and a full path classify together.

**SPEC-56** Function is the semantic unit: the tool derives boundaries from the binary, and the consumer must
not parse the image or map its function tables by hand. **SPEC-57** A synthetic range is visibly labelled and
attributed. **SPEC-58** `unresolved` is only the attribution category: a derived range is attributed, so such a
module is `derived`, never `unresolved` or "no named functions", and rows remain `unresolved` only
when neither a name nor a derived range covers them.

**SPEC-59** Where function rows remain `unresolved` and no boundary source exists, fall back to `--by region`:
region = 4 KiB address page; **SPEC-60** samples are grouped by address, not by control flow; **SPEC-61** a page may span
unrelated code; **SPEC-62** the hot RVA is the most precise attribution and the page is the coarse bucket. **SPEC-63** `line`
and `instruction` symbolize every module in scope, report per-scope agreement and coverage, and
mark modules without usable symbols `<no symbol>` with the share of weight that covers.

**SPEC-64** `line` and `instruction` take an explicit parent scope, never inferred from the top row; **SPEC-65** parent
totals are a join of the capture's rows, not a second measurement and independent of `--top`; **SPEC-66** rows
and the remainder carry the same parent shares, so they sum to the parent total.

**SPEC-67** `--symbol-path` and `--image-path` affect the symbolized rows and the profile symbolizer share, not
the share of samples the capture itself named; **SPEC-68** a source is accepted only when its identity is
verified, and another build's source is a resolution failure, not success.

### 3.4 Time filter

**SPEC-69** Samples are selected by timestamp; **SPEC-70** the first sample per context inside the window carries no weight
because its gap crosses the boundary. **SPEC-71** Filtering never re-translates.

### 3.5 Clock anchoring

**SPEC-72** `anchor` reports seconds from capture start for a workload's own clock reading, the units
`--time-filter` uses; **SPEC-73** a negative value is attach latency, outside the capture. **SPEC-74** A recorded start
wall-clock time is reported as UTC with its precision.

### 3.6 Output

- **SPEC-75** Default: a Markdown pipe table, right-aligned numerics, named header lines in a fixed order. **SPEC-76** The
  legend states the share unit and mode-specific meaning, absent columns with reasons, what to
  read, and the runnable next calls. **SPEC-77** Next calls descend process -> module -> function, stop at the
  derived function, and offer region only while function rows remain `unresolved`.
- **SPEC-78** `--json` carries the same rows and semantics; exact keys and fixed orders are in `reference.md`.
  **SPEC-79** The function view reports the hot address summary: the maximum per-function hot sample count,
  not the top row's address. **SPEC-80** The impact order is the mode's primary measure; **SPEC-81** a time-ordered table
  must not be used to claim the sample-hot address.
- **SPEC-82** A scope with no samples prints one warning on stderr even with `--json`/`--out-file`; **SPEC-83** JSON also
  carries it. **SPEC-84** A process-image filter names the process images and the module remedy.
- **SPEC-85** No CSV mode and no table-rendering dependency on the database library.

**SPEC-86** **Instruction context.** The instruction JSON adds one context per row; **SPEC-87** Markdown bytes are
identical. **SPEC-88** It names the anchor, the build identity and the decoded window with raw samples and
per-instruction shares, defining no second share scale; **SPEC-89** the anchor is the nearest safe start at or
below the hot address, with no backward decode. **SPEC-90** Anything that prevents a correct block omits it
with a stable reason and never guesses; **SPEC-91** an unverified build is emitted unverified. **SPEC-92** The row cap
counts emitted slots in the mode's primary order; **SPEC-93** an omitted row consumes a slot, and the remainder
row carries none. **SPEC-94** Windows modules only.

## 4. Columns and capture modes

**SPEC-95** `sample_share` depends on the sampler; **SPEC-96** `time_share` is a gap-weighted estimate and is absent for PMC
captures; **SPEC-97** `tag_to_retire_share` is the share of tag-to-retire cycles, not per-op latency, and is
absent without a usable tag column; **SPEC-98** parent shares are the row as a share of the selected parent
function.

| capture | a sample represents | `sample_share` is |
|---|---|---|
| **SPEC-99** supported cycle event | fixed cycle count | busy core-cycle share |
| **SPEC-100** other PMC event | one event count | not established |
| **SPEC-101** IBS OP, cycle interval | selected operation | core-cycle share, not wall time |
| **SPEC-102** IBS OP, op interval | selected operation | dispatched-op frequency share |
| **SPEC-103** IBS fetch | fetch event | fetch-sample frequency share |

**SPEC-104** Every output states the capture mode, each column's meaning and any absent column with its cause;
**SPEC-105** an instruction or op share is never presented as CPU time.

## 5. Failure behaviour

**SPEC-106** Never return output that is wrong, incomplete or unrepresentative without saying so. **SPEC-107** A misleading
condition — incompatible capture, unusable sampler, missing tooling, an output path colliding with
an input — is reported with the fix and exits nonzero; **SPEC-108** no degraded result is papered over.

**SPEC-109** Symbolization artifacts may be cached per capture session. **SPEC-110** The cache must never change the result
a cold run would produce: transient failures are not stored, the resolved PDBs and the symbol
search state are validated, and a module the capture maps to more than one image version is not
served from it. **SPEC-111** A cache failure leaves `profile` working, uncached, and is opt-out.

## 6. Shared addresses

**SPEC-112** When several function names share one address, a per-name share is not attributable: report it as
shared and mark the row non-attributable at every symbolized granularity.

## 7. Reproducibility

**SPEC-113** The same capture and arguments produce the same table and JSON on every run, except where a
documented tie order permits permutation.

## 8. Shape rules

| rule | requirement |
|---|---|
| **SPEC-114** single surface | one command with subcommands; **SPEC-115** no parallel entry points |
| **SPEC-116** division of labour | SQL answers every data question; **SPEC-117** Python orchestrates and formats |
| **SPEC-118** no optional toggles | a flag names an input or a semantic choice; **SPEC-119** a preference is always-on or never-on |
| **SPEC-120** no dead weight | no unused parameters, fields or compatibility shims; **SPEC-121** no private library APIs |
| **SPEC-122** single source | shared constants and shared SQL exist once |
| **SPEC-123** token-efficient docs | tables over prose; **SPEC-124** numbers stated once |

## 9. Anti-goals

| must not exist | because |
|---|---|
| **SPEC-125** aggregation-bin weighting for op-count captures | within-bin counts are instruction shares, not time shares |
| **SPEC-126** translating a capture in place, or instructing a user to | destroys the original metadata |
| **SPEC-127** silent sampler fallback or silent estimator substitution | the wrong reading looks correct |
| **SPEC-128** reimplementing a query in Python | duplication, drift, and paths the database already answers |
| **SPEC-129** passing SQL parameters through environment variables | encoding and quoting hazards with no benefit |

## 10. Evidence

Measured acceptance figures, their capture, filter and estimator are recorded in
`validation.md`; they are a regression baseline, not requirements.

## Retired IDs

None.
