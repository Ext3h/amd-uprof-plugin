# Changelog

All notable changes to this plugin. Format: Keep a Changelog; versioning: SemVer.

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

