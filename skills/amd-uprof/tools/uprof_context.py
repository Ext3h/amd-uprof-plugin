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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from uprof_native import find_sibling, find_symbolizer, run
from uprof_symbols import find_pdb
from uprof_util import tail_lines

ANCHOR_DISTANCE_CAP: Final = 1 << 20
WINDOW_BEFORE: Final = 32
WINDOW_AFTER: Final = 32
CONTEXT_ROW_CAP: Final = 20
CONTEXT_TOOL_TIMEOUT: Final = 5 * 60
IBS_FIELDS: Final = ("DataSrc", "CacheHitSt", "IbsDcMiss", "IbsOpBrnMisp",
                     "IbsDcLockedOp", "IbsOpMemWidth", "IbsTagToRetCtr")
IBS_DISTRIBUTIONS: Final = frozenset({"DataSrc", "CacheHitSt", "IbsOpMemWidth"})
IBS_FLAGS: Final = frozenset({"IbsDcMiss", "IbsOpBrnMisp", "IbsDcLockedOp"})
IBS_LATENCY: Final = "IbsTagToRetCtr"
IBS_TAG_MAX: Final = 131070
_IBS_TAG_INT: Final = r"""regexp_full_match("IbsTagToRetCtr", '\s*[0-9]+\s*')"""

_IMAGE_BASE = re.compile(r"^\s*ImageBase:\s*(0x[0-9A-Fa-f]+)\s*$")
_CHECKSUM = re.compile(r"^\s*CheckSum:\s*(0x[0-9A-Fa-f]+)\s*$")
_MAGIC = re.compile(r"^\s*Magic:\s*(0x[0-9A-Fa-f]+)\s*$")
_DATE_STAMP = re.compile(r"^\s*TimeDateStamp:.*\((0x[0-9A-Fa-f]+)\)\s*$")
_SECTION_NAME = re.compile(r"^\s*Name:\s*(\S+)(?:\s+\(([0-9A-Fa-f ]+)\))?\s*$")
_SECTION_VIRTUAL_SIZE = re.compile(r"^\s*VirtualSize:\s*(0x[0-9A-Fa-f]+)\s*$")
_SECTION_VIRTUAL_ADDRESS = re.compile(r"^\s*VirtualAddress:\s*(0x[0-9A-Fa-f]+)\s*$")
_SECTION_RAW_SIZE = re.compile(r"^\s*RawDataSize:\s*(0x[0-9A-Fa-f]+)\s*$")
_OBJDUMP_LINE = re.compile(r"^\s*([0-9A-Fa-f]+):\s+(.*)$")
_RAW_BYTES = re.compile(r"^(?:[0-9A-Fa-f]{2}\s+)+")
_IMPORT_BLOCK = re.compile(r"^\s*Import\s*\{\s*$")
_DELAY_IMPORT_BLOCK = re.compile(r"^\s*DelayImport\s*\{\s*$")
_IMPORT_END = re.compile(r"^\s*\}\s*$")
_IMPORT_DLL = re.compile(r"^\s*Name:\s*(\S+)\s*$")
_IMPORT_IAT_RVA = re.compile(r"^\s*ImportAddressTableRVA:\s*(0x[0-9A-Fa-f]+)\s*$")
_IMPORT_SYMBOL = re.compile(r"^\s*Symbol:\s*(\S+)(?:\s+\([^)]*\))?\s*$")
_RIP_OPERAND = re.compile(r"^\*(?:(-?0x[0-9A-Fa-f]+))?\(%rip\)$")
_ABSOLUTE_OPERAND = re.compile(r"^\*(-?0x[0-9A-Fa-f]+)$")
_CALL_MNEMONICS: Final = frozenset({"call", "callq", "calll", "callw"})


class ContextOmission(Exception):
    """A context that cannot be produced safely; reason is a stable machine-readable code."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"omitted": True, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
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
    """PE header identity used to confirm the module matches the capture's build."""

    image_base: int
    time_date_stamp: int
    checksum: int
    magic: int

    @property
    def slot_width(self) -> int:
        """IAT slot width: 8 bytes for PE32+, 4 for PE32."""
        return 8 if self.magic == 0x20B else 4


