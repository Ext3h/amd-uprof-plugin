"""Instruction-scope context: safe LLVM anchors, disassembly windows and their annotations.

LLVM decides instruction boundaries, names and import destinations. The tool only joins sample
data. When LLVM cannot give a safe answer the context is omitted with a machine-readable reason
rather than guessed.

The first cut covers the instruction granularity for PE images only (COFF); an ELF module is
reported as unsupported rather than disassembled with different rules.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from uprof_native import find_sibling, find_symbolizer, run, tool_operand
from uprof_symbols import resolve_image
from uprof_util import UprofError, inline, literal_path, one_line, tail_lines

ANCHOR_DISTANCE_CAP: Final = 1 << 20
WINDOW_BEFORE: Final = 32
WINDOW_AFTER: Final = 32
CONTEXT_ROW_CAP: Final = 20
CONTEXT_TOOL_TIMEOUT: Final = 5 * 60
IBS_FIELDS: Final = (
    "DataSrc",
    "CacheHitSt",
    "IbsDcMiss",
    "IbsOpBrnMisp",
    "IbsDcLockedOp",
    "IbsOpMemWidth",
    "IbsTagToRetCtr",
)
IBS_DISTRIBUTIONS: Final = frozenset({"DataSrc", "CacheHitSt", "IbsOpMemWidth"})
IBS_FLAGS: Final = frozenset({"IbsDcMiss", "IbsOpBrnMisp", "IbsDcLockedOp"})
IBS_LATENCY: Final = "IbsTagToRetCtr"
IBS_TAG_MAX: Final = 65535
_IBS_TAG_INT: Final = r"""regexp_full_match("IbsTagToRetCtr", '\s*[0-9]+\s*')"""

_IMAGE_BASE = re.compile(r"^\s*ImageBase:\s*(0x[0-9A-Fa-f]+)\s*$")
_CHECKSUM = re.compile(r"^\s*CheckSum:\s*(0x[0-9A-Fa-f]+)\s*$")
_SIZE_OF_IMAGE = re.compile(r"^\s*SizeOfImage:\s*(\d+)\s*$")
_MAGIC = re.compile(r"^\s*Magic:\s*(0x[0-9A-Fa-f]+)\s*$")
_DATE_STAMP = re.compile(r"^\s*TimeDateStamp:.*\((0x[0-9A-Fa-f]+)\)\s*$")
_UNWIND_START = re.compile(r"^\s*StartAddress:\s*\(?(0x[0-9A-Fa-f]+)\)?\s*$")
_EXPORT_BLOCK = re.compile(r"^\s*Export\s*\{\s*$")
_EXPORT_RVA = re.compile(r"^\s*RVA:\s*(0x[0-9A-Fa-f]+)\s*$")
_EXPORT_NAME = re.compile(r"^\s*Name:\s*(.*?)\s*$")
_SECTION_BLOCK = re.compile(r"^\s*Section\s*\{\s*$")
_SECTION_NAME = re.compile(r"^\s*Name:\s*(\S+)(?:\s+\(([0-9A-Fa-f ]+)\))?\s*$")
_SECTION_VIRTUAL_SIZE = re.compile(r"^\s*VirtualSize:\s*(0x[0-9A-Fa-f]+)\s*$")
_SECTION_VIRTUAL_ADDRESS = re.compile(r"^\s*VirtualAddress:\s*(0x[0-9A-Fa-f]+)\s*$")
_SECTION_RAW_SIZE = re.compile(r"^\s*RawDataSize:\s*(\d+)\s*$")
_OBJDUMP_LINE = re.compile(r"^\s*([0-9A-Fa-f]+):\s+(.*)$")
_RAW_BYTES = re.compile(r"^(?:[0-9A-Fa-f]{2}\s+)+")
_IMPORT_BLOCK = re.compile(r"^\s*Import\s*\{\s*$")
_DELAY_IMPORT_BLOCK = re.compile(r"^\s*DelayImport\s*\{\s*$")
_IMPORT_END = re.compile(r"^\s*\}\s*$")
_IMPORT_DLL = re.compile(r"^\s*Name:\s*(\S+)\s*$")
_IMPORT_IAT_RVA = re.compile(r"^\s*ImportAddressTableRVA:\s*(0x[0-9A-Fa-f]+)\s*$")
_IMPORT_SYMBOL = re.compile(r"^\s*Symbol:\s*(\([^)]*\)|\S+)(?:\s+\([^)]*\))?\s*$")
_RIP_OPERAND = re.compile(r"^\*(?:(-?0x[0-9A-Fa-f]+))?\(%rip\)$")
_ABSOLUTE_OPERAND = re.compile(r"^\*(-?0x[0-9A-Fa-f]+)$")
_CALL_MNEMONICS: Final = frozenset({"call", "callq", "calll", "callw"})


class ContextOmission(Exception):
    """A context that cannot be produced safely; reason is a stable machine-readable code."""

    def __init__(
        self, reason: str, detail: str | None = None, build: dict[str, object] | None = None
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.build = build

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"omitted": True, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        if self.build is not None:
            payload["build"] = self.build
        return payload


@dataclass(frozen=True)
class PeSection:
    """One PE section in RVA space; code marks CNT_CODE or MEM_EXECUTE."""

    start: int
    size: int
    code: bool

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass(frozen=True)
class BuildIdentity:
    """PE header identity used to confirm the module matches the capture's build.

    size_of_image is 0 when the headers did not carry a parseable SizeOfImage.
    """

    image_base: int
    time_date_stamp: int
    checksum: int
    magic: int
    size_of_image: int = 0

    @property
    def slot_width(self) -> int:
        """IAT slot width: 8 bytes for PE32+, 4 for PE32."""
        return 8 if self.magic == 0x20B else 4


@dataclass(frozen=True)
class VerifiedBuild:
    """The capture's build evidence and the strongest on-disk identity that supports it.

    identity is "checksum", "size" or "unverified"; only the first two set verified. An
    unverified block is still emitted, with warning explaining which evidence was missing or
    disagreed.
    """

    pe: BuildIdentity
    capture_checksum: int
    verified: bool = True
    identity: str = "checksum"
    capture_size: int | None = None
    warning: str | None = None

    @property
    def checksum(self) -> int:
        return self.pe.checksum

    @property
    def image_base(self) -> int:
        return self.pe.image_base

    @property
    def time_date_stamp(self) -> int:
        return self.pe.time_date_stamp

    @property
    def size_of_image(self) -> int:
        return self.pe.size_of_image


@dataclass(frozen=True)
class Anchor:
    """The instruction boundary a window is decoded forward from."""

    kind: str  # "export", "pdb_public", "pdata" or "section"
    rva: int
    name: str | None = None


@dataclass
class DecodedInstruction:
    """One instruction from llvm-objdump; rva is module-relative and text is mnemonic+operands."""

    rva: int
    text: str
    next_rva: int | None = None


@dataclass(frozen=True)
class ImportSlot:
    """One IAT entry: the slot's RVA, its DLL and the imported symbol name."""

    slot_rva: int
    dll: str
    name: str


@dataclass(frozen=True)
class SymbolInfo:
    """One address's innermost symbolizer frame; file and line stay None when absent."""

    function: str | None
    file: str | None
    line: int | None


def _as_int(value: object) -> int | None:
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return None


