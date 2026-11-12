# Debug-symbol format fixtures

`fixture_debug.c` is the complete source of every binary here; there is no third-party code.
Each fixture isolates one debug-symbol format that the plugin may meet, so the tests in
`tests/test_debug_formats.py` can run the real LLVM tools and the production symbolization
paths against it. The fixtures are checked in so CI needs only the LLVM tools, not a compiler.

The source has two functions, each with a distinct line range:

| Source | Function | Lines |
|---|---|---|
| `fixture_debug.c` | `fixture_helper` | definition 2, return 3 |
| `fixture_debug.c` | `fixture_entry` | definition 6, call 7, return 8 |

## Coverage of issue #57

| Row | Format | Fixture | Test |
|---|---|---|---|
| 1 | stripped PE, exports and `.pdata` | `../fixture.exe` | `test_context_fixture.py`, `test_function_ranges.py` |
| 2 | PE COFF symbol table, no PDB | `pe/fixture_coff.exe` | `PeCoffSymbolTableTest` |
| 3 | PE CodeView, full PDB (lines, publics, types) | `pe/fixture_full.exe`, `pe/fixture_full.pdb` | `PeFullPdbTest` |
| 4 | PE CodeView, public-only PDB | `pe/fixture_public.exe`, `pe/fixture_public.pdb` | `PePublicOnlyPdbTest` |
| 5 | PE CodeView, mismatched PDB rejected | `pe/fixture_mismatch.exe`, `pe/fixture_other.pdb` | `PeMismatchedPdbTest` |
| 6 | PE with embedded DWARF | `pe/fixture_pe_dwarf.exe` | `PeDwarfTest` |
| 7 | ELF executable with DWARF | `elf/fixture_dwarf` | `ElfDwarfTest` |
| 8 | ELF shared object, DWARF and `.dynsym` | `elf/fixture_shared.so` | `ElfSharedObjectTest` |
| 9 | stripped ELF, separate `.debug` by `.gnu_debuglink` and build-id | `elf/fixture_debuglink`, `elf/fixture_buildid`, `elf/debug/buildid/...` | `ElfSeparateDebugTest` |
| 10 | ELF `.symtab`/`.dynsym` only, names without lines | `elf/fixture_symtab`, `elf/fixture_dynsym.so` | `ElfSymbolTableTest`, `ElfSharedObjectTest` |
| 11 | ELF with no symbols or debug info | `elf/fixture_nosym` | `ElfNoSymbolsTest` |
| 12 | compressed and split DWARF | `elf/fixture_compressed`, `elf/fixture_split` + `.dwo` | `CompressedAndSplitDwarfTest` |

The direct LLVM-tool tests run on Windows and Linux. Production PDB discovery is gated by
`os.name == "nt"` in `uprof_symbols.py`, so those tests carry an explicit Windows-only skip;
the Linux CI job reports them as skipped, not passed. Every ELF and DWARF production path runs
on both operating systems.

## Addresses and lines

| Fixture | `fixture_helper` | `fixture_entry` | line 7 address |
|---|---|---|---|
| `pe/fixture_full.exe`, `fixture_public.exe`, `fixture_pe_dwarf.exe` | `0x1000` (line 2) | `0x1010` (line 6) | `0x1018` |
| `elf/fixture_dwarf`, `fixture_symtab`, `fixture_nosym`, `fixture_debuglink`, `fixture_buildid` | `0x1370` | `0x1380` | `0x138E` |
| `elf/fixture_shared.so`, `fixture_dynsym.so` | `0x13D0` | `0x13E0` | `0x13EE` |
| `elf/fixture_compressed`, `fixture_split` | `0x1310` | `0x1320` | `0x132E` |

`fixture_nosym` has no function names; the test uses `0x1370`, the start of its `.text`.
`fixture_coff.exe` and `fixture_symtab`/`fixture_dynsym.so` resolve only exact function starts
and carry line 0, so the tests use the start addresses only.