@dataclass(frozen=True)
class VerifiedBuild:
    """A PE identity that matches the checksum recorded in the capture."""

    identity: BuildIdentity
    capture_checksum: int
    verified: bool = True

    @property
    def checksum(self) -> int:
        return self.identity.checksum

    @property
    def image_base(self) -> int:
        return self.identity.image_base

    @property
    def time_date_stamp(self) -> int:
        return self.identity.time_date_stamp


@dataclass(frozen=True)
class Anchor:
    """The instruction boundary a window is decoded forward from."""

    kind: str          # "export" or "section"
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


def parse_file_headers(text: str) -> BuildIdentity:
    """Read ImageBase, TimeDateStamp, CheckSum and the optional-header magic from readobj."""
    image_base = checksum = magic = date_stamp = None
    for line in text.splitlines():
        if image_base is None and (match := _IMAGE_BASE.match(line)):
            image_base = int(match.group(1), 16)
        elif checksum is None and (match := _CHECKSUM.match(line)):
            checksum = int(match.group(1), 16)
        elif magic is None and (match := _MAGIC.match(line)):
            magic = int(match.group(1), 16)
        elif date_stamp is None and (match := _DATE_STAMP.match(line)):
            date_stamp = int(match.group(1), 16)
    missing = [name for name, value in (("ImageBase", image_base), ("TimeDateStamp", date_stamp),
                                        ("CheckSum", checksum), ("Magic", magic))
               if value is None]
    if missing:
        raise ContextOmission("metadata_incomplete",
                              f"llvm-readobj file headers lack {', '.join(missing)}")
    return BuildIdentity(image_base=image_base, time_date_stamp=date_stamp,
                         checksum=checksum, magic=magic)


def parse_sections(text: str) -> list[PeSection]:
    """Read section RVAs and sizes from `llvm-readobj --sections`."""
    sections: list[PeSection] = []
    current: dict[str, object] = {}
    in_block = False
    for line in text.splitlines():
        if re.match(r"^\s*Section\s*\{\s*$", line):
            current = {}
            in_block = True
            continue
        if not in_block:
            continue
        if re.match(r"^\s*\}\s*$", line):
            _append_section(sections, current)
            current = {}
            in_block = False
            continue
        if "Name:" in line and (match := _SECTION_NAME.match(line)):
            current["name"] = match.group(1)
        elif match := _SECTION_VIRTUAL_SIZE.match(line):
            current["virtual_size"] = int(match.group(1), 16)
        elif match := _SECTION_VIRTUAL_ADDRESS.match(line):
            current["start"] = int(match.group(1), 16)
        elif match := _SECTION_RAW_SIZE.match(line):
            current["raw_size"] = int(match.group(1), 16)
        elif "IMAGE_SCN_CNT_CODE" in line or "IMAGE_SCN_MEM_EXECUTE" in line:
            current["code"] = True
    return sections


def _append_section(sections: list[PeSection], values: dict[str, object]) -> None:
    start = values.get("start")
    if start is None:
        return
    size = values.get("virtual_size") or values.get("raw_size") or 0
    sections.append(PeSection(start=int(start), size=int(size),
                              code=bool(values.get("code", False))))


def find_code_section(sections: list[PeSection], rva: int) -> PeSection | None:
    """The executable section containing rva, or None."""
    for section in sections:
        if section.code and section.start <= rva < section.end:
            return section
    return None


def select_anchor(hot_rva: int, sections: list[PeSection],
                  public_starts: list[tuple[int, str | None]],
                  cap: int = ANCHOR_DISTANCE_CAP) -> Anchor:
    """Nearest public start at or below hot_rva inside its code section, else the section start."""
    section = find_code_section(sections, hot_rva)
    if section is None:
        raise ContextOmission("no_code_section",
                              f"0x{hot_rva:X} is not inside an executable PE section")
    candidates = [(rva, name) for rva, name in public_starts
                  if section.start <= rva <= hot_rva]
    if candidates:
        rva, name = max(candidates, key=lambda item: item[0])
        if hot_rva - rva <= cap:
            return Anchor("export", rva, name)
    if hot_rva - section.start <= cap:
        return Anchor("section", section.start, None)
    raise ContextOmission(
        "anchor_too_far",
        f"0x{hot_rva - section.start:X} bytes from the nearest safe start exceeds the "
        f"{cap}-byte cap")


