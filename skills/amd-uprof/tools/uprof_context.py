"""Instruction-scope context: safe LLVM anchors, disassembly windows and their annotations.

LLVM decides instruction boundaries, names and import destinations. The tool only joins sample
data. When LLVM cannot give a safe answer the context is omitted with a machine-readable reason
rather than guessed.

The first cut covers the instruction granularity for PE images only (COFF); an ELF module is
reported as unsupported rather than disassembled with different rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from uprof_native import find_sibling, find_symbolizer, run
from uprof_util import tail_lines

ANCHOR_DISTANCE_CAP: Final = 1 << 20
WINDOW_BEFORE: Final = 32
WINDOW_AFTER: Final = 32
CONTEXT_ROW_CAP: Final = 20

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
    completed = run(command)
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        raise ContextOmission("objdump_failed",
                              tail_lines((completed.stdout or "") + "\n" +
                                         (completed.stderr or "")))
    decoded = parse_objdump(completed.stdout or "", build.image_base)
    if not decoded:
        raise ContextOmission("objdump_failed",
                              f"llvm-objdump produced no instructions for 0x{anchor.rva:X}")
    return select_window(decoded, hot_rva)