def _append_section(sections: list[PeSection], values: dict[str, object]) -> None:
    start = values.get("start")
    if start is None:
        return
    size = values.get("virtual_size") or values.get("raw_size") or 0
    sections.append(
        PeSection(start=int(start), size=int(size), code=bool(values.get("code", False)))
    )


@dataclass(frozen=True)
class ParsedImage:
    """The pieces of one llvm-readobj dump that boundary derivation needs.

    sections keeps the dump's order; unwind_starts and exports hold one entry per start and
    export in dump order.
    """

    build: BuildIdentity
    sections: list[PeSection]
    unwind_starts: list[tuple[int, str | None]]
    exports: list[tuple[int, str | None]]
    # The debug directory records a CodeView PDB (PDBGUID), which llvm-symbolizer loads unchecked
    # unless --pdb names one.
    records_pdb: bool = False


def parse_image(text: str) -> ParsedImage:
    """File headers, sections, .pdata starts, exports and the CodeView PDB record from one
    llvm-readobj text.

    Raises ContextOmission("metadata_incomplete") when the file headers lack ImageBase,
    TimeDateStamp, CheckSum or Magic.
    """
    image_base = checksum = magic = date_stamp = size_of_image = None
    sections: list[PeSection] = []
    unwind_addresses: set[int] = set()
    exports: list[tuple[int, str | None]] = []
    current: dict[str, object] = {}
    in_section = in_export = False
    rva: int | None = None
    name: str | None = None
    records_pdb = False
    for line in text.splitlines():
        bare = line.strip()
        # The base parser scans StartAddress globally, so record it before the block state can
        # consume the line.
        if match := _UNWIND_START.match(line):
            unwind_addresses.add(int(match.group(1), 16))
        if in_section:
            if bare == "}":
                _append_section(sections, current)
                current = {}
                in_section = False
            elif bare.startswith("Name:"):
                if match := _SECTION_NAME.match(line):
                    current["name"] = match.group(1)
            elif bare.startswith("VirtualSize:"):
                if match := _SECTION_VIRTUAL_SIZE.match(line):
                    current["virtual_size"] = int(match.group(1), 16)
            elif bare.startswith("VirtualAddress:"):
                if match := _SECTION_VIRTUAL_ADDRESS.match(line):
                    current["start"] = int(match.group(1), 16)
            elif bare.startswith("RawDataSize:"):
                if match := _SECTION_RAW_SIZE.match(line):
                    current["raw_size"] = int(match.group(1), 10)
            elif "IMAGE_SCN_CNT_CODE" in bare or "IMAGE_SCN_MEM_EXECUTE" in bare:
                current["code"] = True
            continue
        if in_export:
            if bare == "}":
                if rva is not None:
                    exports.append((rva, name))
                in_export = False
            elif rva is None and bare.startswith("RVA:"):
                if match := _EXPORT_RVA.match(line):
                    rva = int(match.group(1), 16)
            elif name is None and bare.startswith("Name:") and (match := _EXPORT_NAME.match(line)):
                name = match.group(1) or None
            continue
        if bare.startswith("Section") and _SECTION_BLOCK.match(line):
            current, in_section = {}, True
        elif _EXPORT_BLOCK.match(line):
            in_export, rva, name = True, None, None
        elif bare.startswith("ImageBase:"):
            if image_base is None and (match := _IMAGE_BASE.match(line)):
                image_base = int(match.group(1), 16)
        elif bare.startswith("CheckSum:"):
            if checksum is None and (match := _CHECKSUM.match(line)):
                checksum = int(match.group(1), 16)
        elif bare.startswith("SizeOfImage:"):
            if size_of_image is None and (match := _SIZE_OF_IMAGE.match(line)):
                size_of_image = int(match.group(1), 10)
        elif bare.startswith("Magic:"):
            if magic is None and (match := _MAGIC.match(line)):
                magic = int(match.group(1), 16)
        elif (
            bare.startswith("TimeDateStamp:")
            and date_stamp is None
            and (match := _DATE_STAMP.match(line))
        ):
            date_stamp = int(match.group(1), 16)
        elif bare.startswith("PDBGUID:"):
            records_pdb = True
    missing = [
        name
        for name, value in (
            ("ImageBase", image_base),
            ("TimeDateStamp", date_stamp),
            ("CheckSum", checksum),
            ("Magic", magic),
        )
        if value is None
    ]
    if missing:
        raise ContextOmission(
            "metadata_incomplete", f"llvm-readobj file headers lack {', '.join(missing)}"
        )
    unwind_starts = [
        (address - image_base, None)
        for address in sorted(unwind_addresses)
        if address - image_base >= 0
    ]
    return ParsedImage(
        BuildIdentity(
            image_base=image_base,
            time_date_stamp=date_stamp,
            checksum=checksum,
            magic=magic,
            size_of_image=size_of_image or 0,
        ),
        sections,
        unwind_starts,
        exports,
        records_pdb,
    )


def find_code_section(sections: list[PeSection], rva: int) -> PeSection | None:
    """The executable section containing rva, or None."""
    for section in sections:
        if section.code and section.start <= rva < section.end:
            return section
    return None


def require_code_section(sections: list[PeSection], rva: int) -> PeSection:
    """The executable section containing rva; omits the context when there is none."""
    section = find_code_section(sections, rva)
    if section is None:
        raise ContextOmission(
            "no_code_section", f"0x{rva:X} is not inside an executable PE section"
        )
    return section


_ANCHOR_PRIORITY: Final = {"export": 0, "pdb_public": 1, "pdata": 2}


def select_anchor(
    hot_rva: int,
    sections: list[PeSection],
    public_starts: list[tuple[int, str | None]],
    pdata_starts: Sequence[tuple[int, str | None]] = (),
    export_starts: Sequence[tuple[int, str | None]] = (),
    cap: int = ANCHOR_DISTANCE_CAP,
) -> Anchor:
    """Nearest safe start at or below hot_rva inside its code section, else the section start.

    Safe starts come from the PE export table, PDB publics and .pdata runtime functions. The
    nearest one wins; at the same RVA an export is preferred, then a PDB public, then a .pdata
    start, so the block reports the most specific name available. The anchor kind names the
    source that supplied the start.
    """
    section = require_code_section(sections, hot_rva)
    sources = (("export", export_starts), ("pdb_public", public_starts), ("pdata", pdata_starts))
    by_rva: dict[int, tuple[int, str, str | None]] = {}
    for kind, starts in sources:
        for rva, name in starts:
            if section.start <= rva <= hot_rva:
                current = by_rva.get(rva)
                if current is None or _ANCHOR_PRIORITY[kind] < current[0]:
                    by_rva[rva] = (_ANCHOR_PRIORITY[kind], kind, name)
    if by_rva:
        rva = max(by_rva)
        _, kind, name = by_rva[rva]
        if hot_rva - rva <= cap:
            return Anchor(kind, rva, name)
    if hot_rva - section.start <= cap:
        return Anchor("section", section.start, None)
    raise ContextOmission(
        "anchor_too_far",
        f"0x{hot_rva - section.start:X} bytes from the nearest safe start exceeds the "
        f"{cap}-byte cap",
    )


