"""Derive function ranges from a PE image with LLVM, for samples uProf did not name.

A boundary is a PE export, a PDB public symbol or a runtime-function start from `.pdata`; a
range runs from one boundary to the next boundary in the same executable section, or to the
section end. The range is what `profile --by function` groups unresolved samples by, so a
consumer does not write a PE parser or map `.pdata` by hand. The names here are only the names
the boundaries carry; the SQL adds a visibly synthetic name when no source symbol exists.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Sequence

import uprof_context
from uprof_context import ContextOmission, PeSection
from uprof_native import run
from uprof_symbols import (artifact_file_identity, artifact_identity_present, artifact_key,
                           load_cached_artifacts, store_cached_text)
from uprof_util import UprofError, tail_lines

READOBJ_TIMEOUT: Final = 5 * 60
# At one RVA an export is the most specific name, then a PDB public, then a .pdata start.
SOURCE_PRIORITY: Final = {"export": 0, "pdb_public": 1, "pdata": 2, "none": 3}
_PUBLIC_SEPARATOR: Final = " | "


@dataclass(frozen=True)
class FunctionRange:
    """One derived function range: [start, end) in module-relative bytes.

    source is where the start boundary came from (export, pdb_public, pdata or none); name is
    None when that source carries no name, which is always the case for pdata.
    """

    module_name: str
    start: int
    end: int
    source: str
    name: str | None


@dataclass
class RangeDerivation:
    """The ranges one module produced and, when there are none, why."""

    ranges: list[FunctionRange]
    reason: str | None = None


def _boundaries(section: PeSection, sources: Sequence[tuple[str, Iterable[tuple[int, str | None]]]]
                ) -> dict[int, tuple[int, str, str | None]]:
    """The strongest boundary at each RVA inside one executable section."""
    by_rva: dict[int, tuple[int, str, str | None]] = {}
    for source, starts in sources:
        for rva, name in starts:
            if not (section.start <= rva < section.end):
                continue
            current = by_rva.get(rva)
            if current is None or SOURCE_PRIORITY[source] < current[0]:
                by_rva[rva] = (SOURCE_PRIORITY[source], source, name)
    return by_rva


def build_ranges(module_name: str, sections: Sequence[PeSection],
                 public_starts: Sequence[tuple[int, str | None]] = (),
                 pdata_starts: Sequence[tuple[int, str | None]] = (),
                 export_starts: Sequence[tuple[int, str | None]] = ()) -> list[FunctionRange]:
    """Function ranges for every executable section, in ascending section order.

    A section with no export, PDB public or .pdata start yields no range: a whole-section row
    would claim one function where no boundary was derived, so --by region is the fallback for
    such an image. Ranges do not overlap: each end is the next start in the same section.
    """
    sources = (("export", export_starts), ("pdb_public", public_starts),
               ("pdata", pdata_starts))
    code_sections = sorted((section for section in sections if section.code and section.size > 0),
                           key=lambda section: section.start)
    ranges: list[FunctionRange] = []
    for section in code_sections:
        boundaries = _boundaries(section, sources)
        if not boundaries:
            continue
        starts = sorted(boundaries)
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else section.end
            _, source, name = boundaries[start]
            ranges.append(FunctionRange(module_name, start, end, source, name))
    return ranges


def public_starts_from_names(rows: Iterable[tuple[object, object]]) -> list[tuple[int, str | None]]:
    """(rva, name) public starts from v_public_starts rows of (rva, names).

    A folded start carries several names joined by ' | '; the first is used as the range name.
    """
    starts: list[tuple[int, str | None]] = []
    for rva, names in rows:
        text = str(names or "")
        name = text.split(_PUBLIC_SEPARATOR, 1)[0].strip() or None
        starts.append((int(rva), name))
    return starts


def _ranges_from_cache(path: Path, module_name: str) -> list[FunctionRange] | None:
    """Ranges from a cached artifact, or None when the artifact is unreadable or malformed."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["ranges"]
        return [FunctionRange(module_name, int(row[0]), int(row[1]), str(row[2]),
                              None if row[3] is None else str(row[3])) for row in rows]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def derive_module_ranges(module_name: str, module_path: str | Path,
                         public_starts: Sequence[tuple[int, str | None]] = (),
                         readobj: str | None = None) -> RangeDerivation:
    """Read one module with llvm-readobj and derive its ranges; never guesses a boundary.

    A missing or unusable tool, a non-PE image or an image with no executable section yields no
    ranges and a stable reason, which the caller reports instead of failing the profile.
    """
    tool = readobj or uprof_context.find_readobj()
    if not tool:
        return RangeDerivation([], "readobj_missing")
    # The ranges depend only on the image, the effective public starts and the readobj build, so
    # an entry serves every capture. Only the first name at an RVA can win, and sorting by RVA
    # makes the key independent of the row order the SQL returned. Failures are not stored: a
    # tool failure may be transient and an empty result carries its reason.
    image_id = artifact_file_identity(module_path)
    readobj_id = artifact_file_identity(tool)
    cache_key = None
    if artifact_identity_present(image_id) and artifact_identity_present(readobj_id):
        first_by_rva: dict[int, str | None] = {}
        for rva, name in public_starts:
            first_by_rva.setdefault(rva, name)
        cache_key = artifact_key({
            "kind": "ranges",
            "module_name": module_name,
            "image": image_id,
            "readobj": readobj_id,
            "public_starts": sorted(first_by_rva.items()),
        })
        cached = load_cached_artifacts("ranges", cache_key, ("ranges.json",))
        if cached is not None:
            ranges = _ranges_from_cache(cached[0] / "ranges.json", module_name)
            if ranges is not None:
                return RangeDerivation(ranges)
    try:
        completed = run([tool, "--file-headers", "--sections", "--unwind", "--coff-exports",
                         str(module_path)], timeout=READOBJ_TIMEOUT)
    except UprofError as exc:
        return RangeDerivation([], f"readobj_failed: {exc}")
    if completed.returncode != 0:
        detail = tail_lines((completed.stdout or "") + "\n" + (completed.stderr or ""))
        return RangeDerivation([], f"readobj_failed: {detail or 'no output'}")
    text = completed.stdout or ""
    try:
        image = uprof_context.parse_image(text)
    except ContextOmission as exc:
        # ELF and other non-PE images are not mapped with PE rules.
        return RangeDerivation([], f"not_pe: {exc.detail or exc.reason}")
    sections = image.sections
    if not [section for section in sections if section.code and section.size > 0]:
        return RangeDerivation([], "no_code_section")
    ranges = build_ranges(module_name, sections, public_starts,
                          image.unwind_starts, image.exports)
    if not ranges:
        return RangeDerivation([], "no_boundary_source")
    if cache_key is not None:
        store_cached_text("ranges", cache_key, "ranges.json",
                          json.dumps({"ranges": [[item.start, item.end, item.source, item.name]
                                                 for item in ranges]},
                                     separators=(",", ":")) + "\n")
    return RangeDerivation(ranges)


def write_ranges_csv(path: Path, ranges: Sequence[FunctionRange]) -> Path:
    """Write the range table the SQL view reads; UTF-8 without a BOM, LF newlines.

    function_name is empty for an unnamed boundary; the SQL supplies the synthetic name and
    reports that it is derived.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["module_name", "function_start", "function_end", "function_source",
                         "function_name"])
        for item in ranges:
            writer.writerow([item.module_name, item.start, item.end, item.source,
                             item.name or ""])
    return path
