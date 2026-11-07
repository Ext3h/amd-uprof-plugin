# Instruction-context PE fixture

`fixture.exe` is a 2560-byte, stripped x86-64 PE kept in the repository so
`test_context_fixture.py` can run the real `llvm-readobj`, `llvm-objdump` and
`llvm-symbolizer` text formats end to end. It contains no third-party code: the
assembly and module definition in this directory are the complete source, and
they are distributed under the repository's license.

## Files

| File | Role |
|---|---|
| `fixture.s` | AT&T-syntax source; `fixture_entry` at RVA `0x1000`, hot `fixture_hot` at `0x101B` |
| `kernel32.def` | import module definition, `ExitProcess` only |
| `fixture.exe` | the linked fixture, stripped (`SymbolCount: 0`), two exports and `.pdata` |

## Properties the tests rely on

- `ImageBase` `0x140000000`, PE32+ (`Magic` `0x20B`), a nonzero `CheckSum`.
- `.text` at RVA `0x1000`, `IMAGE_SCN_CNT_CODE` and `IMAGE_SCN_MEM_EXECUTE`.
- Export `fixture_entry` at RVA `0x1000`; this is the anchor and the
  symbolizer's function name.
- Export `fixture_second` at RVA `0x1030`; `fixture_internal` at RVA `0x1050`
  has a `.pdata` runtime function but no export, so it exercises the `pdata`
  anchor kind.
- `.pdata` at RVA `0x3000` with runtime functions at `0x1000`, `0x1030` and
  `0x1050`.
- At RVA `0x101B`, `callq *0x107f(%rip)` targets IAT slot `0x20A0`, imported
  from `KERNEL32.dll` as `ExitProcess`.
- The next instruction starts at `0x1021`, so `0x101B` is a boundary and
  `0x1001` is not.

The tests read the checksum, timestamp and sizes from the file, so a rebuild
that changes them does not invalidate them.

## Building

Built with LLVM 23.1.4 (Windows). Run from this directory:

```console
clang --target=x86_64-pc-windows-msvc -c fixture.s -o fixture.obj
llvm-dlltool -m i386:x86-64 -d kernel32.def -l kernel32.lib
lld-link /nodefaultlib /subsystem:console /entry:fixture_entry /release /out:fixture.exe fixture.obj kernel32.lib
```

The `.seh_proc`/`.seh_endprologue`/`.seh_endproc` directives emit the
`.pdata` entries the anchor tests read. `/release` asks lld-link to write the
PE checksum; without it `CheckSum` stays zero and the block is verified only by
its recorded `SizeOfImage`. `fixture.obj` and `kernel32.lib` are build
intermediates and are not committed.

## Checking a rebuild

```console
python -m unittest test_context_fixture -v
```

The suite skips when the three LLVM tools are not on `PATH` or beside the
configured `LLVM_SYMBOLIZER`.