def verify_build(
    build: BuildIdentity, capture_checksum: object, capture_size: object = None
) -> VerifiedBuild:
    """Compare the on-disk build with the checksum and SizeOfImage the capture recorded.

    A nonzero checksum pair that disagrees is evidence of a different build and omits. When a
    checksum is unavailable, an equal recorded SizeOfImage verifies the build. Without any
    matching evidence the block is still emitted, marked unverified, so ordinary MSVC modules
    with PE CheckSum 0 keep their context; the warning names what was missing or disagreed.
    """
    recorded = _as_int(capture_checksum)
    recorded_size = _as_int(capture_size)
    if recorded and build.checksum:
        if recorded != build.checksum:
            raise ContextOmission(
                "build_mismatch",
                f"capture checksum 0x{recorded:X} differs from the on-disk module checksum "
                f"0x{build.checksum:X}",
            )
        return VerifiedBuild(
            pe=build, capture_checksum=recorded, identity="checksum", capture_size=recorded_size
        )
    warning = "no usable checksum or SizeOfImage evidence; build identity is unverified"
    if recorded_size and build.size_of_image:
        if recorded_size == build.size_of_image:
            return VerifiedBuild(
                pe=build,
                capture_checksum=recorded or 0,
                identity="size",
                capture_size=recorded_size,
            )
        warning = (
            f"capture SizeOfImage 0x{recorded_size:X} differs from the on-disk "
            f"SizeOfImage 0x{build.size_of_image:X}; build identity is unverified"
        )
    return VerifiedBuild(
        pe=build,
        capture_checksum=recorded or 0,
        verified=False,
        identity="unverified",
        capture_size=recorded_size,
        warning=warning,
    )


# ------------------------------------------------------------------ imports


def parse_imports(text: str, build: BuildIdentity) -> dict[int, ImportSlot]:
    """Map IAT slot RVAs to imported names from `llvm-readobj --coff-imports` text.

    Each block's ImportAddressTableRVA is authoritative; symbols are laid out from it in slot
    order at the image's PE32+/PE32 slot width. Delay imports are not IAT slots.
    """
    imports: dict[int, ImportSlot] = {}
    dll: str | None = None
    iat_rva: int | None = None
    index = 0
    in_import = False
    for line in text.splitlines():
        if _DELAY_IMPORT_BLOCK.match(line):
            in_import = False
            continue
        if _IMPORT_BLOCK.match(line):
            in_import = True
            dll = None
            iat_rva = None
            index = 0
            continue
        if not in_import:
            continue
        if _IMPORT_END.match(line):
            in_import = False
            continue
        if dll is None and (match := _IMPORT_DLL.match(line)):
            dll = match.group(1)
        elif iat_rva is None and (match := _IMPORT_IAT_RVA.match(line)):
            iat_rva = int(match.group(1), 16)
        elif (match := _IMPORT_SYMBOL.match(line)) and dll and iat_rva is not None:
            slot_rva = iat_rva + index * build.slot_width
            imports[slot_rva] = ImportSlot(slot_rva, dll, _import_name(match.group(1)))
            index += 1
    return imports


def _import_name(token: str) -> str:
    """A symbol name; an ordinal-only import (`Symbol: (17)`) becomes `#17`."""
    if not (token.startswith("(") and token.endswith(")")):
        return token
    ordinal = token[1:-1].strip()
    if ordinal.lower().startswith("ordinal "):
        ordinal = ordinal[len("ordinal ") :].strip()
    return f"#{ordinal}"


def resolve_iat_call(
    instruction: DecodedInstruction, image_base: int, imports: dict[int, ImportSlot]
) -> ImportSlot | None:
    """The imported slot an indirect call targets, or None when the target is not a slot.

    A call through a register or to an address outside the import table stays unresolved; only
    the RIP-relative and absolute memory operands name an IAT slot.
    """
    parts = instruction.text.split(None, 1)
    if len(parts) != 2 or parts[0] not in _CALL_MNEMONICS:
        return None
    operand = parts[1].strip()
    if match := _RIP_OPERAND.match(operand):
        if instruction.next_rva is None:
            return None
        displacement = int(match.group(1), 0) if match.group(1) else 0
        return imports.get(instruction.next_rva + displacement)
    if match := _ABSOLUTE_OPERAND.match(operand):
        return imports.get(int(match.group(1), 0) - image_base)
    return None


def find_readobj(symbolizer: object | None = None) -> str | None:
    """llvm-readobj beside llvm-symbolizer, else on PATH; None when unavailable."""
    found = find_sibling(symbolizer if symbolizer else find_symbolizer(), "llvm-readobj")
    return str(found) if found else None


def load_imports(
    module_path: str, build: BuildIdentity, readobj: str | None = None
) -> dict[int, ImportSlot]:
    """Read the module's IAT slots; a readobj failure is an omission, not an empty table."""
    tool = readobj or find_readobj()
    if not tool:
        raise ContextOmission(
            "readobj_missing", "llvm-readobj not found beside llvm-symbolizer or on PATH"
        )
    completed = run(
        [tool, "--coff-imports", tool_operand(module_path)], timeout=CONTEXT_TOOL_TIMEOUT
    )
    if completed.returncode != 0:
        raise ContextOmission(
            "imports_failed", tail_lines((completed.stdout or "") + "\n" + (completed.stderr or ""))
        )
    return parse_imports(completed.stdout or "", build)


# ------------------------------------------------------------------ symbolizer names


def parse_symbolizer_json(text: str) -> dict[int, SymbolInfo]:
    """Innermost executed frame per module-relative address from llvm-symbolizer JSON lines."""
    symbols: dict[int, SymbolInfo] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        address = _as_int(record.get("Address"))
        frames = record.get("Symbol")
        if address is None or not isinstance(frames, list) or not frames:
            continue
        frame = frames[0]
        if not isinstance(frame, dict):
            continue
        function = frame.get("FunctionName") or None
        file = frame.get("FileName") or None
        line_number = _as_int(frame.get("Line")) or None
        if function or file or line_number is not None:
            symbols[address] = SymbolInfo(function, file, line_number)
    return symbols


