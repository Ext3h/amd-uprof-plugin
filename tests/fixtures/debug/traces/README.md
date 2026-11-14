# Debug-format traces

Goldens for `tests/test_debug_traces.py`. Each file records one fixture from
`tests/capture_debug_traces.py`: the direct `llvm-readobj`, symbol, `llvm-objdump`,
`llvm-pdbutil` and `llvm-symbolizer` output, the production `symbolize_module` outcome and
probe evidence, and a `profile --by line` / `--by function` render over a synthetic session:
its `SYMBOL_COVERAGE` line, its stderr `WARNING:` lines and its rows.

## Provenance

The tool output was captured by
[capture-debug-traces.yml](../../../../.github/workflows/capture-debug-traces.yml) in
[run 43479468364](https://github.com/Ext3h/amd-uprof-plugin/actions/runs/43479468364) at
commit `9775806`:

| directory | runner | Python | LLVM | DuckDB |
| --- | --- | --- | --- | --- |
| `windows/` | windows-latest | 3.12.10 | 23.1.4 (MSI) | 2.0.0 |
| `linux/` | ubuntu-latest | 3.12.16 | 23.1.4 (release tarball) | 2.0.0 |

Both jobs succeeded, and both downloaded artifacts are byte-identical to the committed files.
Compared with run 43216136838 at `7c516a0`, only the `symbol_coverage=` line of
`elf_fixture_nosym.txt` changed: line views name both symbolizer channels whenever
llvm-symbolizer ran.
The capture commit is the parent of the commit that adds these files.
Run 43216136838 differed from run 43214725994 at `c3e42a7` only in the PDB GUIDs of the PE fixtures,
because they were relinked with `/release`.
Run 43212399428, with apt.llvm.org 23.1.6 on Linux, produced the same files. The previous
capture, run 43111099142 at `3b30ab6`, had no `symbol_coverage=` or `WARNING:` lines and
otherwise matches this one.

Run 43097467349 (tag `trace-capture-2026-11-11-3` at `2a03321`) was queued during a GitHub
Actions outage and superseded by run 43111099142. Run 43090273610 captured the same Windows
traces but left the absolute embedded PDB path in the `debug pdb=` line on Linux; neither run
may be used to refresh the goldens.

## Normalization

- The fixture root is replaced with `<FIXTURES>` in both slash styles.
- The temporary root of the synthetic session is replaced with `<SESSION_ROOT>`.
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

- On Windows, production symbolization enters the PDB discovery path even for ELF; an image
  that records no PDB is `resolved` when DWARF or its symbol table names an address, and
  `outcome=no_pdb_record` otherwise. On Linux that path is skipped.
- Source paths are joined with the host separator by `llvm-symbolizer`.
- PDB acceptance, public folds and PDB-derived function ranges appear only in the Windows
  traces because production only looks for PDBs there.

## Refreshing

Push a `trace-capture-<date>` tag or run the workflow manually, download the artifacts with
`gh run download <run-id>`, copy `debug-traces-windows/windows/*.txt` to `windows/` and
`debug-traces-linux/linux/*.txt` to `linux/`, update the table above with the run ID and tool
versions, then run `python tests/run_tests.py test_debug_traces` on each OS.