def is_instruction_start(decoded_starts: list[int], hot_rva: int) -> bool:
    """Whether the decoded stream reaches hot_rva on an instruction boundary."""
    return hot_rva in decoded_starts


def verify_build(build: BuildIdentity, capture_checksum: object) -> VerifiedBuild:
    """Accept the module only when its PE checksum matches the capture's recorded checksum."""
    recorded = _as_int(capture_checksum)
    if not recorded or not build.checksum:
        raise ContextOmission("build_unverified",
                              "the capture records no usable module checksum")
    if recorded != build.checksum:
        raise ContextOmission(
            "build_mismatch",
            f"capture checksum 0x{recorded:X} differs from the on-disk module checksum "
            f"0x{build.checksum:X}")
    return VerifiedBuild(identity=build, capture_checksum=recorded)


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
            imports[slot_rva] = ImportSlot(slot_rva, dll, match.group(1))
            index += 1
    return imports


def resolve_iat_call(instruction: DecodedInstruction, image_base: int,
                     imports: dict[int, ImportSlot]) -> ImportSlot | None:
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


def load_imports(module_path: str, build: BuildIdentity,
                 readobj: str | None = None) -> dict[int, ImportSlot]:
    """Read the module's IAT slots; a readobj failure is an omission, not an empty table."""
    tool = readobj or find_readobj()
    if not tool:
        raise ContextOmission("readobj_missing",
                              "llvm-readobj not found beside llvm-symbolizer or on PATH")
    completed = run([tool, "--coff-imports", module_path], timeout=CONTEXT_TOOL_TIMEOUT)
    if completed.returncode != 0:
        raise ContextOmission("imports_failed",
                              tail_lines((completed.stdout or "") + "\n" +
                                         (completed.stderr or "")))
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


def symbolize_rvas(module_path: str, rvas: list[int], pdb: str | Path | None = None,
                   symbolizer: str | None = None) -> dict[int, SymbolInfo]:
    """Symbolize module-relative addresses; a tool failure is an omission, not a guessed name."""
    addresses = sorted({int(rva) for rva in rvas})
    if not addresses:
        return {}
    found = symbolizer if symbolizer else find_symbolizer()
    if not found:
        raise ContextOmission("symbolizer_missing", "llvm-symbolizer not found")
    command = [str(found), f"--obj={module_path}"]
    if pdb:
        command.append(f"--pdb={pdb}")
    command += ["--relative-address", "--functions=linkage", "--inlines",
                "--output-style=JSON"]
    input_text = "\n".join(f"0x{rva:X}" for rva in addresses) + "\n"
    completed = run(command, input_text=input_text, timeout=CONTEXT_TOOL_TIMEOUT)
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        raise ContextOmission("symbolizer_failed",
                              tail_lines((completed.stdout or "") + "\n" +
                                         (completed.stderr or "")))
    return parse_symbolizer_json(completed.stdout or "")


# ------------------------------------------------------------------ IBS hardware labels

def present_ibs_fields(columns: Iterable[str]) -> tuple[str, ...]:
    """The whitelisted IBS fields the dump carries, in a fixed order."""
    present = set(columns)
    return tuple(field for field in IBS_FIELDS if field in present)


def ibs_expression(field: str) -> list[str]:
    """SELECT expressions for one field: a value histogram, a nonzero count or latency stats.

    The field name itself is the label; nothing maps the hardware value to a semantic role.
    """
    if field in IBS_DISTRIBUTIONS:
        return [f'histogram("{field}") AS "{field}"']
    if field in IBS_FLAGS:
        return [f'count(*) FILTER (WHERE TRY_CAST("{field}" AS BIGINT) <> 0) AS "{field}"']
    if field == IBS_LATENCY:
        clipped = (f'CASE WHEN {_IBS_TAG_INT} '
                   f'THEN LEAST(TRY_CAST("{field}" AS BIGINT), {IBS_TAG_MAX}) END')
        return [f'count(*) FILTER (WHERE {_IBS_TAG_INT}) AS "{field}_samples"',
                f'sum({clipped}) AS "{field}_sum"',
                f'max({clipped}) AS "{field}_max"']
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
    for field in fields:
        if field in IBS_DISTRIBUTIONS:
            counts = record.get(field)
            if isinstance(counts, dict) and counts:
                summary[field] = {
                    str(code): int(count) for code, count in
                    sorted(counts.items(), key=lambda item: _code_key(str(item[0])))}
        elif field in IBS_FLAGS:
            summary[field] = {"set": int(record.get(field) or 0), "of": samples}
        elif field == IBS_LATENCY:
            observed = int(record.get(f"{field}_samples") or 0)
            if observed:
                summary[field] = {"samples": observed,
                                  "sum": int(record.get(f"{field}_sum") or 0),
                                  "max": int(record.get(f"{field}_max") or 0)}
    return summary