def _symbolizer_error(text: str) -> str | None:
    """The first llvm-symbolizer JSON Error.Message on stdout, if any."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        error = record.get("Error")
        message = error.get("Message") if isinstance(error, dict) else error
        if isinstance(message, str) and message:
            return message
    return None


def _empty_symbol_records(text: str) -> tuple[int, int]:
    """(records with a Symbol result, records whose frames carry no name and line 0)."""
    records = empty = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or "Symbol" not in record:
            continue
        records += 1
        frames = record.get("Symbol")
        if isinstance(frames, list) and all(
            not (frame.get("FunctionName") or "").strip() and not (frame.get("Line") or 0)
            for frame in frames
            if isinstance(frame, dict)
        ):
            empty += 1
    return records, empty


def symbolize_rvas(
    module_path: str,
    rvas: list[int],
    pdb: str | Path | None = None,
    symbolizer: str | None = None,
    debug_dirs: Iterable[str] = (),
    fallback_paths: Iterable[str] = (),
) -> dict[int, SymbolInfo]:
    """Symbolize module-relative addresses; a tool failure is an omission, not a guessed name.

    debug_dirs and fallback_paths are passed to llvm-symbolizer as --debug-file-directory and
    --fallback-debug-path for DWARF/debug files.
    """
    addresses = sorted({int(rva) for rva in rvas})
    if not addresses:
        return {}
    found = symbolizer if symbolizer else find_symbolizer()
    if not found:
        raise ContextOmission("symbolizer_missing", "llvm-symbolizer not found")
    # --no-debuginfod: profile never contacts a symbol URL, even when DEBUGINFOD_URLS is set.
    command = [str(found), "--no-debuginfod", f"--obj={module_path}"]
    if pdb:
        command.append(f"--pdb={pdb}")
    command += [f"--debug-file-directory={directory}" for directory in debug_dirs]
    command += [f"--fallback-debug-path={directory}" for directory in fallback_paths]
    command += ["--relative-address", "--functions=linkage", "--inlines", "--output-style=JSON"]
    input_text = "\n".join(f"0x{rva:X}" for rva in addresses) + "\n"
    completed = run(command, input_text=input_text, timeout=CONTEXT_TOOL_TIMEOUT)
    if completed.returncode != 0:
        # JSON output carries the resolution error on stdout, so it is the primary evidence.
        detail = _symbolizer_error(completed.stdout or "")
        if not detail:
            detail = tail_lines((completed.stdout or "") + "\n" + (completed.stderr or ""))
        message = f"llvm-symbolizer exited {completed.returncode}: {detail or 'no output'}"
        print(
            f"WARNING: {inline(module_path)}: {one_line(message)}; affected instructions "
            "carry no symbol",
            file=sys.stderr,
        )
        raise ContextOmission("symbolizer_failed", message)
    symbols = parse_symbolizer_json(completed.stdout or "")
    records, empty_records = _empty_symbol_records(completed.stdout or "")
    if pdb and not symbols and records and empty_records == records:
        # A valid PDB for another build yields exit 0, no Error object and empty Symbol frames.
        message = (
            f"llvm-symbolizer resolved no address ({records} empty Symbol entries); "
            f"the PDB {Path(pdb).name} is probably for another build"
        )
        print(f"WARNING: {inline(module_path)}: {one_line(message)}", file=sys.stderr)
        raise ContextOmission("symbolizer_failed", message)
    return symbols


# ------------------------------------------------------------------ IBS hardware labels


def present_ibs_fields(columns: Iterable[str]) -> tuple[str, ...]:
    """The whitelisted IBS fields the dump carries, in a fixed order."""
    present = set(columns)
    return tuple(field for field in IBS_FIELDS if field in present)


def ibs_expression(field: str) -> list[str]:
    """SELECT expressions for one field: a value histogram, a nonzero count or tag statistics.

    The field name itself is the label; nothing maps the hardware value to a semantic role.
    """
    if field in IBS_DISTRIBUTIONS:
        return [f'histogram("{field}") AS "{field}"']
    if field in IBS_FLAGS:
        return [f'count(*) FILTER (WHERE TRY_CAST("{field}" AS BIGINT) <> 0) AS "{field}"']
    if field == IBS_LATENCY:
        clipped = (
            f'CASE WHEN {_IBS_TAG_INT} THEN LEAST(TRY_CAST("{field}" AS BIGINT), {IBS_TAG_MAX}) END'
        )
        return [
            f'count(*) FILTER (WHERE {_IBS_TAG_INT}) AS "{field}_samples"',
            f'sum({clipped}) AS "{field}_sum"',
            f'max({clipped}) AS "{field}_max"',
        ]
    raise ValueError(f"not a whitelisted IBS field: {field}")


def _code_key(value: str) -> tuple[int, int, str]:
    try:
        return (0, int(value, 0), value)
    except ValueError:
        return (1, 0, value)


def ibs_summary(record: dict[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    """Per-RVA IBS labels keyed by field name; fields the capture lacks are omitted."""
    samples = int(record.get("samples") or 0)
    summary: dict[str, object] = {"samples": samples}
    for key in fields:
        if key in IBS_DISTRIBUTIONS:
            counts = record.get(key)
            if isinstance(counts, dict) and counts:
                summary[key] = {
                    str(code): int(count)
                    for code, count in sorted(
                        counts.items(), key=lambda item: _code_key(str(item[0]))
                    )
                }
        elif key in IBS_FLAGS:
            summary[key] = {"set": int(record.get(key) or 0), "of": samples}
        elif key == IBS_LATENCY:
            observed = int(record.get(f"{key}_samples") or 0)
            if observed:
                summary[key] = {
                    "samples": observed,
                    "sum": int(record.get(f"{key}_sum") or 0),
                    "max": int(record.get(f"{key}_max") or 0),
                }
    return summary


def ibs_rows(connection: object, module_name: str, rvas: list[int]) -> dict[int, dict[str, object]]:
    """Aggregate the dump's IBS fields per requested RVA; empty when there is no IBS dump."""
    addresses = sorted({int(rva) for rva in rvas})
    if not addresses:
        return {}
    dump = connection.execute("SELECT getvariable('uprof_ibs_dump')").fetchone()
    if not dump or not dump[0]:
        return {}
    pattern = literal_path(str(dump[0]))
    columns = {
        str(row[0])
        for row in connection.execute(
            "DESCRIBE SELECT * FROM read_csv(?, header = true, all_varchar = true)", [pattern]
        ).fetchall()
    }
    fields = present_ibs_fields(columns)
    if not fields:
        return {}
    expressions = [expression for field in fields for expression in ibs_expression(field)]
    # The rows counted here must be the ones v_scoped kept: same PID, image and time window.
    sql = (
        'SELECT CAST(d."Offset" AS BIGINT) AS rva, count(*) AS samples, '
        + ", ".join(expressions)
        + " FROM read_csv(?, header = true, all_varchar = true)"
        ' AS d LEFT JOIN Process AS p ON p.id = TRY_CAST(d."PID" AS INTEGER)'
        ' WHERE module_key(d."ModuleName") = CAST(? AS VARCHAR)'
        ' AND TRY_CAST(d."Offset" AS BIGINT) IN (SELECT * FROM unnest(?))'
        " AND TRY_CAST(d.\"DeltaTick\" AS BIGINT) >= getvariable('time_start')"
        " AND TRY_CAST(d.\"DeltaTick\" AS BIGINT) < getvariable('time_end')"
        " AND (getvariable('uprof_pid') = ''"
        '      OR TRY_CAST(d."PID" AS INTEGER)'
        "         = CAST(getvariable('uprof_pid') AS INTEGER))"
        " AND image_matches(p.name, getvariable('uprof_image'))"
        " GROUP BY rva"
    )
    rows = connection.execute(sql, [pattern, module_name, addresses]).fetchall()
    names = [str(column[0]) for column in connection.description]
    return {int(row[0]): ibs_summary(dict(zip(names, row)), fields) for row in rows}


# ------------------------------------------------------------------ disassembly window


def find_objdump(symbolizer: object | None = None) -> str | None:
    """llvm-objdump beside llvm-symbolizer, else on PATH; None when unavailable."""
    found = find_sibling(symbolizer if symbolizer else find_symbolizer(), "llvm-objdump")
    return str(found) if found else None


