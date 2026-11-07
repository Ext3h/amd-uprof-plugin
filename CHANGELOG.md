# Changelog

All notable changes to this plugin. Format: Keep a Changelog; versioning: SemVer.

## 1.0.6 — 2026-11-07

### Fixed

- `ACHIEVED` no longer warns on a capture whose per-CPU average is diluted by idle CPUs. It now
  reports the busiest sampling context's cadence (median running `DeltaTick` gap for a dump,
  samples/s for PMC) and the number of active contexts, and the below-10% warning is based only
  on that context. The per-CPU average stays in the line, labeled as the whole-capture figure it
  is. An intermittent attached thread whose running gaps are at the requested interval no longer
  warns. `profile` uses `TID` as the context for attached captures and `CPU_Number` for
  system-wide ones; `v_dump` now exposes `tid`.

### Changed

- Documentation states the corrected picture: the collector delivers about 1,000/s on a busy CPU
  at a 1 ms request, the old 14/s and 92/s figures were averages over every CPU in the capture,
  and uProf's interval bounds (IBS 50,000-134,217,727 cycles, PMC at least 25,000 cycles) constrain
  the request rather than the delivered rate.

## 1.0.5 — 2026-11-07

### Fixed

- Dump sanity: samples outside the recorded span raise their own error naming those samples,
  instead of claiming core time exceeds a bound it does not; a recorded `translate --time-filter`
  is checked against its `[T1, T2)` window, so a window that starts after capture start no longer
  reports a false inconsistency.
- `ACHIEVED` on PMC captures counts `UnifiedSampleSeries` event-column samples over the whole
  capture, not the `--image`/`--pid` scope, and takes the CPU count from `coreId`, so it reports
  the per-CPU rate and the below-10% warning instead of zero samples and a claim that no rate is
  derivable.
- `translate --ascii` prints `ACHIEVED` counted from the dump it wrote, and says the rate is
  reported at profile time when it writes no dump.

### Changed

- Documentation matches the shipped checks: the `SANITY`/`ACHIEVED` header fields and failure
  modes in `SKILL.md`, the sanity, achieved-rate, anchoring and view descriptions in
  `references/reference.md`, and the validation record, including the attached capture's 39.682 s
  elapsed and the PMC and translate evidence. `anchor` states that `--freq` defaults to 10 MHz
  because `session.uprof` records no QPC frequency.

## 1.0.4 — 2026-11-07

### Changed

- The plugin icon is redrawn as a heat-mapped flame graph: each block's colour is sampled from
  its exclusive share of the block width, from faint yellow through orange to crimson, and the
  micro sign takes a colour just above mid-heat. The vector master ships as `icon.svg` alongside
  the `icon.png` raster.

## 1.0.3 — 2026-11-07

### Added

- Plugin icon: a flame-graph mark whose layout derives from golden-ratio divisions of the icon,
  with the micro sign set in Open Sans Regular — the typeface AMD uProf ships and uses for it.

### Changed

- Description now reads "built on AMD uProf"; the README states the plugin is not affiliated
  with or endorsed by Advanced Micro Devices.

## 1.0.2 — 2026-11-07

### Added

- MIT `LICENSE` (root) and the `license` field in the plugin manifest.

## 1.0.1 — 2026-11-07

### Changed

- `references/spec.md` aligned with the shipped behaviour: the `anchor` command, the `SANITY` and
  `ACHIEVED` header lines, and the contract that a delivered sample rate below the requested
  interval is reported rather than assumed.

## 1.0.0 — 2026-11-07

Initial packaging of the `amd-uprof` skill as a Claude Code plugin.

### Added

- `anchor` command: maps a workload's `QueryPerformanceCounter` reading onto capture-relative
  seconds, with the capture's UTC start where derivable. Negative values are attach latency.
- `ACHIEVED` header on `profile`: sample count, capture window, CPUs and achieved samples/s per
  CPU against the requested interval, with a warning below 10% of the ideal.

### Changed

- The dump sanity check no longer rejects good captures for per-CPU boundary gaps. It errors only
  on genuine inconsistency (core time exceeding the implied span, or a sample beyond it), reports
  the boundary gaps and the worst CPU, and distinguishes attached captures, whose coverage is
  limited to the CPUs the target ran on.
- `references/reference.md` documents the capture-clock derivation, the sanity semantics, the
  measured sampling cadence, and the limits of nearest-export attribution for symbol-less DLLs.

### Fixed

- Attached IBS captures were rejected by the sanity check with a ~28% mismatch because the
  denominator assumed every CPU was sampled for the target's whole lifetime.

