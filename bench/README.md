# Maintainer benchmarks

This tree holds the timing harness used for maintainer measurements. It lives outside the
plugin source (`plugin/`), so it is not part of the installed plugin.

The harness is added by #40 (`bench_timing.py`) and adapted to this layout there. Measurement
snapshots belong in the MR/issue record, not in this repository; see the review policy in the
issue #19 and #42 records.

Run the harness with `--help` for its arguments.