def parse_objdump(text: str, image_base: int) -> list[DecodedInstruction]:
    """Parse `llvm-objdump --no-show-raw-insn --no-symbolize-operands` output into RVAs.

    The nearest-symbol labels objdump prints are never read: only the address and the instruction
    text after it are used, and a trailing comment is discarded. next_rva is the following decoded
    address, which is what RIP-relative arithmetic needs.
    """
    decoded: list[DecodedInstruction] = []
    for line in text.splitlines():
        match = _OBJDUMP_LINE.match(line)
        if not match:
            continue
        address = int(match.group(1), 16)
        remainder = match.group(2)
        if address < image_base:
            continue
        instruction = remainder.split("#", 1)[0]
        instruction = _RAW_BYTES.sub("", instruction).strip()
        if not instruction:
            continue
        decoded.append(DecodedInstruction(address - image_base, instruction))
    for current, following in zip(decoded, decoded[1:]):
        current.next_rva = following.rva
    return decoded


def select_window(
    decoded: list[DecodedInstruction],
    hot_rva: int,
    before: int = WINDOW_BEFORE,
    after: int = WINDOW_AFTER,
) -> list[DecodedInstruction]:
    """The last <=before instructions and <=after instructions around hot_rva.

    A hot RVA that is not an instruction start in the decoded stream has no safe window; it is
    rejected rather than snapped to the preceding boundary.
    """
    for index, instruction in enumerate(decoded):
        if instruction.rva == hot_rva:
            return decoded[max(0, index - before) : index + after + 1]
    raise ContextOmission(
        "hot_rva_not_instruction_start", f"0x{hot_rva:X} was not reached as an instruction boundary"
    )


def instruction_share(samples: int, total_samples: int) -> float | None:
    """sample_share: a fraction of the scoped samples, the same denominator as the row."""
    if not total_samples:
        return None
    return samples / total_samples


def decode_stream(
    module_path: str,
    build: BuildIdentity,
    anchor: Anchor,
    last_rva: int,
    section_end: int,
    objdump: str | None = None,
) -> list[DecodedInstruction]:
    """Decode forward from anchor far enough to window last_rva, clamped to the section end.

    The stop address leaves enough room for 32 instructions after last_rva plus one more, so the
    last window instruction still has a known length for RIP-relative call resolution.
    """
    tool = objdump or find_objdump()
    if not tool:
        raise ContextOmission(
            "objdump_missing", "llvm-objdump not found beside llvm-symbolizer or on PATH"
        )
    start_vma = build.image_base + anchor.rva
    stop_rva = min(last_rva + (WINDOW_AFTER + 2) * 16, section_end)
    stop_vma = build.image_base + stop_rva
    if stop_vma <= start_vma:
        stop_vma = start_vma + 16
    command = [
        tool,
        "-d",
        f"--start-address=0x{start_vma:X}",
        f"--stop-address=0x{stop_vma:X}",
        "--no-show-raw-insn",
        "--no-symbolize-operands",
        tool_operand(module_path),
    ]
    completed = run(command, timeout=CONTEXT_TOOL_TIMEOUT)
    if completed.returncode != 0:
        detail = tail_lines((completed.stdout or "") + "\n" + (completed.stderr or ""))
        message = f"llvm-objdump exited {completed.returncode}: {detail or 'no output'}"
        if not (completed.stdout or "").strip():
            raise ContextOmission("objdump_failed", message)
        print(
            f"WARNING: {inline(module_path)}: {one_line(message)}; using the instructions "
            "it printed",
            file=sys.stderr,
        )
    decoded = parse_objdump(completed.stdout or "", build.image_base)
    if not decoded:
        raise ContextOmission(
            "objdump_failed", f"llvm-objdump produced no instructions for 0x{anchor.rva:X}"
        )
    return decoded


# ------------------------------------------------------------------ JSON context block


def _hex(value: int) -> str:
    return f"0x{value:X}"


def build_json(build: VerifiedBuild) -> dict[str, object]:
    """The build-identity object shared by an emitted context and a later omission."""
    payload: dict[str, object] = {
        "verified": bool(build.verified),
        "identity": build.identity,
        "image_base": _hex(build.image_base),
        "time_date_stamp": _hex(build.time_date_stamp),
        "checksum": _hex(build.checksum),
        "capture_checksum": _hex(build.capture_checksum),
    }
    if build.size_of_image:
        payload["size_of_image"] = _hex(build.size_of_image)
    if build.capture_size is not None:
        payload["capture_size"] = _hex(build.capture_size)
    if build.warning is not None:
        payload["warning"] = build.warning
    return payload


def build_context(
    anchor: Anchor,
    build: VerifiedBuild,
    hot_rva: int,
    instructions: list[DecodedInstruction],
    samples: dict[int, int],
    total_samples: int,
    symbols: dict[int, SymbolInfo],
    imports: dict[int, ImportSlot],
    ibs: dict[int, dict[str, object]],
) -> dict[str, object]:
    """The JSON block for one instruction row; instructions are ordered by RVA."""
    anchor_json: dict[str, object] = {"kind": anchor.kind, "rva": _hex(anchor.rva)}
    if anchor.name is not None:
        anchor_json["name"] = anchor.name
    anchor_json["distance"] = hot_rva - anchor.rva
    framed: list[tuple[tuple[str | None, str | None, int | None], dict[str, object]]] = []
    for instruction in sorted(instructions, key=lambda item: item.rva):
        count = int(samples.get(instruction.rva, 0))
        entry: dict[str, object] = {
            "rva": _hex(instruction.rva),
            "instruction": instruction.text,
            "samples": count,
            "sample_share": instruction_share(count, total_samples),
        }
        symbol = symbols.get(instruction.rva)
        frame = (
            (symbol.function, symbol.file, symbol.line)
            if symbol is not None
            else (None, None, None)
        )
        slot = resolve_iat_call(instruction, build.image_base, imports)
        if slot is not None:
            entry["import"] = {"dll": slot.dll, "name": slot.name, "slot_rva": _hex(slot.slot_rva)}
        ibs_labels = ibs.get(instruction.rva)
        if ibs_labels:
            entry["ibs"] = ibs_labels
        framed.append((frame, entry))
    context: dict[str, object] = {"anchor": anchor_json, "build": build_json(build)}
    context.update(_context_source(frame for frame, _entry in framed))
    context["runs"] = _source_runs(framed, context)
    return context


def _context_source(frames) -> dict[str, object]:
    """The function and file most instructions of the window share (first seen on a tie)."""
    counts: dict[tuple[str | None, str | None], int] = {}
    for function, file, _line in frames:
        if function is not None or file is not None:
            counts[(function, file)] = counts.get((function, file), 0) + 1
    if not counts:
        return {}
    function, file = max(counts, key=counts.get)
    source: dict[str, object] = {}
    if function is not None:
        source["function"] = function
    if file is not None:
        source["file"] = file
    return source


def _source_runs(framed, context: dict[str, object]) -> list[dict[str, object]]:
    """Consecutive instructions with the same frame 0, each run naming what differs."""
    runs: list[dict[str, object]] = []
    previous = None
    for frame, entry in framed:
        if frame != previous:
            function, file, line = frame
            run: dict[str, object] = {}
            if function != context.get("function"):
                run["function"] = function
            if file != context.get("file"):
                run["file"] = file
            run["line"] = line
            run["instructions"] = []
            runs.append(run)
            previous = frame
        runs[-1]["instructions"].append(entry)
    return runs