## Building

The checked-in ELF fixtures were built with LLVM 23.1.4 and the PE fixtures with LLVM 22.1.6,
both on Windows. Run the commands from one neutral build directory that contains a copy of
`fixture_debug.c`; the recipes use relative source and output paths so no user or checkout path
enters the DWARF. `-fdebug-compilation-dir=/build`
makes CodeView and DWARF record `/build/fixture_debug.c`. The PE debug directory does embed the
absolute PDB path of whichever directory built it; only its basename is used at lookup time.

```console
mkdir pe elf
```

### PE: COFF symbol table, full PDB, public-only PDB, mismatched PDB

```console
clang --target=x86_64-pc-windows-msvc -c fixture_debug.c -o pe/coff.obj
lld-link /nodefaultlib /subsystem:console /entry:fixture_entry /release /debug:symtab /out:pe/fixture_coff.exe pe/coff.obj

clang --target=x86_64-pc-windows-msvc -g -gcodeview -fdebug-compilation-dir=/build -c fixture_debug.c -o pe/full.obj
lld-link /nodefaultlib /subsystem:console /entry:fixture_entry /release /debug /pdb:pe/fixture_full.pdb /out:pe/fixture_full.exe pe/full.obj

clang --target=x86_64-pc-windows-msvc -c fixture_debug.c -o pe/public.obj
lld-link /nodefaultlib /subsystem:console /entry:fixture_entry /release /debug /pdb:pe/fixture_public.pdb /out:pe/fixture_public.exe pe/public.obj

clang --target=x86_64-pc-windows-msvc -g -gcodeview -fdebug-compilation-dir=/other -c fixture_debug.c -o pe/other.obj
lld-link /nodefaultlib /subsystem:console /entry:fixture_entry /release /debug /pdb:pe/fixture_other.pdb /out:pe/fixture_other.exe pe/other.obj
```

Every PE link uses `/release`, so each image carries a nonzero `CheckSum` that build-identity
checks can compare with a capture's `Module.checksum`; `PeChecksumTest` asserts that every
checked-in PE image has the `CheckSum` its bytes compute to.

`fixture_coff.exe` has a COFF symbol table because of `/debug:symtab` and no PDB because there
is no `/debug` link. `fixture_public.exe` is compiled without `-g`, so its PDB has public
symbols but an empty TPI stream (`Has Types: true` with `Showing 0 records`).

`fixture_mismatch.exe` is linked against the `fixture_other.pdb` name but with the `/build`
compile, so the image and the adjacent PDB disagree. Linking writes a matching PDB over
`fixture_other.pdb`; restore the `/other` PDB afterwards:

```console
cp pe/fixture_other.pdb pe/other.keep.pdb
lld-link /nodefaultlib /subsystem:console /entry:fixture_entry /release /debug /pdb:pe/fixture_other.pdb /out:pe/fixture_mismatch.exe pe/full.obj
mv pe/other.keep.pdb pe/fixture_other.pdb
```

`lld-link` overwrites the PDB named by `/pdb:`; the restored file is the one built from the
`/other` compile directory, and the exe still names `fixture_other.pdb`, so the two sit beside
each other with different GUIDs. `llvm-symbolizer` may still load an adjacent PDB by name
without checking its GUID; the reject tests therefore assert the production probe evidence
(`PDB=none`, probe status `mismatch`, GUID detail), not empty symbolizer frames.

### PE with DWARF

```console
clang --target=x86_64-pc-windows-msvc -gdwarf -fdebug-compilation-dir=/build -c fixture_debug.c -o pe/dwarf.obj
lld-link /nodefaultlib /subsystem:console /entry:fixture_entry /release /debug:dwarf /out:pe/fixture_pe_dwarf.exe pe/dwarf.obj
```

### ELF with DWARF, a shared object, compressed and split DWARF