def ibs_rows(connection: object, module_name: str,
             rvas: list[int]) -> dict[int, dict[str, object]]:
    """Aggregate the dump's IBS fields per requested RVA; empty when there is no IBS dump."""
    addresses = sorted({int(rva) for rva in rvas})
    if not addresses:
        return {}
    dump = connection.execute("SELECT getvariable('uprof_ibs_dump')").fetchone()
    if not dump or not dump[0]:
        return {}
    columns = {str(row[0]) for row in connection.execute(
        "DESCRIBE SELECT * FROM read_csv(getvariable('uprof_ibs_dump'), "
        "header = true, all_varchar = true)").fetchall()}
    fields = present_ibs_fields(columns)
    if not fields:
        return {}
    expressions = [expression for field in fields for expression in ibs_expression(field)]
    sql = ('SELECT CAST("Offset" AS BIGINT) AS rva, count(*) AS samples, '
           + ", ".join(expressions)
           + " FROM read_csv(getvariable('uprof_ibs_dump'), header = true, all_varchar = true)"
             " WHERE module_key(ModuleName) = CAST(? AS VARCHAR)"
             ' AND TRY_CAST("Offset" AS BIGINT) IN (SELECT * FROM unnest(?))'
             " GROUP BY rva")
    rows = connection.execute(sql, [module_name, addresses]).fetchall()
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


def select_window(decoded: list[DecodedInstruction], hot_rva: int,
                  before: int = WINDOW_BEFORE, after: int = WINDOW_AFTER
                  ) -> list[DecodedInstruction]:
    """The last <=before instructions and <=after instructions around hot_rva.

    A hot RVA that is not an instruction start in the decoded stream has no safe window; it is
    rejected rather than snapped to the preceding boundary.
    """
    for index, instruction in enumerate(decoded):
        if instruction.rva == hot_rva:
            return decoded[max(0, index - before):index + after + 1]
    raise ContextOmission(
        "hot_rva_not_instruction_start",
        f"0x{hot_rva:X} was not reached as an instruction boundary")


def instruction_share(samples: int, total_samples: int) -> float | None:
    """Raw sample share, the same denominator as the instruction row's pct_samples."""
    if not total_samples:
        return None
    return 100.0 * samples / total_samples


def decode_window(module_path: str, build: BuildIdentity, anchor: Anchor, hot_rva: int,
                  objdump: str | None = None) -> list[DecodedInstruction]:
    """Decode forward from anchor and return the window around hot_rva.

    The stop address leaves enough room for 32 instructions after the hot RVA plus one more, so
    the last window instruction still has a known length for RIP-relative call resolution.
    """
    tool = objdump or find_objdump()
    if not tool:
        raise ContextOmission("objdump_missing",
                              "llvm-objdump not found beside llvm-symbolizer or on PATH")
    start_vma = build.image_base + anchor.rva
    stop_vma = build.image_base + hot_rva + (WINDOW_AFTER + 2) * 16
    if stop_vma <= start_vma:
        stop_vma = start_vma + 16
    command = [tool, "-d", f"--start-address=0x{start_vma:X}",
               f"--stop-address=0x{stop_vma:X}", "--no-show-raw-insn",
               "--no-symbolize-operands", module_path]
    completed = run(command, timeout=CONTEXT_TOOL_TIMEOUT)
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        raise ContextOmission("objdump_failed",
                              tail_lines((completed.stdout or "") + "\n" +
                                         (completed.stderr or "")))
    decoded = parse_objdump(completed.stdout or "", build.image_base)
    if not decoded:
        raise ContextOmission("objdump_failed",
                              f"llvm-objdump produced no instructions for 0x{anchor.rva:X}")
    return select_window(decoded, hot_rva)