_CONTEXT_HELP_FIELDS: Final = {
    "anchor": (
        "the instruction boundary decoding starts from: kind is export, pdb_public, "
        "pdata or section; rva is module-relative; name is present when the source names "
        "it; distance is the byte distance from the anchor to the row's rva"
    ),
    "build": (
        "identity of the disassembled file: verified is true only with matching checksum "
        "or size evidence, false means unverified; identity is checksum, size or "
        "unverified; image_base, time_date_stamp, checksum, capture_checksum, "
        "size_of_image and capture_size are hex; size_of_image, capture_size and warning "
        "are present when known"
    ),
    "function, file": (
        "llvm-symbolizer frame 0 shared by most instructions of the window; "
        "absent when no instruction has one"
    ),
    "runs": (
        "the static decoded stream in ascending rva, grouped into runs of consecutive "
        "instructions with the same frame 0; each run has line and instructions, and "
        "function or file only where the run's frame differs from the context; null means "
        "the frame has none, and a run with function, file and line null has no frame"
    ),
    "runs[].instructions": (
        "each entry has rva, instruction (assembly), samples (raw count), "
        "sample_share (fraction) and optional import and ibs; instruction "
        "may include llvm-objdump's nearest-symbol <...> decoration, which "
        "is not the frame-0 symbol, so use the run's function for the real "
        "frame"
    ),
    "runs[].instructions[].import": (
        "the IAT target of a call through a slot: dll, name and "
        "slot_rva; absent for register or non-IAT targets"
    ),
    "runs[].instructions[].ibs": (
        "IBS OP aggregates for the same PID, image and time window as the "
        "row's samples: DataSrc, CacheHitSt and IbsOpMemWidth are value "
        "histograms; IbsDcMiss, IbsOpBrnMisp and IbsDcLockedOp are nonzero "
        "counts as {set, of}; IbsTagToRetCtr is {samples, sum, max} over "
        "unsigned decimal integer cells, each clipped at 65535, the 16-bit "
        "field maximum, with NULL, negative, fractional and exponent cells "
        "excluded; values are AMD-encoded hardware fields (counts or "
        "histograms), not decoded meanings; fields the dump lacks are omitted"
    ),
    "omitted": (
        "an omitted context has omitted=true, reason, optional detail and, when the "
        "module was examined, the same build object; see omission_reasons"
    ),
}
_CONTEXT_HELP_OMISSIONS: Final = {
    "module_missing": (
        "the capture records no module path, or the file is absent on this host; "
        "remedy: copy the module to the recorded path or capture on this host"
    ),
    "readobj_missing": (
        "llvm-readobj is unavailable; remedy: install LLVM so llvm-readobj sits "
        "beside llvm-symbolizer or on PATH"
    ),
    "readobj_failed": (
        "llvm-readobj failed to read the PE headers, sections, exports or unwind "
        "data; remedy: check the module file is readable and the LLVM version "
        "supports it"
    ),
    "metadata_incomplete": (
        "the PE headers lack ImageBase, TimeDateStamp, CheckSum or Magic "
        "(for example an ELF module); remedy: use a PE/COFF module, ELF "
        "modules are not supported for this block"
    ),
    "no_code_section": (
        "the row's rva is not inside an executable PE section; remedy: use the "
        "module/RVA row without a context unless the address is known code"
    ),
    "anchor_too_far": (
        "the nearest safe start is beyond the 1 MiB distance cap; remedy: read "
        "the module/RVA row without a context"
    ),
    "hot_rva_not_instruction_start": (
        "decoding from the anchor never reaches the row's rva on "
        "an instruction boundary; remedy: use the module/RVA row; "
        "do not snap the address by hand"
    ),
    "objdump_missing": (
        "llvm-objdump is unavailable; remedy: install LLVM so llvm-objdump sits "
        "beside llvm-symbolizer or on PATH"
    ),
    "objdump_failed": (
        "llvm-objdump failed or produced no instructions; remedy: check the module "
        "file and the LLVM installation; the table row remains valid"
    ),
    "symbolizer_missing": (
        "llvm-symbolizer is unavailable; remedy: install LLVM so "
        "llvm-symbolizer sits beside the other LLVM tools or on PATH"
    ),
    "symbolizer_failed": (
        "llvm-symbolizer exited nonzero or resolved no address and the detail "
        "names its exit status, JSON Error and any stderr; remedy: pass profile "
        "--symbol-path <file|dir> for a symbol or debug file or a search root, "
        "or --image-path <file|dir> when the module image is not at its "
        "recorded path; llvm-symbolizer ignores _NT_SYMBOL_PATH, so only an "
        "adjacent PDB, the recorded CodeView path or the skill's local srv* "
        "cache scan is searched"
    ),
    "pdb_unverified": (
        "the image records a PDB, but symbolization accepted none whose GUID "
        "and age match; remedy: pass profile --symbol-path <file|dir> naming "
        "the captured build's PDB"
    ),
    "imports_failed": (
        "llvm-readobj could not read the import table; remedy: check the module "
        "file is readable; the block is omitted rather than showing calls "
        "without IAT targets"
    ),
    "build_mismatch": (
        "a nonzero on-disk CheckSum differs from the capture's Module.checksum; "
        "remedy: symbolicate against the exact binary recorded in the capture"
    ),
    "ambiguous_module_version": (
        "the capture maps the module name to more than one image "
        "version, so no single Module row identifies the build to "
        "verify; remedy: read the module/RVA row without a context"
    ),
    "context_failed": (
        "an LLVM tool timed out or a scoped query failed while building this "
        "context; remedy: retry, then check the LLVM tools and the artifact "
        "directory"
    ),
    "row_cap": (
        "this row is past the 20-row context cap; remedy: inspect it in a separate run "
        "with a smaller --top"
    ),
}


def context_help(legend: dict[str, Any]) -> dict[str, Any]:
    """Once-per-document help for the context blocks; JSON only, in a fixed key order.

    The share unit and denominator come from the document's legend rather than a second scale,
    so the context cannot drift from the table's sample_share meaning.
    """
    fields = dict(_CONTEXT_HELP_FIELDS)
    columns = legend.get("columns") or {}
    if "attribution" in columns:
        fields["row.attribution"] = columns["attribution"]
    read = (
        "The instruction entry whose rva equals the row's rva is the sampled instruction. "
        "sample_share is the raw share for this capture mode: read its meaning from "
        "legend.columns.sample_share and its fraction unit and denominator from "
        "legend.share_unit, legend.share_scale and legend.denominators.sample_share; it is "
        "not a per-op cost. The table orders rows by legend.impact.order_by, the mode's "
        "primary measure: when that measure is sample_share the first row is the sample-hot "
        "instruction, and when it is time_share the table is not a sample-hot ranking. "
        "Imports and symbol frames identify the work kind."
    )
    if "tag_to_retire_share" in columns:
        read += (
            " The row's tag_to_retire_share and the instruction ibs.IbsTagToRetCtr values "
            "are total tag-to-retire dwell evidence, not per-op latency."
        )
    return {
        "scope": (
            "one context per instruction row; the 20-row cap counts row slots in the "
            "table's row order, so a row that omits for another reason still consumes a "
            "slot; rows past the cap carry omitted with reason row_cap; the <other "
            "instructions> row carries no context"
        ),
        "share": (
            "sample_share is a fraction; legend.share_unit, legend.share_scale and "
            "legend.denominators.sample_share define its unit and denominator, and "
            "legend.columns.sample_share gives its meaning for this capture mode"
        ),
        "fields": fields,
        "omission_reasons": dict(_CONTEXT_HELP_OMISSIONS),
        "read": read,
        "next": (
            "Use legend.next to change scope or granularity. Rank by "
            "legend.impact.order_by; a high sample_share with a low time_share and no IBS "
            "tag-to-retire evidence is frequency, not an impact finding."
        ),
    }


