#!/usr/bin/env python
"""Capture the normalized debug-format traces used as CI goldens.

Run from the repository root:

    python tests/capture_debug_traces.py --out-dir build/debug-traces

The output holds one text file per fixture under <out-dir>/<os>/. The CI workflow
.github/workflows/capture-debug-traces.yml runs this on Windows and Linux and uploads the
result; tests/test_debug_traces.py runs the same capture and compares it with the committed
goldens. Nothing here touches an AMD PMU, IBS or AMDuProfCLI: the sessions are synthetic and
the traces are symbol/debug-format and production-pipeline evidence, not hardware profiling.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "plugin" / "skills" / "amd-uprof" / "tools"))
sys.path.insert(0, str(HERE))

import debug_fixtures as F
import uprof_symbols


@dataclass(frozen=True)
class Case:
    """One fixture and the search inputs and profile modes its trace covers."""

    name: str
    image: Path
    addresses: tuple[int, ...]
    pdb: Path | None = None
    direct_pdb: Path | None = None
    symbol_dirs: tuple[Path, ...] = ()
    symbol_files: tuple[Path, ...] = ()
    function_profile: bool = True
    notes: tuple[str, ...] = field(default=())


CASES = (
    Case(
        "pe_fixture_coff",
        F.PE / "fixture_coff.exe",
        (F.PE_HELPER, F.PE_ENTRY),
        notes=("COFF symbol table, no PDB record",),
    ),
    Case(
        "pe_fixture_full",
        F.PE / "fixture_full.exe",
        (F.PE_HELPER, F.PE_ENTRY, F.PE_ENTRY_BODY),
        pdb=F.PE / "fixture_full.pdb",
        direct_pdb=F.PE / "fixture_full.pdb",
    ),
    Case(
        "pe_fixture_public",
        F.PE / "fixture_public.exe",
        (F.PE_HELPER, F.PE_ENTRY),
        pdb=F.PE / "fixture_public.pdb",
        direct_pdb=F.PE / "fixture_public.pdb",
        notes=("public-only PDB: names and folds, no lines",),
    ),
    Case(
        "pe_fixture_mismatch",
        F.PE / "fixture_mismatch.exe",
        (F.PE_HELPER, F.PE_ENTRY, F.PE_ENTRY_BODY),
        direct_pdb=F.PE / "fixture_other.pdb",
        symbol_files=(F.PE / "fixture_other.pdb",),
        notes=(
            "adjacent PDB has a different GUID (same age); production rejects it",
            "the direct symbolizer may still load it, as the symbolizer section shows",
        ),
    ),
    Case(
        "pe_fixture_pe_dwarf",
        F.PE / "fixture_pe_dwarf.exe",
        (F.PE_HELPER, F.PE_ENTRY, F.PE_ENTRY_BODY),
        notes=("PE image carrying DWARF and no PDB record",),
    ),
    Case(
        "elf_fixture_dwarf", F.ELF / "fixture_dwarf", (F.ELF_HELPER, F.ELF_ENTRY, F.ELF_ENTRY_BODY)
    ),
    Case(
        "elf_fixture_shared_so",
        F.ELF / "fixture_shared.so",
        (F.ELF_SHARED_HELPER, F.ELF_SHARED_ENTRY, F.ELF_SHARED_BODY),
    ),
    Case(
        "elf_fixture_compressed",
        F.ELF / "fixture_compressed",
        (F.ELF_COMPRESSED_HELPER, F.ELF_COMPRESSED_ENTRY, F.ELF_COMPRESSED_BODY),
        notes=("compressed DWARF: SHF_COMPRESSED .debug_info and .debug_str",),
    ),
    Case(
        "elf_fixture_split",
        F.ELF / "fixture_split",
        (F.ELF_COMPRESSED_HELPER, F.ELF_COMPRESSED_ENTRY, F.ELF_COMPRESSED_BODY),
        notes=("split DWARF: the skeleton resolves lines without loading the .dwo",),
    ),
    Case(
        "elf_fixture_debuglink",
        F.ELF / "fixture_debuglink",
        (F.ELF_HELPER, F.ELF_ENTRY, F.ELF_ENTRY_BODY),
        notes=("stripped image; DWARF in the adjacent fixture_debuglink.debug",),
    ),
    Case(
        "elf_fixture_buildid",
        F.ELF / "fixture_buildid",
        (F.ELF_HELPER, F.ELF_ENTRY, F.ELF_ENTRY_BODY),
        symbol_dirs=(F.ELF / "debug" / "buildid",),
        notes=("stripped image; DWARF found under the build-id root",),
    ),
    Case(
        "elf_fixture_symtab",
        F.ELF / "fixture_symtab",
        (F.ELF_HELPER, F.ELF_ENTRY),
        notes=(".symtab only: names without lines",),
    ),
    Case(
        "elf_fixture_dynsym_so",
        F.ELF / "fixture_dynsym.so",
        (F.ELF_SHARED_HELPER, F.ELF_SHARED_ENTRY),
        notes=(".dynsym only: names without lines",),
    ),
    Case(
        "elf_fixture_nosym",
        F.ELF / "fixture_nosym",
        (F.ELF_HELPER,),
        notes=("no named symbols and no DWARF: empty frames",),
    ),
)


def run_tool(tool, *args) -> str:
    completed = F.run(tool, *args)
    if completed.returncode != 0:
        raise SystemExit(
            f"{Path(str(tool)).name} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def replace_spellings(text: str, paths, placeholder: str) -> str:
    """Replace each path in native and forward-slash spelling, either case, longest first."""
    spellings = set()
    for path in paths:
        spellings |= {str(path), path.as_posix(), str(path).lower(), path.as_posix().lower()}
    for spelling in sorted(spellings, key=len, reverse=True):
        text = text.replace(spelling, placeholder)
    return text


def normalize(text: str) -> str:
    """Replace checkout-specific fixture spellings; nothing else varies by machine."""
    return replace_spellings(text, (F.DEBUG,), "<FIXTURES>")


def readobj_section(case: Case) -> list[str]:
    pe = case.image.suffix.lower() in {".exe", ".dll"}
    options = (
        ["--file-headers", "--sections", "--symbols", "--coff-debug-directory"]
        if pe
        else ["--file-headers", "--sections", "--symbols", "--dyn-syms", "--notes"]
    )
    text = run_tool(F.READOBJ, *options, case.image)
    facts = ["## readobj"]
    for pattern, label in (
        (r"^Format: (.+)$", "format"),
        (r"^\s+Entry: (0x[0-9A-Fa-f]+)$", "entry"),
        (r"^\s+PointerToSymbolTable: (.+)$", "pointer_to_symbol_table"),
    ):
        match = re.search(pattern, text, re.M)
        if match:
            facts.append(f"{label}={match.group(1).strip()}")
    image_type = re.search(r"^\s+Type: (.+)$", text, re.M)
    if image_type:
        facts.append(("debug_type=" if pe else "type=") + image_type.group(1).strip())
    sections = []
    compressed = []
    current = None
    for line in text.splitlines():
        name = re.match(r"^\s+Name: (\S+) \(", line)
        if name:
            current = name.group(1)
            if current not in sections:
                sections.append(current)
        if current and "SHF_COMPRESSED" in line:
            compressed.append(current)
    if sections:
        facts.append("sections=" + ",".join(sections))
    if compressed:
        facts.append("compressed=" + ",".join(dict.fromkeys(compressed)))
    filename = re.search(r"^\s+PDBFileName: (.+)$", text, re.M)
    guid = re.search(r"^\s+PDBGUID: (.+)$", text, re.M)
    age = re.search(r"^\s+PDBAge: (.+)$", text, re.M)
    if filename or guid or age:
        name = F.embedded_basename(filename.group(1).strip()) if filename else "unknown"
        parts = [f"pdb={name}"]
        if guid:
            parts.append(f"guid={guid.group(1).strip()}")
        if age:
            parts.append(f"age={age.group(1).strip()}")
        facts.append("debug " + " ".join(parts))
    build_id = re.search(r"^\s+Build ID: (\S+)$", text, re.M)
    if build_id:
        facts.append(f"build_id={build_id.group(1)}")
    return facts


def symbol_section(case: Case) -> list[str]:
    pe = case.image.suffix.lower() in {".exe", ".dll"}
    options = ["--sections", "--symbols"] if pe else ["--sections", "--symbols", "--dyn-syms"]
    text = run_tool(F.READOBJ, *options, case.image)
    section_addresses = {}
    current_section = None
    for line in text.splitlines():
        name = re.match(r"^\s+Name: (\S+) \(", line)
        if name:
            current_section = name.group(1)
        address = re.match(r"^\s+VirtualAddress: (0x[0-9A-Fa-f]+)$", line)
        if current_section and address:
            section_addresses[current_section] = int(address.group(1), 16)
    symbols = []
    name = value = section = None
    for line in text.splitlines() + ["Symbol {"]:
        if line.strip() == "Symbol {":
            if name and name.startswith("fixture_") and value is not None:
                if pe:
                    value = section_addresses.get(section, 0) + value
                symbols.append((value, name))
            name = value = section = None
            continue
        match = re.match(r"^\s+Name: (.+?)(?: \(\d+\))?$", line)
        if match:
            name = match.group(1).strip()
            continue
        match = re.match(r"^\s+Value: (0x[0-9A-Fa-f]+|\d+)$", line)
        if match:
            value = int(match.group(1), 0)
            continue
        match = re.match(r"^\s+Section: (\S+)", line)
        if match:
            section = match.group(1)
    facts = ["## symbols"]
    for address, symbol_name in sorted(set(symbols)):
        facts.append(f"symbol {symbol_name} rva=0x{address:X}")
    if len(facts) == 1:
        facts.append("none")
    return facts


def objdump_section(case: Case) -> list[str]:
    text = run_tool(F.OBJDUMP, "--disassemble", "--no-show-raw-insn", case.image)
    return ["## objdump", *text.rstrip("\n").splitlines()]


def pdb_section(case: Case) -> list[str]:
    pdb = case.pdb or case.direct_pdb
    if pdb is None:
        return []
    summary = run_tool(F.PDBUTIL, "dump", "--summary", pdb)
    types = run_tool(F.PDBUTIL, "dump", "--types", pdb)
    headers = run_tool(F.PDBUTIL, "dump", "--section-headers", pdb)
    publics = run_tool(F.PDBUTIL, "dump", "--publics", pdb)
    guid = re.search(r"^\s+GUID: (.+)$", summary, re.M)
    age = re.search(r"^\s+Age: (.+)$", summary, re.M)
    has_types = re.search(r"^\s+Has Types: (.+)$", summary, re.M)
    has_publics = re.search(r"^\s+Has Publics: (.+)$", summary, re.M)
    type_count = re.search(r"Showing (\d+) records", types)
    section_addresses = {}
    current = None
    for line in headers.splitlines():
        match = re.search(r"SECTION HEADER #(\d+)", line)
        if match:
            current = int(match.group(1))
            continue
        match = re.match(r"^\s+(0x[0-9A-Fa-f]+|\d+) virtual address$", line)
        if current and match:
            section_addresses[current] = int(match.group(1), 16)
    facts = [
        "## pdbutil",
        f"pdb={pdb.name}"
        + (f" guid={guid.group(1).strip()}" if guid else "")
        + (f" age={age.group(1).strip()}" if age else "")
        + (f" has_types={has_types.group(1).strip()}" if has_types else "")
        + (f" has_publics={has_publics.group(1).strip()}" if has_publics else "")
        + (f" type_records={type_count.group(1)}" if type_count else ""),
    ]
    public_name = None
    for line in publics.splitlines():
        match = re.search(r"`([^`]+)`", line)
        if match and "S_PUB32" in line:
            public_name = match.group(1)
            continue
        match = re.search(r"addr = (\d+):(\d+)", line)
        if match and public_name:
            section, offset = int(match.group(1)), int(match.group(2), 10)
            address = section_addresses.get(section, 0) + offset
            facts.append(f"public {public_name} rva=0x{address:X}")
            public_name = None
    return facts


def frame_line(address: int, frame: dict) -> str:
    function = frame.get("FunctionName") or "<none>"
    file_name = frame.get("FileName") or "<no file>"
    line = frame.get("Line") or 0
    return f"0x{address:X} {function} {file_name}:{line}"


def symbolizer_section(case: Case) -> list[str]:
    records = F.symbolizer_records(
        case.image, case.addresses, pdb=case.direct_pdb, debug_dirs=case.symbol_dirs
    )
    facts = ["## symbolizer"]
    for record in records:
        address = int(record["Address"], 16)
        facts.append(frame_line(address, F.innermost(record)))
    return facts


def production_section(case: Case) -> list[str]:
    module, records, stream = F.production(
        case.image, case.addresses, symbol_dirs=case.symbol_dirs, symbol_files=case.symbol_files
    )
    facts = [
        "## production",
        f"pdb={Path(module.pdb).name if module.pdb else 'none'}",
        f"outcome={module.evidence.outcome}",
    ]
    if module.evidence.search:
        for probe in module.evidence.search.probes:
            if probe.status != "not_found":
                facts.append(f"probe {Path(probe.path).name} status={probe.status}")
    folds = re.search(r"FOLDS: (\d+ shared starts of \d+)", stream)
    if folds:
        facts.append(f"folds={folds.group(1)}")
    by_address = {int(record["Address"], 16): F.innermost(record) for record in records}
    for address in sorted(by_address):
        facts.append(frame_line(address, by_address[address]))
    return facts


def profile_section(case: Case, by: str) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="uprof-trace-session-") as root:
        session = F.write_session(root, case.image, list(case.addresses))
        document, stderr = F.profile_with_stderr(session, by)
        # The symbol-outcome evidence names the temporary session; only its role is stable.
        evidence = [f"symbol_coverage={document.get('SYMBOL_COVERAGE') or 'none'}"]
        evidence += [
            line
            for line in F.without_elevation_warning(stderr).splitlines()
            if line.startswith("WARNING:")
        ]
        evidence = [
            replace_spellings(line, (Path(root), Path(root).resolve()), "<SESSION_ROOT>")
            for line in evidence
        ]
    facts = [f"## profile.{by}", *evidence]
    rows = document.get("rows") or []
    if by == "line":
        rows = sorted(
            rows,
            key=lambda row: (
                row.get("function") or "",
                row.get("file") or "",
                row.get("line") or 0,
            ),
        )
        for row in rows:
            facts.append(
                f"line module={Path(row.get('module') or '').name}"
                f" function={row.get('function') or '<no symbol>'}"
                f" file={row.get('file') or '<no source line>'}"
                f" line={row.get('line') or 0}"
                f" samples={row.get('samples', 0)}"
                f" attribution={row.get('attribution') or 'unknown'}"
            )
    else:
        rows = sorted(
            rows, key=lambda row: (row.get("function") or "", row.get("function_start") or "")
        )
        for row in rows:
            facts.append(
                f"function module={Path(row.get('module') or '').name}"
                f" function={row.get('function') or '<no symbol>'}"
                f" start={row.get('function_start') or 'none'}"
                f" end={row.get('function_end') or 'none'}"
                f" source={row.get('function_source') or 'none'}"
                f" samples={row.get('samples', 0)}"
                f" attribution={row.get('attribution') or 'unknown'}"
            )
    return facts


def fallback_case() -> tuple[str, list[str]]:
    """Production lookup of a moved debuglink binary through the mirrored fallback root."""
    with tempfile.TemporaryDirectory(prefix="uprof-trace-fallback-") as root:
        moved = Path(root) / "fixture_debuglink"
        shutil.copyfile(F.ELF / "fixture_debuglink", moved)
        fallback = Path(root) / "fallback"
        target = fallback / F.relative_path_for_fallback(moved.parent) / "fixture_debuglink.debug"
        target.parent.mkdir(parents=True)
        shutil.copyfile(F.ELF / "fixture_debuglink.debug", target)
        module, records, stream = F.production(moved, [F.ELF_ENTRY], symbol_dirs=[fallback])
    facts = [
        "# fixture: elf/fixture_debuglink (moved to another directory)",
        "# production fallback-root layout",
        "## production",
        f"pdb={Path(module.pdb).name if module.pdb else 'none'}",
        f"outcome={module.evidence.outcome}",
    ]
    by_address = {int(record["Address"], 16): F.innermost(record) for record in records}
    for address in sorted(by_address):
        facts.append(frame_line(address, by_address[address]))
    return "elf_fixture_debuglink_fallback", facts


def render(case: Case) -> str:
    lines = [f"# fixture: {case.image.relative_to(F.DEBUG).as_posix()}"]
    lines += [f"# {note}" for note in case.notes]
    lines += readobj_section(case)
    lines += symbol_section(case)
    lines += objdump_section(case)
    lines += pdb_section(case)
    lines += symbolizer_section(case)
    lines += production_section(case)
    lines += profile_section(case, "line")
    if case.function_profile:
        lines += profile_section(case, "function")
    return normalize("\n".join(lines)) + "\n"


def manifest() -> list[str]:
    import platform

    lines = [f"os={platform.system().lower()}", f"python={platform.python_version()}"]
    for name, path in (
        ("llvm-symbolizer", F.SYMBOLIZER),
        ("llvm-readobj", F.READOBJ),
        ("llvm-objdump", F.OBJDUMP),
        ("llvm-pdbutil", F.PDBUTIL),
    ):
        if path:
            version = F.run(path, "--version").stdout.splitlines()[:2]
            lines.append(f"{name}={path}")
            lines += [f"  {line}" for line in version]
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir", required=True, help="directory that receives <os>/ and manifest.txt"
    )
    args = parser.parse_args(argv)
    if os.environ.get("UPROF_REQUIRE_TOOLS") == "1":
        missing = F.tool_skip() or F.duckdb_skip()
        if missing:
            raise SystemExit(f"UPROF_REQUIRE_TOOLS=1: {missing}")
    os_name = "windows" if os.name == "nt" else "linux"
    out_dir = Path(args.out_dir).resolve()
    target = out_dir / os_name
    target.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        (target / f"{case.name}.txt").write_text(render(case), encoding="utf-8", newline="\n")
    name, lines = fallback_case()
    (target / f"{name}.txt").write_text(
        normalize("\n".join(lines)) + "\n", encoding="utf-8", newline="\n"
    )
    (out_dir / "manifest.txt").write_text(
        "\n".join(manifest()) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {len(CASES) + 1} traces to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