`-nostdlib` avoids a sysroot dependency; the fixture calls no library. `-Wl,-e,fixture_entry`
names the entry point because there is no `_start`. The link is a position-independent
executable (`ET_DYN`) with the default clang settings.

```console
clang --target=x86_64-linux-gnu -nostdlib -fuse-ld=lld -fdebug-compilation-dir=/build -g -Wl,--build-id=sha1 -Wl,-e,fixture_entry fixture_debug.c -o elf/fixture_dwarf

clang --target=x86_64-linux-gnu -nostdlib -fuse-ld=lld -fdebug-compilation-dir=/build -g -shared -fPIC -Wl,--build-id=sha1 -Wl,-soname,fixture_shared.so fixture_debug.c -o elf/fixture_shared.so

clang --target=x86_64-linux-gnu -nostdlib -fuse-ld=lld -fdebug-compilation-dir=/build -g -gz=zlib -Wl,-e,fixture_entry fixture_debug.c -o elf/fixture_compressed

clang --target=x86_64-linux-gnu -nostdlib -fuse-ld=lld -fdebug-compilation-dir=/build -g -gsplit-dwarf -Wl,-e,fixture_entry fixture_debug.c -o elf/fixture_split
```

The compressed and split fixtures omit the build-id note: they exercise compression and the
skeleton/dwo split, not separate-debug lookup.

### ELF derivatives

```console
cp elf/fixture_dwarf elf/fixture_symtab
llvm-strip --strip-debug elf/fixture_symtab

cp elf/fixture_dwarf elf/fixture_nosym
llvm-strip --strip-all elf/fixture_nosym

cp elf/fixture_shared.so elf/fixture_dynsym.so
llvm-strip --strip-all elf/fixture_dynsym.so
```

`--strip-debug` keeps `.symtab` (row 10); `--strip-all` leaves only an empty `.dynsym` in the
executable, which is why `fixture_nosym` has no named symbols but a `.dynsym` section.

### ELF separate debug files

The `.gnu_debuglink` fixture keeps its DWARF in a sidecar next to the stripped image:

```console
cp elf/fixture_dwarf elf/fixture_debuglink
llvm-objcopy --only-keep-debug elf/fixture_debuglink elf/fixture_debuglink.debug
cd elf
llvm-objcopy --strip-debug --add-gnu-debuglink=fixture_debuglink.debug fixture_debuglink
cd ..
```

The build-id fixture stores the DWARF under the build-id layout. `fixture_dwarf` and the
build-id-stripped image share the same build id because the build id hashes the loadable image,
not the debug sections; copying the checked-in `fixture_dwarf` is therefore the simplest
recipe:

```console
id=a07350ae6f157c05928a524a0d3e042c583f0a0d
mkdir -p elf/debug/buildid/.build-id/${id:0:2}
cp elf/fixture_dwarf elf/fixture_buildid
llvm-objcopy --only-keep-debug elf/fixture_buildid elf/debug/buildid/.build-id/${id:0:2}/${id:2}.debug
llvm-strip --strip-debug elf/fixture_buildid
printf '%s\n' "$id" > elf/build-id.txt
```

`elf/debug/buildid` is the search root a caller passes as a symbol directory. The tests also
exercise the `--fallback-debug-path` layout: for a binary moved to another directory, the
sidecar is found at `<fallback-root>/<binary-directory-without-its-root>/<debuglink-name>`.

## Rebuild effects

The ELF fixtures reproduce byte for byte with the commands above and LLVM 23.1.4 when the
commands run from the neutral build directory with forward slashes in `-o` paths (a backslash
in the split output path is recorded in the `.dwo` name). PE timestamps, and with them the
`CheckSum`, change on every link, and the PDB GUID depends on the absolute PDB output path, so
the tests read the image GUID and age from the checked-in image and compare the PDB against that
instead of hardcoding values.
The PE addresses and the DWARF line table are stable.

## Known limits recorded by the tests

