# Debug-format traces

Goldens for `tests/test_debug_traces.py`. Each file records one fixture from
`tests/capture_debug_traces.py`: the direct `llvm-readobj`, symbol, `llvm-objdump`,
`llvm-pdbutil` and `llvm-symbolizer` output, the production `symbolize_module` outcome and
probe evidence, and a `profile --by line` / `--by function` render over a synthetic session.

## Provenance

The tool output was captured by
[capture-debug-traces.yml](../../../../.github/workflows/capture-debug-traces.yml) in
[run 43111099142](https://github.com/Ext3h/amd-uprof-plugin/actions/runs/43111099142) at
commit `3b30ab6`:

| directory | runner | Python | LLVM | DuckDB |
| --- | --- | --- | --- | --- |
| `windows/` | windows-latest | 3.12.10 | 23.1.4 (MSI) | 2.0.0 |
| `linux/` | ubuntu-latest | 3.12.16 | 23.1.6 (apt.llvm.org) | 2.0.0 |

Both jobs succeeded, and both downloaded artifacts are byte-identical to the committed files.
The capture commit is an ancestor of the final head; the only later change is this README.

Run 43097467349 (tag `trace-capture-2026-11-11-3` at `2a03321`) was queued during a GitHub
Actions outage and superseded by run 43111099142. Run 43090273610 captured the same Windows
traces but left the absolute embedded PDB path in the `debug pdb=` line on Linux; neither run
may be used to refresh the goldens.

## Normalization

- The fixture root is replaced with `<FIXTURES>` in both slash styles.
- An embedded PDB path is reduced to its basename, since the absolute build directory is local
  to whoever built the fixture.
- Tool locations, version banners, timestamps and durations are absent. Profile rows are
  sorted, and timing and share fields are dropped, so the traces carry format and resolution
  results only.
- Files are UTF-8 with LF endings. `read_text` applies universal newlines, so a Windows
  checkout with CRLF still compares equal.

## What the traces are not

The CI runners have no AMD PMU or IBS and the workflow never calls `AMDuProfCLI` for hardware
collection. The profile traces come from a synthetic session that names the fixture module and
canned RVAs; they are symbolization and pipeline evidence, not hardware profiling. Rows that
depend on real IBS or PMC capture are not runnable in CI and are not represented here.

## OS differences

The comparison is per OS; a Linux test never reads the Windows goldens, or the reverse. Known
differences are expected:

- On Windows, production symbolization enters the PDB discovery path even for ELF and ends
  with `outcome=readobj_failed`; on Linux that path is skipped and the outcome is `resolved`.
- Source paths are joined with the host separator by `llvm-symbolizer`.
- PDB acceptance, public folds and PDB-derived function ranges appear only in the Windows
  traces because production only looks for PDBs there.

## Refreshing

Push a `trace-capture-<date>` tag or run the workflow manually, download the artifacts with
`gh run download <run-id>`, copy `debug-traces-windows/windows/*.txt` to `windows/` and
`debug-traces-linux/linux/*.txt` to `linux/`, update the table above with the run ID and tool
versions, then run `python tests/run_tests.py test_debug_traces` on each OS.
