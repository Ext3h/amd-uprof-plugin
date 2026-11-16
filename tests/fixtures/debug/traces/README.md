# Debug-format traces

Goldens for `tests/test_debug_traces.py`. Each file records one fixture from
`tests/capture_debug_traces.py`: the direct `llvm-readobj`, symbol, `llvm-objdump`,
`llvm-pdbutil` and `llvm-symbolizer` output, the production `symbolize_module` outcome and
probe evidence, and a `profile --by line` / `--by function` render over a synthetic session:
its `SYMBOL_COVERAGE` line, its stderr `WARNING:` lines and its rows.

Only `elf_fixture_dwarf` covers the session cache: its synthetic session alone records the image
size, checksum and bitness the cache needs, and each of its profile sections is followed by a
`cached` section from a second run of the same session. That section starts with a
`session_cache=` line: `hit` when the second run left the stored symbol run untouched, `miss`
when it stored one again, `none` when no cache file exists. The other sessions lack those
columns, so their runs warn that the session cache is disabled.

## Provenance

The committed files are the artifacts of
[capture-debug-traces.yml](../../../../.github/workflows/capture-debug-traces.yml)
[run 43836866209](https://github.com/Ext3h/amd-uprof-plugin/actions/runs/43836866209), byte for
byte on both OSes:

| directory | runner | Python | LLVM | DuckDB |
| --- | --- | --- | --- | --- |
| `windows/` | windows-latest | 3.12.10 | 23.1.4 (MSI) | 2.0.0 |
| `linux/` | ubuntu-latest | 3.12.16 | 23.1.4 (release tarball) | 2.0.0 |

The run captured branch commit `cb5e315`, which is not in main's history. Main has it rebased as
`5803eeb` (same subject, newer base), and its child `f23fce2` committed the traces.
Compared with run 43833713311, only the profile rows and `symbol_coverage=` lines of
`elf_fixture_buildid`, `elf_fixture_dynsym_so` and `elf_fixture_symtab` changed: an ELF
symbol-table function with a nonzero size is a source symbol again, so its rows are `unique`;
PE export and COFF names stay `derived`.

Earlier captures, at commits not in main's history:

- Run 43833713311 at `db7ff27` (on main as `5bf1fc0`, whose child `f305823` committed that run's
  traces) differed from run 43826808166 in the profile rows and `symbol_coverage=` lines of
  `elf_fixture_buildid`, `elf_fixture_dynsym_so`, `elf_fixture_symtab` and `pe_fixture_coff`,
  and on Linux of `pe_fixture_public`: rows named from the nearest symbol became `derived`.
- Run 43826808166 at `6f7e952` (on main as `8bf04c0`, whose child `b881ccc` committed that run's
  traces) differed from run 43826327821 at `578d371` (on main as `55f0a04`) only in the
  `profile.function` sections: a module with no derived range is named by the llvm-symbolizer
  containing function, so its rows are `unique` and its SYMBOL_COVERAGE line counts them as
  named.
- Run 43479468364 at `9775806` (same tree as `c0ecee2` on main, whose child `cd96b84` committed
  that run's traces).
- Run 43216136838 at `7c516a0` (on main as `c5a89bb`, whose child `1c21e5b` committed that
  run's traces) differed from run 43479468364 in the `symbol_coverage=` line of
  `elf_fixture_nosym.txt` and, on Windows, in the remedy of the `pe_fixture_mismatch.txt`
  WARNING lines.
- Run 43214725994 at `c3e42a7` (same tree as `f416e7d` on main) differed from run 43216136838
  only in the PDB GUIDs of the PE fixtures, because they were relinked with `/release`. Run
  43212399428, with apt.llvm.org 23.1.6 on Linux, produced the same files as run 43214725994.
- Run 43111099142 at `3b30ab6` (same golden files as `c392b9b` on main) had no
  `symbol_coverage=` or `WARNING:` lines and otherwise matched run 43214725994.

Run 43097467349 (tag `trace-capture-2026-11-11-3` at `2a03321`, same tree as `304d0b7` on main)
was queued during a GitHub Actions outage and never ran; run 43111099142 superseded it. Run
43090273610 captured the same Windows traces as run 43111099142 apart from the note line of
`pe_fixture_mismatch.txt`, but left the absolute embedded PDB path in the `debug pdb=` line on
Linux. Neither run may be used to refresh the goldens.

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

Name the run's commit as a branch commit, together with its subject. Main merges by rebase, so
that commit never reaches main with its hash and is not the parent of the commit that adds the
files; once merged, name its main counterpart and the commit that committed the traces. State
that the files equal the run's artifacts only after comparing them, and say what changed
against the previous run from a diff of the two runs' artifacts.