- The tool does not derive ELF function ranges: `derive_module_ranges` returns `not_pe: ...`
  and the profile groups ELF modules as unresolved for `--by function`; `ElfDwarfTest` asserts
  the reason. An ELF module without DWARF keeps the uProf `Function.id`. This is an
  implementation limit, not a data limit; the research spike below shows LLVM exposes both
  `.eh_frame` FDE starts and symbol sizes.
- `no_debug_info` is a Windows-only evidence outcome: on Windows the PDB-discovery branch
  reports `readobj_failed`/`no_debug_info` when the image records no PDB. On Linux that branch
  is skipped, the symbolizer still produces empty frames, and the outcome remains `resolved`.
  `ElfNoSymbolsTest` asserts the empty frames, not an OS-specific outcome.
- `llvm-symbolizer` looks up `.gnu_debuglink` sidecars by the binary's directory and by the
  mirrored `--fallback-debug-path` layout, not by `--debug-file-directory`; build-id lookup
  uses `--debug-file-directory`. The separate-debug tests cover exactly those routes.
- A symbolizer loaded with a mismatched adjacent PDB can still produce frames from it; the
  production probe is what rejects the PDB (`PeMismatchedPdbTest`). The mismatch is in the GUID;
  the image and `fixture_other.pdb` happen to share age 1.
- The split fixture has a skeleton `.debug_info` and a separate `.dwo`; the tests assert the
  `.dwo` carries `.debug_info.dwo`/`.debug_str.dwo` and that the skeleton resolves the lines.
  No test forces the symbolizer to load the `.dwo`, because this source has no inlined functions.

## Research spikes for the ELF limits

The unsupported notes above describe the current tool, not a missing input format.

- Function boundaries without DWARF: `llvm-readobj --unwind elf/fixture_nosym` reports an
  `.eh_frame` with `fde_count: 2` and initial locations `0x1370` and `0x1380`, although the
  image has no function symbols. `llvm-readobj --symbols elf/fixture_symtab` reports a nonzero
  `Size` for `fixture_helper` and `fixture_entry`; `llvm-nm --print-size --format=posix` prints
  `fixture_helper T 1370 f` and `fixture_entry T 1380 21`. A future ELF range source could use
  FDE starts with the synthetic `sub_0x<start>` names the docs already allow, or symtab names
  and sizes. `derive_module_ranges` uses neither, so the tested behavior stays `not_pe`.
- Split DWARF: replacing the `.dwo` with a seven-byte corrupt file still resolves lines 2, 6
  and 7 from the skeleton. The checked-in `.dwo` has valid `.debug_info.dwo`/`.debug_str.dwo`
  sections, but this source has no inlined functions, so no test forces the symbolizer to load
  the `.dwo`.
- PDB on Linux: the format layer is not Windows-only. The traces and the cross-OS probe tests
  read the PDB fixtures with `llvm-pdbutil` and `llvm-symbolizer` on Linux; only production PDB
  discovery is gated by `os.name == "nt"`.
- `foundDebugInfo` is available from the session on both systems. Only the Windows branch maps
  `False` to `no_debug_info`; Linux keeps `resolved` with empty frames. That is a platform gate
  in the evidence mapping, not a format limit.

## Checking

```console
python tests/run_tests.py test_debug_formats
```

The tests need LLVM >= 23 built with zlib; CI installs the official LLVM 23.1.4 release. They
skip when one of `llvm-symbolizer`, `llvm-readobj`, `llvm-objdump` or `llvm-pdbutil` is
missing, the tests that pass a PDB skip when `llvm-symbolizer --help` lists no `--pdb` (LLVM
before 23), and the compressed-DWARF tests skip when the symbolizer was built without zlib. The
trace comparison skips in either case, because the goldens hold that toolchain's output. These
are tool skips, so `UPROF_REQUIRE_TOOLS=1` turns them into failures. The PDB production tests
additionally skip off Windows.