@dataclass
class _ModuleState:
    """Per-module work reused by every row that names the same module."""

    path: str
    build: VerifiedBuild
    sections: list[PeSection]
    imports: dict[int, ImportSlot]
    public_starts: list[tuple[int, str | None]]
    pdata_starts: list[tuple[int, str | None]]
    export_starts: list[tuple[int, str | None]]
    pdb: str | None
    records_pdb: bool
    symbols: dict[int, SymbolInfo] = field(default_factory=dict)
    symbol_attempted: set[int] = field(default_factory=set)
    ibs: dict[int, dict[str, object]] = field(default_factory=dict)
    ibs_attempted: set[int] = field(default_factory=set)


def _module_row(
    connection: object, module_name: str, checksum_column: bool, size_column: bool = False
) -> tuple[object, object, object] | None:
    """The capture's path, checksum and SizeOfImage, all read from the same Module row."""
    return connection.execute(
        f"SELECT path, {'checksum' if checksum_column else 'NULL'}, "
        f"{'size' if size_column else 'NULL'} "
        "FROM Module WHERE module_key(path) = CAST(? AS VARCHAR) "
        "ORDER BY path DESC NULLS LAST LIMIT 1",
        [module_name],
    ).fetchone()


class _ContextBuilder:
    def __init__(
        self,
        connection: object,
        total_samples: int,
        debug_dirs: Iterable[str] = (),
        fallback_paths: Iterable[str] = (),
        image_files: Iterable[str | Path] = (),
        image_dirs: Iterable[str | Path] = (),
        ambiguous_modules: Iterable[str] = (),
    ) -> None:
        self.connection = connection
        self.ambiguous_modules = frozenset(ambiguous_modules)
        self.total_samples = total_samples
        self.debug_dirs = tuple(debug_dirs)
        self.fallback_paths = tuple(fallback_paths)
        self.image_files = tuple(image_files)
        self.image_dirs = tuple(image_dirs)
        self.modules: dict[str, _ModuleState | ContextOmission] = {}

    def _module(self, module_name: str) -> _ModuleState:
        cached = self.modules.get(module_name)
        if isinstance(cached, ContextOmission):
            raise ContextOmission(cached.reason, cached.detail, cached.build)
        if cached is not None:
            return cached
        try:
            state = self._load_module(module_name)
        except ContextOmission as exc:
            self.modules[module_name] = exc
            raise
        self.modules[module_name] = state
        return state

    def _load_module(self, module_name: str) -> _ModuleState:
        if module_name in self.ambiguous_modules:
            # Any one Module row would verify or reject the build by row order, not evidence.
            raise ContextOmission(
                "ambiguous_module_version",
                f"the capture records more than one image version of {module_name!r}",
            )
        columns = {
            str(name).lower()
            for (name,) in self.connection.execute(
                "SELECT lower(column_name) FROM duckdb_columns() WHERE lower(table_name) = 'module'"
            ).fetchall()
        }
        row = _module_row(self.connection, module_name, "checksum" in columns, "size" in columns)
        path = row[0] if row else None
        capture_checksum = row[1] if row else None
        capture_size = row[2] if row else None
        if not path:
            resolved = resolve_image(module_name, None, self.image_files, self.image_dirs)[0]
            if resolved is None:
                raise ContextOmission(
                    "module_missing", f"the capture records no path for {module_name!r}"
                )
            module_path = str(resolved)
        else:
            module_path = str(path)
            resolved = resolve_image(module_name, module_path, self.image_files, self.image_dirs)[0]
            if resolved is None:
                raise ContextOmission("module_missing", f"{module_path} is not on this host")
            module_path = str(resolved)
        readobj = find_readobj()
        if not readobj:
            raise ContextOmission(
                "readobj_missing", "llvm-readobj not found beside llvm-symbolizer or on PATH"
            )
        completed = run(
            [
                readobj,
                "--file-headers",
                "--sections",
                "--unwind",
                "--coff-exports",
                "--coff-debug-directory",
                tool_operand(module_path),
            ],
            timeout=CONTEXT_TOOL_TIMEOUT,
        )
        if completed.returncode != 0:
            raise ContextOmission(
                "readobj_failed",
                tail_lines((completed.stdout or "") + "\n" + (completed.stderr or "")),
            )
        text = completed.stdout or ""
        image = parse_image(text)
        build = image.build
        sections = image.sections
        try:
            verified = verify_build(build, capture_checksum, capture_size)
        except ContextOmission as exc:
            # The mismatch detail is the evidence; attach the same build shape the emitted
            # block would carry so the omitted row is not silent about the build.
            exc.build = build_json(
                VerifiedBuild(
                    pe=build,
                    capture_checksum=_as_int(capture_checksum) or 0,
                    verified=False,
                    identity="unverified",
                    capture_size=_as_int(capture_size),
                    warning=exc.detail,
                )
            )
            raise
        try:
            imports = load_imports(module_path, build, readobj)
        except ContextOmission as exc:
            # The import table is part of the block's evidence; omitting only it would make a
            # call through an IAT slot look like a call without a destination.
            exc.build = build_json(verified)
            raise
        public_starts = [
            (int(rva), names)
            for rva, names in self.connection.execute(
                "SELECT rva, names FROM v_public_starts WHERE module_name = CAST(? AS VARCHAR)",
                [module_name],
            ).fetchall()
        ]
        pdb_row = self.connection.execute(
            "SELECT pdb FROM v_pdbs WHERE module_name = CAST(? AS VARCHAR)", [module_name]
        ).fetchone()
        pdb = pdb_row[0] if pdb_row else None
        return _ModuleState(
            module_path,
            verified,
            sections,
            imports,
            public_starts,
            image.unwind_starts,
            image.exports,
            str(pdb) if pdb else None,
            image.records_pdb,
        )

    def _samples(self, module_name: str, rvas: list[int]) -> dict[int, int]:
        if not rvas:
            return {}
        rows = self.connection.execute(
            "SELECT rva, CAST(sum(samples) AS BIGINT) FROM v_scoped "
            "WHERE module_name = CAST(? AS VARCHAR) "
            "AND rva IN (SELECT * FROM unnest(?)) GROUP BY rva",
            [module_name, sorted({int(rva) for rva in rvas})],
        ).fetchall()
        return {int(rva): int(count or 0) for rva, count in rows}

    def _symbols(self, state: _ModuleState, rvas: list[int]) -> dict[int, SymbolInfo]:
        if state.records_pdb and state.pdb is None:
            # Without --pdb, llvm-symbolizer loads the PDB the CodeView entry names, or the one
            # beside the image, with no GUID check; symbolization accepted none of them.
            raise ContextOmission(
                "pdb_unverified",
                f"{state.path} records a PDB, but symbolization accepted none whose GUID and "
                "age match the image",
            )
        missing = [rva for rva in rvas if rva not in state.symbol_attempted]
        if missing:
            state.symbol_attempted.update(missing)
            state.symbols.update(
                symbolize_rvas(
                    state.path,
                    missing,
                    pdb=state.pdb,
                    debug_dirs=self.debug_dirs,
                    fallback_paths=self.fallback_paths,
                )
            )
        return state.symbols

    def _ibs(
        self, state: _ModuleState, module_name: str, rvas: list[int]
    ) -> dict[int, dict[str, object]]:
        missing = [rva for rva in rvas if rva not in state.ibs_attempted]
        if missing:
            state.ibs_attempted.update(missing)
            state.ibs.update(ibs_rows(self.connection, module_name, missing))
        return state.ibs