# ------------------------------------------------------------------ JSON context block

def _hex(value: int) -> str:
    return f"0x{value:X}"


def build_context(anchor: Anchor, build: VerifiedBuild, hot_rva: int,
                  instructions: list[DecodedInstruction], samples: dict[int, int],
                  total_samples: int, symbols: dict[int, SymbolInfo],
                  imports: dict[int, ImportSlot],
                  ibs: dict[int, dict[str, object]]) -> dict[str, object]:
    """The JSON block for one instruction row; instructions are ordered by RVA."""
    anchor_json: dict[str, object] = {"kind": anchor.kind, "rva": _hex(anchor.rva)}
    if anchor.name is not None:
        anchor_json["name"] = anchor.name
    anchor_json["distance"] = hot_rva - anchor.rva
    build_json = {
        "verified": bool(build.verified),
        "image_base": _hex(build.image_base),
        "time_date_stamp": _hex(build.time_date_stamp),
        "checksum": _hex(build.checksum),
        "capture_checksum": _hex(build.capture_checksum),
    }
    entries: list[dict[str, object]] = []
    for instruction in sorted(instructions, key=lambda item: item.rva):
        count = int(samples.get(instruction.rva, 0))
        entry: dict[str, object] = {
            "rva": _hex(instruction.rva),
            "instruction": instruction.text,
            "samples": count,
            "pct_samples": instruction_share(count, total_samples),
        }
        symbol = symbols.get(instruction.rva)
        if symbol is not None:
            symbol_json: dict[str, object] = {}
            if symbol.function is not None:
                symbol_json["function"] = symbol.function
            if symbol.file is not None:
                symbol_json["file"] = symbol.file
            if symbol.line is not None:
                symbol_json["line"] = symbol.line
            if symbol_json:
                entry["symbol"] = symbol_json
        slot = resolve_iat_call(instruction, build.image_base, imports)
        if slot is not None:
            entry["import"] = {"dll": slot.dll, "name": slot.name,
                               "slot_rva": _hex(slot.slot_rva)}
        ibs_labels = ibs.get(instruction.rva)
        if ibs_labels and len(ibs_labels) > 1:
            entry["ibs"] = ibs_labels
        entries.append(entry)
    return {"anchor": anchor_json, "build": build_json, "instructions": entries}


@dataclass
class _ModuleState:
    """Per-module work reused by every row that names the same module."""

    path: str
    build: VerifiedBuild
    sections: list[PeSection]
    imports: dict[int, ImportSlot]
    public_starts: list[tuple[int, str | None]]
    pdb: str | None
    symbols: dict[int, SymbolInfo] = field(default_factory=dict)
    symbol_attempted: set[int] = field(default_factory=set)
    ibs: dict[int, dict[str, object]] = field(default_factory=dict)
    ibs_attempted: set[int] = field(default_factory=set)