def _build_module_contexts(
    builder: _ContextBuilder,
    contexts: list[object | None],
    module_name: str,
    items: list[tuple[int, int, Anchor, int]],
) -> None:
    """Decode once per anchor and annotate every row of one module in batches.

    items holds (row index, hot RVA, anchor, section end) for rows that already selected an
    anchor; contexts receives one entry per row index.
    """
    state = builder._module(module_name)
    groups: dict[tuple[Anchor, int], list[tuple[int, int]]] = {}
    for index, hot_rva, anchor, section_end in items:
        groups.setdefault((anchor, section_end), []).append((index, hot_rva))
    windows: dict[int, tuple[int, Anchor, list[DecodedInstruction]]] = {}
    for (anchor, section_end), members in groups.items():
        last_rva = max(hot_rva for _, hot_rva in members)
        try:
            stream = decode_stream(state.path, state.build, anchor, last_rva, section_end)
        except ContextOmission as exc:
            exc.build = build_json(state.build)
            for index, _ in members:
                contexts[index] = exc.as_json()
            continue
        except UprofError as exc:
            omission = ContextOmission(
                "context_failed", str(exc), build=build_json(state.build)
            ).as_json()
            for index, _ in members:
                contexts[index] = omission
            continue
        for index, hot_rva in members:
            try:
                windows[index] = (hot_rva, anchor, select_window(stream, hot_rva))
            except ContextOmission as exc:
                exc.build = build_json(state.build)
                contexts[index] = exc.as_json()
    if not windows:
        return
    rvas = sorted({instruction.rva for _, _, window in windows.values() for instruction in window})
    try:
        samples = builder._samples(module_name, rvas)
        symbols = builder._symbols(state, rvas)
        ibs = builder._ibs(state, module_name, rvas)
    except ContextOmission as exc:
        # A missing symbolizer, a failed symbolizer or an unreadable import table is the
        # module-level reason for every row of this module, not a silent empty annotation.
        exc.build = build_json(state.build)
        for index in windows:
            contexts[index] = exc.as_json()
        return
    except UprofError as exc:
        omission = ContextOmission(
            "context_failed", str(exc), build=build_json(state.build)
        ).as_json()
        for index in windows:
            contexts[index] = omission
        return
    for index, (hot_rva, anchor, window) in windows.items():
        contexts[index] = build_context(
            anchor,
            state.build,
            hot_rva,
            window,
            samples,
            builder.total_samples,
            symbols,
            state.imports,
            ibs,
        )


def instruction_contexts(
    connection: object,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    cap: int = CONTEXT_ROW_CAP,
    debug_dirs: Iterable[str] = (),
    fallback_paths: Iterable[str] = (),
    image_files: Iterable[str | Path] = (),
    image_dirs: Iterable[str | Path] = (),
    ambiguous_modules: Iterable[str] = (),
) -> list[object | None]:
    """One context (or omission) per instruction row, in row order.

    Rows without a module and RVA (the aggregate row) carry no context. Rows past cap carry
    reason row_cap instead of doing more work. debug_dirs and fallback_paths are the debug-file
    search roots the profile symbol path supplies; image_files and image_dirs resolve a module
    image that is not at its recorded path. A module in ambiguous_modules has more than one
    image version in the capture and omits.
    """
    indices = {name: index for index, name in enumerate(columns)}
    if "module" not in indices or "rva" not in indices:
        return [None] * len(rows)
    total_rows = connection.execute("SELECT total_samples FROM v_scope_totals").fetchall()
    total_samples = int(total_rows[0][0] or 0) if total_rows else 0
    builder = _ContextBuilder(
        connection,
        total_samples,
        debug_dirs=debug_dirs,
        fallback_paths=fallback_paths,
        image_files=image_files,
        image_dirs=image_dirs,
        ambiguous_modules=ambiguous_modules,
    )
    module_at, rva_at = indices["module"], indices["rva"]
    contexts: list[object | None] = [None] * len(rows)
    planned: dict[str, list[tuple[int, int, Anchor, int]]] = {}
    built = 0
    for index, row in enumerate(rows):
        module, rva_text = row[module_at], row[rva_at]
        if module is None or rva_text is None:
            continue
        if built >= cap:
            contexts[index] = {"omitted": True, "reason": "row_cap"}
            continue
        built += 1
        module_name = str(module)
        hot_rva = int(str(rva_text), 0)
        state: _ModuleState | None = None
        try:
            state = builder._module(module_name)
            anchor = select_anchor(
                hot_rva,
                state.sections,
                state.public_starts,
                state.pdata_starts,
                state.export_starts,
            )
            section = require_code_section(state.sections, hot_rva)
        except ContextOmission as exc:
            if state is not None:
                exc.build = build_json(state.build)
            contexts[index] = exc.as_json()
            continue
        except UprofError as exc:
            # A tool timeout or a failing scoped query loses one optional block, not the run.
            build = build_json(state.build) if state is not None else None
            contexts[index] = ContextOmission("context_failed", str(exc), build=build).as_json()
            continue
        planned.setdefault(module_name, []).append((index, hot_rva, anchor, section.end))
    for module_name, items in planned.items():
        _build_module_contexts(builder, contexts, module_name, items)
    return contexts


# The missing-symbol reasons context_omission_summary states once, with the detail it prints
# when the rows' details differ.
_SUMMARY_FALLBACK: Final = {
    "symbolizer_failed": "llvm-symbolizer resolved no symbol for the instruction rows",
    "pdb_unverified": "symbolization accepted no PDB the images record",
}


def context_omission_summary(contexts: Sequence[object | None]) -> str | None:
    """One stated fact when every emitted context omitted for the same missing-symbol reason.

    A symbol-less module fails every row for one reason. Repeating that as N omission objects
    invites N investigations; the caller prints this once on stderr instead.
    """
    omissions = [item for item in contexts if isinstance(item, dict) and item.get("omitted")]
    if not omissions or len(omissions) != len(contexts):
        return None
    reasons = {item.get("reason") for item in omissions}
    if len(reasons) != 1 or not reasons <= set(_SUMMARY_FALLBACK):
        return None
    reason = str(reasons.pop())
    details = sorted({str(item.get("detail")) for item in omissions if item.get("detail")})
    detail = details[0] if len(details) == 1 else _SUMMARY_FALLBACK[reason]
    return (
        f"all {len(omissions)} instruction contexts omitted with {reason}: {detail}; "
        "this is one missing-symbol finding, not one failure per row; the instruction "
        "context is optional, so report the module as symbol-less or pass profile "
        "--symbol-path <file|dir> and re-run"
    )