class _ContextBuilder:
    def __init__(self, connection: object, total_samples: int) -> None:
        self.connection = connection
        self.total_samples = total_samples
        self.modules: dict[str, _ModuleState | ContextOmission] = {}

    def context(self, module_name: str, hot_rva: int) -> dict[str, object]:
        state = self._module(module_name)
        anchor = select_anchor(hot_rva, state.sections, state.public_starts)
        window = decode_window(state.path, state.build, anchor, hot_rva)
        rvas = [instruction.rva for instruction in window]
        return build_context(
            anchor, state.build, hot_rva, window, self._samples(module_name, rvas),
            self.total_samples, self._symbols(state, rvas),
            state.imports, self._ibs(state, module_name, rvas))

    def _module(self, module_name: str) -> _ModuleState:
        cached = self.modules.get(module_name)
        if isinstance(cached, ContextOmission):
            raise ContextOmission(cached.reason, cached.detail)
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
        checksum_column = self.connection.execute(
            "SELECT count(*) FROM duckdb_columns() "
            "WHERE lower(table_name) = 'module' AND lower(column_name) = 'checksum'"
        ).fetchone()[0]
        row = self.connection.execute(
            f"SELECT max(path), {'max(checksum)' if checksum_column else 'NULL'} "
            "FROM Module WHERE module_key(path) = CAST(? AS VARCHAR)",
            [module_name]).fetchone()
        path = row[0] if row else None
        capture_checksum = row[1] if row else None
        if not path:
            raise ContextOmission("module_missing",
                                  f"the capture records no path for {module_name!r}")
        module_path = str(path)
        if not Path(module_path).is_file():
            raise ContextOmission("module_missing", f"{module_path} is not on this host")
        readobj = find_readobj()
        if not readobj:
            raise ContextOmission("readobj_missing",
                                  "llvm-readobj not found beside llvm-symbolizer or on PATH")
        completed = run([readobj, "--file-headers", "--sections", module_path],
                        timeout=CONTEXT_TOOL_TIMEOUT)
        if completed.returncode != 0:
            raise ContextOmission("readobj_failed",
                                  tail_lines((completed.stdout or "") + "\n" +
                                             (completed.stderr or "")))
        text = completed.stdout or ""
        build = parse_file_headers(text)
        sections = parse_sections(text)
        verified = verify_build(build, capture_checksum)
        try:
            imports = load_imports(module_path, build, readobj)
        except ContextOmission:
            imports = {}
        public_starts = [
            (int(rva), names) for rva, names in self.connection.execute(
                "SELECT rva, names FROM v_public_starts "
                "WHERE module_name = CAST(? AS VARCHAR)", [module_name]).fetchall()]
        pdb = find_pdb(module_path)
        return _ModuleState(module_path, verified, sections, imports, public_starts,
                            str(pdb) if pdb else None)

    def _samples(self, module_name: str, rvas: list[int]) -> dict[int, int]:
        if not rvas:
            return {}
        rows = self.connection.execute(
            "SELECT rva, CAST(sum(samples) AS BIGINT) FROM v_scoped "
            "WHERE module_name = CAST(? AS VARCHAR) "
            "AND rva IN (SELECT * FROM unnest(?)) GROUP BY rva",
            [module_name, sorted({int(rva) for rva in rvas})]).fetchall()
        return {int(rva): int(count or 0) for rva, count in rows}

    def _symbols(self, state: _ModuleState, rvas: list[int]) -> dict[int, SymbolInfo]:
        missing = [rva for rva in rvas if rva not in state.symbol_attempted]
        if missing:
            state.symbol_attempted.update(missing)
            try:
                state.symbols.update(symbolize_rvas(state.path, missing, pdb=state.pdb))
            except ContextOmission:
                pass
        return state.symbols

    def _ibs(self, state: _ModuleState, module_name: str,
             rvas: list[int]) -> dict[int, dict[str, object]]:
        missing = [rva for rva in rvas if rva not in state.ibs_attempted]
        if missing:
            state.ibs_attempted.update(missing)
            state.ibs.update(ibs_rows(self.connection, module_name, missing))
        return state.ibs


def instruction_contexts(connection: object, columns: Sequence[str],
                         rows: Sequence[Sequence[object]],
                         cap: int = CONTEXT_ROW_CAP) -> list[object | None]:
    """One context (or omission) per instruction row, in row order.

    Rows without a module and RVA (the aggregate row) carry no context. Rows past cap carry
    reason row_cap instead of doing more work.
    """
    indices = {name: index for index, name in enumerate(columns)}
    if "module" not in indices or "rva" not in indices:
        return [None] * len(rows)
    total_rows = connection.execute("SELECT total_samples FROM v_scope_totals").fetchall()
    total_samples = int(total_rows[0][0] or 0) if total_rows else 0
    builder = _ContextBuilder(connection, total_samples)
    module_at, rva_at = indices["module"], indices["rva"]
    contexts: list[object | None] = []
    built = 0
    for row in rows:
        module, rva_text = row[module_at], row[rva_at]
        if module is None or rva_text is None:
            contexts.append(None)
        elif built >= cap:
            contexts.append({"omitted": True, "reason": "row_cap"})
        else:
            built += 1
            try:
                contexts.append(builder.context(str(module), int(str(rva_text), 0)))
            except ContextOmission as exc:
                contexts.append(exc.as_json())
    return contexts

