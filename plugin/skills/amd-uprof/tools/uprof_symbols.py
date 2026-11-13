"""Addresses -> file:line plus the PDB public-symbol fold cache (Windows PDB / Linux DWARF)."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TextIO

import uprof_db
from uprof_native import (
    NtSymbolPath,
    SymbolizerUnsupported,
    find_sibling,
    parse_nt_symbol_path,
    require_symbolizer,
    run,
    symbol_cache_dir,
    tool_operand,
)
from uprof_util import UprofError, absolute, read_text, require_file, same_path, tail_lines

_GUID = re.compile(r"^\s*GUID:\s*(.+)$")
_AGE = re.compile(r"^\s*Age:\s*(\d+)")
_READOBJ = {
    "name": re.compile(r"^\s*PDBFileName:\s*(.+)$"),
    "guid": re.compile(r"^\s*PDBGUID:\s*(.+)$"),
    "age": re.compile(r"^\s*PDBAge:\s*(.+)$"),
}

_SECTION_HEADER = re.compile(r"SECTION HEADER #(\d+)")
_SECTION_ADDRESS = re.compile(r"^\s*([0-9A-Fa-f]+) virtual address\s*$")
_PUBLIC_NAME = re.compile(r"S_PUB32 \[[^\]]*\] `([^`]*)`\s*$")
_PUBLIC_ADDRESS = re.compile(r"flags\s*=\s*([^,]*),\s*addr\s*=\s*(\d+):(\d+)")
_PUBLICS_MARKER = re.compile(r"^\s*Public Symbols\s*$", re.MULTILINE)

PDB_PROBE_TIMEOUT: Final = 5 * 60
# Module work is native-tool and DuckDB bound and releases the GIL, so a small fixed pool stops
# short of oversubscribing instead of scaling with the module count.
MAX_SYMBOLIZATION_WORKERS: Final = 8
# Per-module symbol parquet writes at or below this row count use one DuckDB thread; a larger
# write keeps the default pool, whose startup cost is then repaid. The threshold sits above the
# largest per-module symbol tables seen in normal captures.
PARQUET_SINGLE_THREAD_MAX_ROWS: Final = 100_000


def symbolizer_search_paths(
    symbol_files: Iterable[str | Path], symbol_dirs: Iterable[str | Path]
) -> tuple[list[str], list[str]]:
    """The debug-file search directories a symbol-path set supplies.

    Every search directory is both a primary and a fallback root, so one flag covers both
    symbolizer lookups. An explicit non-PDB symbol file contributes its directory, which is how a
    caller supplies a separate DWARF/debug file.
    """
    roots = [os.fspath(path) for path in symbol_dirs]
    parents = [
        os.fspath(Path(path).parent) for path in symbol_files if Path(path).suffix.lower() != ".pdb"
    ]
    combined = list(dict.fromkeys([*roots, *parents]))
    return combined, combined


@dataclass(frozen=True)
class SourcePaths:
    """Caller-facing symbol and image paths, split by whether each path is a file or a root.

    symbol_files are explicit symbol/debug files; symbol_dirs are search roots. image_files are
    explicit module images; image_dirs are searched by the module's basename. The tool probes the
    filesystem, so a caller never has to choose between a file and a directory flag.
    """

    symbol_files: tuple[Path, ...] = ()
    symbol_dirs: tuple[Path, ...] = ()
    image_files: tuple[Path, ...] = ()
    image_dirs: tuple[Path, ...] = ()

    @property
    def debug_dirs(self) -> tuple[Path, ...]:
        """Search roots for separate debug files: the directory roots and a debug file's parent."""
        return tuple(
            Path(path) for path in symbolizer_search_paths(self.symbol_files, self.symbol_dirs)[0]
        )


def _probe_paths(
    values: list[str] | tuple[str, ...] | None, option: str
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    files: list[Path] = []
    dirs: list[Path] = []
    for value in values or ():
        path = absolute(value)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            dirs.append(path)
        else:
            raise UprofError(f"{option} path not found: {path}")
    return tuple(dict.fromkeys(files)), tuple(dict.fromkeys(dirs))


def probe_paths(
    symbol_paths: list[str] | tuple[str, ...] | None = None,
    image_paths: list[str] | tuple[str, ...] | None = None,
) -> SourcePaths:
    """Split repeatable --symbol-path/--image-path values into explicit files and search roots."""
    symbol_files, symbol_dirs = _probe_paths(symbol_paths, "--symbol-path")
    image_files, image_dirs = _probe_paths(image_paths, "--image-path")
    return SourcePaths(symbol_files, symbol_dirs, image_files, image_dirs)


def _image_names(module_name: str, module_path: str | None) -> list[str]:
    names: list[str] = []
    for value in (module_path, module_name):
        if value:
            name = Path(value).name
            if name:
                names.append(name)
    return list(dict.fromkeys(names))


def resolve_image(
    module_name: str,
    module_path: str | None,
    image_files: tuple[Path, ...] | list[Path] = (),
    image_dirs: tuple[Path, ...] | list[Path] = (),
) -> tuple[Path | None, str | None]:
    """Resolve the image for one module from --image-path, else from the capture's own path.

    An explicit image file matches by basename; a directory is searched for the basename. The
    capture's recorded path is the last resort, so a run without --image-path is unchanged. The
    second value names the source when --image-path supplied the image, and is None otherwise.
    """
    names = _image_names(module_name, module_path)
    folded = {name.casefold() for name in names}
    for path in image_files:
        if path.name.casefold() in folded:
            return path, "explicit --image-path file"
    for directory in image_dirs:
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file():
                return candidate, f"found in --image-path directory {directory}"
    if module_path and Path(module_path).is_file():
        return Path(module_path), None
    return None, None


@dataclass
class PdbProbe:
    """One candidate PDB and why it was accepted or rejected; detail is the evidence."""

    path: Path
    status: str  # accepted, not_found, mismatch, probe_failed, unverified
    pdb_guid: str | None = None
    pdb_age: str | None = None
    detail: str = ""


@dataclass
class PdbSearch:
    """The accepted PDB (or None), every probe, and the srv URLs that were never contacted."""

    pdb: Path | None = None
    probes: list[PdbProbe] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


@dataclass
class SymbolizerRun:
    """One llvm-symbolizer invocation: exit status, its JSON Error objects and stderr.

    records counts addresses that came back with a Symbol result; empty records is the subset
    with no usable frame (no FunctionName and line 0). A wrong-build PDB produces the latter
    with exit status 0 and no Error object, so the pair is the evidence for that case.
    """

    returncode: int
    errors: list[tuple[str | None, str]] = field(default_factory=list)
    stderr: str = ""
    records: int = 0
    empty_records: int = 0


@dataclass
class ModuleEvidence:
    """Per-module evidence for the symbolization outcome, printed when no PDB is accepted."""

    module_name: str
    module_path: str
    found_debug_info: bool | None = None
    image_guid: str | None = None
    image_age: int | None = None
    search: PdbSearch | None = None
    symbolizer: SymbolizerRun | None = None
    pdb: str | None = None
    # resolved, no_debug_info, no_pdb, readobj_failed, module_missing, wrong_build, failed
    outcome: str = "resolved"
    note: str | None = None
    # A toolchain cause carries its own remedy in note; the symbol-path remedy would mislead.
    toolchain: bool = False
    # A cause-specific remedy that replaces the default symbol-path remedy.
    remedy: str | None = None


@dataclass
class SymbolizedModule:
    """The artifacts one module produced and the evidence behind the PDB choice."""

    parquet: Path | None
    publics: Path | None
    pdb: Path | None
    evidence: ModuleEvidence


def _normalize_guid(value: str) -> str:
    return re.sub(r"[{}-]", "", value).strip().upper()


def _as_int(value: object) -> int | None:
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return None


_HEADER_CHECKSUM = re.compile(r"^\s*CheckSum:\s*(0x[0-9A-Fa-f]+)\s*$")
_HEADER_SIZE_OF_IMAGE = re.compile(r"^\s*SizeOfImage:\s*(\d+)\s*$")


def image_build_mismatch(
    image: str, capture_checksum: object, capture_size: object, readobj: Path | None
) -> str | None:
    """Why the image on disk is not the build the capture recorded, or None.

    A nonzero PE CheckSum pair that differs proves another build; without that pair, a nonzero
    SizeOfImage pair that differs does. Missing evidence (no Module column, CheckSum 0, an image
    without PE headers, no llvm-readobj) is not a mismatch, so such modules are symbolized as
    before.
    """
    recorded, recorded_size = _as_int(capture_checksum), _as_int(capture_size)
    if not (recorded or recorded_size) or not readobj:
        return None
    completed = run(
        [str(readobj), "--file-headers", tool_operand(image)], timeout=PDB_PROBE_TIMEOUT
    )
    if completed.returncode != 0:
        return None
    checksum = size = None
    for line in (completed.stdout or "").splitlines():
        if checksum is None and (match := _HEADER_CHECKSUM.match(line)):
            checksum = int(match.group(1), 16)
        elif size is None and (match := _HEADER_SIZE_OF_IMAGE.match(line)):
            size = int(match.group(1), 10)
    if recorded and checksum:
        if recorded != checksum:
            return (
                f"the image is a different build than the capture recorded: CheckSum "
                f"0x{checksum:X}, capture 0x{recorded:X}"
            )
        return None
    if recorded_size and size and recorded_size != size:
        return (
            f"the image is a different build than the capture recorded: SizeOfImage "
            f"0x{size:X}, capture 0x{recorded_size:X}"
        )
    return None


def _readobj_info(exe: str, readobj: Path | None) -> tuple[str, str | None, int | None, str | None]:
    """(PDB file name, GUID, age, PDB path as recorded) from the image's CodeView entry."""
    pdb_name = Path(exe).stem + ".pdb"
    guid = age = recorded = None
    if not readobj:
        return pdb_name, guid, age, recorded
    completed = run(
        [str(readobj), "--coff-debug-directory", tool_operand(exe)], timeout=PDB_PROBE_TIMEOUT
    )
    if completed.returncode != 0:
        raise UprofError(
            f"llvm-readobj exited {completed.returncode}: {tail_lines(completed.stderr)}"
        )
    for line in (completed.stdout or "").splitlines():
        for key, pattern in _READOBJ.items():
            match = pattern.match(line)
            if not match:
                continue
            value = match.group(1).strip()
            if key == "name":
                pdb_name = Path(value).name
                recorded = value
            elif key == "guid":
                guid = _normalize_guid(value)
            else:
                age = _as_int(value)
    return pdb_name, guid, age, recorded


def _pdb_signature(pdbutil: Path | None, path: Path) -> tuple[str | None, str | None, str | None]:
    """(GUID, age, error) for a candidate PDB; the error carries the probe exit and stderr."""
    if not pdbutil:
        return None, None, "llvm-pdbutil not found"
    completed = run(
        [str(pdbutil), "dump", "--summary", tool_operand(path)], timeout=PDB_PROBE_TIMEOUT
    )
    if completed.returncode != 0:
        return (
            None,
            None,
            (f"llvm-pdbutil exited {completed.returncode}: {tail_lines(completed.stderr)}"),
        )
    guid = age = None
    for line in (completed.stdout or "").splitlines():
        if match := _GUID.match(line):
            guid = _normalize_guid(match.group(1))
        elif match := _AGE.match(line):
            age = match.group(1)
    if not guid or age is None:
        return guid, age, "llvm-pdbutil reported no GUID/age for this candidate"
    return guid, age, None


def _probe_pdb(pdbutil: Path, path: Path, module_guid: str, module_age: int | None) -> PdbProbe:
    """Accept only a PDB whose GUID matches and whose age matches the image or its cache key."""
    probe = PdbProbe(path, "not_found", detail="not found")
    if not path.is_file():
        return probe
    try:
        guid, age, error = _pdb_signature(pdbutil, path)
    except (UprofError, OSError) as exc:
        probe.status, probe.detail = "probe_failed", str(exc)
        return probe
    probe.pdb_guid, probe.pdb_age = guid, age
    if error:
        probe.status, probe.detail = "probe_failed", error
        return probe
    if guid != module_guid:
        probe.status = "mismatch"
        probe.detail = (
            f"PDB GUID {guid} != image GUID {module_guid}; PDB age {age}, image age {module_age}"
        )
        return probe
    if age == str(module_age):
        probe.status = "accepted"
        return probe
    # Microsoft's symbol cache stores the file under <GUID><age> taken from the image, while the
    # PDB's own age can differ on this host (measured: ntdll image age 1, cached PDB age 4). The
    # key directory is then the authoritative age check; any other candidate must match exactly.
    key = f"{module_guid}{module_age:X}" if module_age is not None else None
    if key is not None and path.parent.name.upper() == key.upper():
        probe.status = "accepted"
        return probe
    probe.status = "mismatch"
    probe.detail = f"PDB age {age} != image age {module_age} (GUID matches)"
    return probe


def _nt_symbol_path() -> NtSymbolPath:
    """The _NT_SYMBOL_PATH local roots and URLs; only the local roots are ever read."""
    return parse_nt_symbol_path(os.environ.get("_NT_SYMBOL_PATH"))


# translate records the command it ran in session.uprof as space-joined, unquoted arguments.
_RECORDED_SYMBOL_CACHE = re.compile(r"--symbol-cache-dir\s+(.+?)(?=\s+--|\s*$)")


def _unique_dirs(paths: Iterable[str | Path]) -> list[Path]:
    result: dict[str, Path] = {}
    for path in paths:
        full = absolute(path)
        if full.is_dir():
            result.setdefault(os.path.normcase(os.fspath(full)), full)
    return list(result.values())


def symbol_stores(session: str | Path | None) -> list[Path]:
    """The symbol stores translate may have staged this session's PDBs into, besides srv* caches.

    translate stages a PDB as <store>/<pdb>/<GUID><age>/<pdb> under the --symbol-cache-dir it
    recorded in session.uprof, by default the user's symbol cache. Existing directories only.
    """
    try:
        meta = json.loads(read_text(Path(session) / "session.uprof")) if session else None
    except (UprofError, ValueError):
        meta = None
    command = str(meta.get("translateCommand") or "") if isinstance(meta, dict) else ""
    match = _RECORDED_SYMBOL_CACHE.search(command)
    return _unique_dirs([*([match.group(1)] if match else []), symbol_cache_dir()])


def _candidate_pdbs(
    exe: Path,
    pdb_name: str,
    module_guid: str | None,
    age: int | None,
    symbol_dirs: Iterable[str | Path] = (),
    symbol_files: Iterable[str | Path] = (),
    symbol_stores: Iterable[str | Path] = (),
    recorded_pdb: Path | None = None,
) -> list[Path]:
    module_name = exe.stem
    names = list(dict.fromkeys([pdb_name, module_name + ".pdb"]))
    candidates: list[Path] = []
    # Caller-supplied symbol files are the caller's choice, so they are probed first; search
    # roots come before the module's own directory and its keyed <GUID><age> subdirectory.
    candidates.extend(Path(path) for path in symbol_files)
    for directory in symbol_dirs:
        root = Path(directory)
        candidates.extend(root / name for name in names)
        if module_guid and age is not None:
            candidates.extend(root / f"{module_guid}{age:X}" / name for name in names)
    candidates.extend(exe.parent / name for name in names)
    # The PDB path the image's CodeView entry records, as llvm-symbolizer would try it next.
    if recorded_pdb is not None and recorded_pdb.is_absolute():
        candidates.append(recorded_pdb)
    nt_symbol_path = _nt_symbol_path()
    for directory in nt_symbol_path.dirs:
        candidates.extend(Path(directory) / name for name in names)
    # Stores in the <store>/<pdb>/<GUID><age>/<pdb> layout: _NT_SYMBOL_PATH local stores and
    # translate's stores always name their keyed path; a --symbol-path directory only once it
    # holds a <pdb> folder.
    stores = [Path(cache_root) for cache_root in nt_symbol_path.caches]
    stores += [Path(path) for path in symbol_stores]
    stores += [Path(path) for path in symbol_dirs if (Path(path) / pdb_name).is_dir()]
    seen: set[str] = set()
    for cache_root in stores:
        key = os.path.normcase(os.fspath(absolute(cache_root)))
        if key in seen:
            continue
        seen.add(key)
        cache_dir = cache_root / pdb_name
        if module_guid and age is not None:
            candidates.append(cache_dir / f"{module_guid}{age:X}" / pdb_name)
        try:
            discovered = list(cache_dir.rglob(pdb_name))
        except OSError:
            # An unreachable or unreadable cache path contributes nothing.
            continue
        candidates.extend(discovered)
    return candidates


def _choose_pdb(
    pdbutil: Path,
    exe_path: Path,
    pdb_name: str,
    module_guid: str,
    module_age: int | None,
    symbol_dirs: Iterable[str | Path] = (),
    symbol_files: Iterable[str | Path] = (),
    symbol_stores: Iterable[str | Path] = (),
    recorded_pdb: Path | None = None,
) -> PdbSearch:
    """Probe every candidate once; the search carries each rejection as evidence."""
    search = PdbSearch(urls=list(_nt_symbol_path().upstreams))
    for path in dict.fromkeys(
        _candidate_pdbs(
            exe_path,
            pdb_name,
            module_guid,
            module_age,
            symbol_dirs,
            symbol_files,
            symbol_stores,
            recorded_pdb,
        )
    ):
        probe = _probe_pdb(pdbutil, path, module_guid, module_age)
        search.probes.append(probe)
        if probe.status == "accepted":
            search.pdb = path
            return search
    return search


def _json_errors(text: str) -> list[tuple[str | None, str]]:
    """(address, message) for every llvm-symbolizer JSON Error object, wherever stdout carries one."""
    errors: list[tuple[str | None, str]] = []
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
            errors.append((record.get("Address"), message))
    return errors


def _symbol_record_stats(text: str) -> tuple[int, int]:
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


def _run_symbolizer(
    symbolizer: Path,
    exe: str,
    pdb: Path | None,
    rva_file: Path,
    out_file: Path,
    functions: str = "linkage",
    warn_failure: bool = True,
    debug_dirs: list[str] | tuple[str, ...] = (),
    fallback_paths: list[str] | tuple[str, ...] = (),
    stream: TextIO | None = None,
) -> SymbolizerRun:
    """Run one pass and keep its exit status and JSON Error objects as evidence.

    JSON output carries the resolution error on stdout, not stderr, so an exit 1 with records is
    a partial result whose Error messages name the missing or mismatched PDB. debug_dirs become
    llvm-symbolizer --debug-file-directory options and fallback_paths become
    --fallback-debug-path, which is how a caller supplies a separate DWARF/debug-file location.
    Warnings go to stream, which defaults to sys.stderr; a concurrent caller passes its own
    buffer so module text is replayed in order.
    """
    if stream is None:
        stream = sys.stderr
    command = [str(symbolizer), f"--obj={exe}"]
    if pdb:
        command.append(f"--pdb={pdb}")
    command += [f"--debug-file-directory={directory}" for directory in debug_dirs]
    command += [f"--fallback-debug-path={directory}" for directory in fallback_paths]
    command += [
        "--relative-address",
        f"--functions={functions}",
        "--inlines",
        "--output-style=JSON",
    ]
    completed = run(command, input_text=read_text(rva_file), timeout=4 * 60 * 60)
    records, empty_records = _symbol_record_stats(completed.stdout or "")
    run_result = SymbolizerRun(
        completed.returncode,
        _json_errors(completed.stdout or ""),
        completed.stderr or "",
        records,
        empty_records,
    )
    if (
        completed.returncode == 0
        and pdb is not None
        and records
        and empty_records == records
        and not run_result.errors
    ):
        run_result.errors.append(
            (
                None,
                (
                    f"llvm-symbolizer resolved no address ({records} empty Symbol entries); "
                    f"the PDB {pdb.name} is probably for another build"
                ),
            )
        )
        if warn_failure:
            print(f"WARNING: {exe}: {run_result.errors[-1][1]}", file=stream)
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        detail = (
            run_result.errors[0][1]
            if run_result.errors
            else tail_lines(run_result.stderr) or "no output"
        )
        raise UprofError(f"llvm-symbolizer exited {completed.returncode}: {detail}")
    if completed.returncode != 0 and warn_failure:
        reason = run_result.errors[0][1] if run_result.errors else tail_lines(run_result.stderr)
        suffix = f": {reason}" if reason else ""
        print(
            f"WARNING: llvm-symbolizer exited {completed.returncode} for {exe}{suffix}; the "
            "addresses it could not symbolize become '<no symbol>'",
            file=stream,
        )
    if completed.returncode == 0 and run_result.stderr.strip() and warn_failure:
        print(f"WARNING: llvm-symbolizer: {tail_lines(run_result.stderr)}", file=stream)
    with open(out_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(completed.stdout or "")
    return run_result


def _jsonl_row_count(path: Path) -> int:
    """Count newline-delimited records without parsing them."""
    rows = 0
    last = b"\n"
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            rows += block.count(b"\n")
            last = block[-1:]
    return rows + (0 if last == b"\n" else 1)


def _write_parquet(
    out_file: Path, parquet_file: Path, short_file: Path | None = None, stream: TextIO | None = None
) -> Path | None:
    if stream is None:
        stream = sys.stderr
    try:
        import duckdb
    except ImportError:
        print(
            f"WARNING: DuckDB not found; cannot write {parquet_file}, so symbolization fails",
            file=stream,
        )
        return None
    temp_file = parquet_file.with_name(parquet_file.name + ".tmp")
    temp_file.unlink(missing_ok=True)
    # Frame 0 is the innermost (executed) position and supplies function, file and line. The last
    # frame is the containing function, which is what uProf's Function.id names. The short-name
    # pass gives every function one identity spelling: an inlined frame carries only that, while
    # the out-of-line copy carries a demangled signature such as 'mix_inline(int)'.
    has_short = short_file is not None and short_file.is_file() and short_file.stat().st_size > 0
    if has_short:
        sql = """
COPY (
  SELECT sym.Address AS address,
         CASE WHEN json_type(sym.Symbol) = 'ARRAY' THEN
              json_extract_string(sym.Symbol, '$[0].FunctionName') END AS function,
         short.function_short,
         CASE WHEN json_type(sym.Symbol) = 'ARRAY' THEN
              json_extract_string(sym.Symbol, '$[0].FileName') END AS file,
         CASE WHEN json_type(sym.Symbol) = 'ARRAY' THEN
              CAST(json_extract_string(sym.Symbol, '$[0].Line') AS INTEGER) END AS line,
         CASE WHEN json_type(sym.Symbol) = 'ARRAY' THEN
              json_extract_string(sym.Symbol, '$[' || (json_array_length(sym.Symbol) - 1) || '].FunctionName') END AS containing_function
  FROM read_json(?, columns = {'Address': 'VARCHAR', 'Symbol': 'JSON'}, format = 'newline_delimited') AS sym
  LEFT JOIN (
    SELECT Address,
           CASE WHEN json_type(Symbol) = 'ARRAY' THEN
                json_extract_string(Symbol, '$[0].FunctionName') END AS function_short
    FROM read_json(?, columns = {'Address': 'VARCHAR', 'Symbol': 'JSON'}, format = 'newline_delimited')
  ) AS short USING (Address)
) TO ? (FORMAT parquet);
"""
        parameters = [str(absolute(out_file)), str(absolute(short_file)), str(absolute(temp_file))]
    else:
        sql = """
COPY (
  SELECT Address AS address,
         CASE WHEN json_type(Symbol) = 'ARRAY' THEN
              json_extract_string(Symbol, '$[0].FunctionName') END AS function,
         NULL::VARCHAR AS function_short,
         CASE WHEN json_type(Symbol) = 'ARRAY' THEN
              json_extract_string(Symbol, '$[0].FileName') END AS file,
         CASE WHEN json_type(Symbol) = 'ARRAY' THEN
              CAST(json_extract_string(Symbol, '$[0].Line') AS INTEGER) END AS line,
         CASE WHEN json_type(Symbol) = 'ARRAY' THEN
              json_extract_string(Symbol, '$[' || (json_array_length(Symbol) - 1) || '].FunctionName') END AS containing_function
  FROM read_json(?, columns = {'Address': 'VARCHAR', 'Symbol': 'JSON'}, format = 'newline_delimited')
) TO ? (FORMAT parquet);
"""
        parameters = [str(absolute(out_file)), str(absolute(temp_file))]
    try:
        with uprof_db.memory_connection() as connection:
            # One thread for a small write: it avoids starting a full pool in every symbolization
            # worker. A large write keeps the default because the pool then pays for itself.
            try:
                small = _jsonl_row_count(out_file) <= PARQUET_SINGLE_THREAD_MAX_ROWS
            except OSError:
                small = False
            if small:
                connection.execute("SET threads = 1")
            connection.execute(sql, parameters)
        os.replace(temp_file, parquet_file)
        return parquet_file
    except (duckdb.Error, OSError) as exc:
        with contextlib.suppress(OSError):
            temp_file.unlink()
        print(f"WARNING: could not write {parquet_file}; symbolization fails ({exc})", file=stream)
        return None


# --------------------------------------------------------------- public symbols


def _sections(lines: list[str]) -> dict[int, int]:
    sections: dict[int, int] = {}
    current = 0
    for line in lines:
        match = _SECTION_HEADER.search(line)
        if match:
            current = int(match.group(1))
            continue
        if current and (address := _SECTION_ADDRESS.match(line)):
            sections[current] = int(address.group(1), 16)
            current = 0
    if not sections:
        raise UprofError("no section headers parsed; pass a section-headers dump or a PDB")
    return sections


def _pairs(lines: list[str], sections: dict[int, int]) -> list[tuple[int, str]]:
    """Public function starts as (RVA, name); S_PUB32 addresses are (section, decimal offset)."""
    pairs: list[tuple[int, str]] = []
    pending = None
    for line in lines:
        match = _PUBLIC_NAME.search(line)
        if match:
            pending = match.group(1)
            continue
        if not pending:
            continue
        address = _PUBLIC_ADDRESS.search(line)
        if not address:
            continue
        flags, section_text, offset_text = address.groups()
        name, pending = pending, None
        if "function" not in flags.lower():
            continue
        section = int(section_text)
        if section not in sections:
            continue
        pairs.append((sections[section] + int(offset_text, 10), name))
    return pairs


def _write_pairs(path: Path, pairs: list[tuple[int, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["rva", "name"])
        writer.writerows(pairs)


class _PublicsCache:
    """Run-scoped reuse for the per-module public-symbol enumeration.

    One scratch directory serves every module of one symbolization run, and a PDB already
    enumerated in the run is copied instead of enumerated again. symbolize_modules resets
    the cache at both ends, so nothing survives the run.

    The module loop runs on a thread pool, so every call gets its own scratch file and the
    memo is guarded by a lock. A PDB enumerated concurrently by two threads is written twice;
    both results are valid and the last one is kept.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._directory: tempfile.TemporaryDirectory | None = None
        self._results: dict[Path, tuple[int, int]] = {}
        self._outputs: dict[Path, Path] = {}
        self._counter = 0

    def pairs_path(self) -> Path:
        """A scratch file no other concurrent enumeration writes to."""
        with self._lock:
            if self._directory is None:
                self._directory = tempfile.TemporaryDirectory(prefix="uprof-publics-")
            self._counter += 1
            return Path(self._directory.name) / f"pairs-{self._counter}.csv"

    def cached(self, pdb: Path, out_path: Path) -> tuple[int, int] | None:
        """Copy a PDB's result already written this run; None when it was not seen."""
        with self._lock:
            source = self._outputs.get(pdb)
            result = self._results.get(pdb)
        if source is None:
            return None
        try:
            shutil.copyfile(source, out_path)
        except OSError:
            # The scratch file vanished or the target is unwritable; re-enumerate instead of
            # failing the profile for a cache-copy problem.
            return None
        return result

    def remember(self, pdb: Path, out_path: Path, result: tuple[int, int]) -> None:
        with self._lock:
            self._outputs[pdb] = out_path
            self._results[pdb] = result

    def reset(self) -> None:
        # Only the bracketing caller resets, and only after every worker has joined.
        with self._lock:
            directory, self._directory = self._directory, None
            self._results.clear()
            self._outputs.clear()
            self._counter = 0
        if directory is not None:
            with contextlib.suppress(Exception):
                directory.cleanup()


_PUBLICS_CACHE = _PublicsCache()


def _aggregate_publics(pairs_path: Path, out_path: Path) -> tuple[int, int]:
    """Group names by RVA in memory; return (starts, starts with more than one name).

    The pairs file is the caller's CSV of (rva, name). A name reads as NULL when its field is
    empty, and NULL names sort after named ones and are left out of the label, which is what
    the CSV/SQL path this replaces produced.
    """
    by_rva: dict[int, list[str | None]] = {}
    try:
        with open(pairs_path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)
            for rva_text, name in reader:
                by_rva.setdefault(int(rva_text), []).append(name or None)
        with open(out_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["rva", "name_count", "names"])
            for rva in sorted(by_rva):
                names = by_rva[rva]
                ordered = sorted(names, key=lambda value: (value is None, value or ""))
                label = " | ".join(value for value in ordered[:4] if value is not None)
                if len(ordered) > 4 and label:
                    label += f" | ... (+{len(ordered) - 4} more)"
                writer.writerow([rva, len(names), label])
    except (OSError, ValueError, csv.Error) as exc:
        raise UprofError(f"cannot aggregate public symbols: {exc}") from exc
    starts = len(by_rva)
    shared = sum(1 for names in by_rva.values() if len(names) > 1)
    return starts, shared


def public_symbols(
    out_file: str | Path, pdb_file: str | Path, pdbutil: str | Path
) -> tuple[int, int]:
    """Write the RVA -> names cache; return (starts, shared starts)."""
    out_path = absolute(out_file)
    if not out_path.parent.is_dir():
        raise UprofError(f"output directory not found: {out_path.parent}")
    if not pdb_file:
        raise UprofError("PDB path is required")
    pdb_path = require_file(pdb_file, "PDB")
    pdbutil_path = absolute(pdbutil)
    if not pdbutil_path.is_file():
        raise UprofError("llvm-pdbutil not found; install LLVM")

    cached = _PUBLICS_CACHE.cached(pdb_path, out_path)
    if cached is not None:
        return cached

    # One invocation supplies both records; the run-scoped cache gives each call its own pairs
    # file in the shared scratch directory.
    # A timeout raises, which the caller reports as skipped fold labelling.
    combined = run(
        [str(pdbutil_path), "dump", "-section-headers", "-publics", tool_operand(pdb_path)],
        timeout=PDB_PROBE_TIMEOUT,
    )
    if combined.returncode != 0:
        detail = tail_lines((combined.stdout or "") + "\n" + (combined.stderr or ""))
        raise UprofError(f"llvm-pdbutil exited {combined.returncode}: {detail or 'no output'}")
    stdout = combined.stdout or ""
    if not _PUBLICS_MARKER.search(stdout):
        raise UprofError("llvm-pdbutil emitted no Public Symbols section")
    lines = stdout.splitlines()
    pairs = _pairs(lines, _sections(lines))
    pairs_path = _PUBLICS_CACHE.pairs_path()
    _write_pairs(pairs_path, pairs)
    result = _aggregate_publics(pairs_path, out_path)
    _PUBLICS_CACHE.remember(pdb_path, out_path, result)
    return result


def _publics(pdb: Path, out_file: Path, symbolizer: Path, stream: TextIO) -> Path | None:
    publics_file = out_file.with_suffix(".publics.csv")
    pdbutil = find_sibling(symbolizer, "llvm-pdbutil")
    if not pdbutil:
        print(
            "FOLDS: skipped (llvm-pdbutil not found; install LLVM to label folded addresses)",
            file=stream,
        )
        return None
    try:
        starts, shared = public_symbols(publics_file, pdb_file=pdb, pdbutil=pdbutil)
    except UprofError as exc:
        print(
            f"WARNING: public-symbol enumeration failed; fold labelling unavailable ({exc})",
            file=stream,
        )
        print("FOLDS: skipped (enumeration failed)", file=stream)
        return None
    print(f"FOLDS: {shared} shared starts of {starts} in {publics_file}", file=stream)
    return publics_file


# ---------------------------------------------------------------- symbolization


def _install_remedy(symbolizer_path: Path, tool: str) -> str:
    return (
        f"install {tool} beside {symbolizer_path} (from the same LLVM release), then re-run "
        "profile; a third-party module with no symbols is a finding, not a blocker"
    )


def symbolize_module(
    exe: str,
    rva_file: str | Path,
    out_dir: str | Path,
    stream: TextIO = sys.stdout,
    module_name: str | None = None,
    found_debug_info: bool | None = None,
    symbol_files: Iterable[str | Path] = (),
    symbol_dirs: Iterable[str | Path] = (),
    symbol_stores: Iterable[str | Path] = (),
) -> SymbolizedModule:
    """Symbolize the RVAs into out_dir; return the artifacts and the symbol-search evidence.

    The symbol file is found from the module's debug directory: each explicit symbol file, each
    symbol directory, the module's directory or a _NT_SYMBOL_PATH cache, and the chosen PDB is
    passed to llvm-symbolizer as its explicit symbol input. A symbol entry is probed by the
    caller: a file is an explicit symbol/debug file; a directory is a search root and is also
    passed to llvm-symbolizer as a debug-file search directory. When no PDB is accepted the
    symbolizer still runs once, so its JSON Error object names the missing file instead of a
    synthetic "GUID/age mismatch".
    """
    exe_input = exe  # llvm-symbolizer records this spelling as ModuleName
    exe_path = require_file(exe, "module")
    rva_path = require_file(rva_file, "RVA file")
    out_path = absolute(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    jsonl_file = out_path / "symbols.jsonl"
    parquet_file = out_path / "symbols.parquet"
    publics_file = out_path / "symbols.publics.csv"
    symbolizer_path = require_symbolizer()
    evidence = ModuleEvidence(module_name or exe, exe, found_debug_info=found_debug_info)
    pdb: Path | None = None
    pdb_files = [Path(path) for path in symbol_files if Path(path).suffix.lower() == ".pdb"]
    search_dirs = [Path(path) for path in symbol_dirs]
    debug_dirs, fallback_paths = symbolizer_search_paths(symbol_files, search_dirs)

    if os.name == "nt":
        readobj = find_sibling(symbolizer_path, "llvm-readobj")
        pdbutil = find_sibling(symbolizer_path, "llvm-pdbutil")
        readobj_error = None
        try:
            pdb_name, module_guid, module_age, recorded = _readobj_info(exe_input, readobj)
        except (UprofError, OSError) as exc:
            pdb_name = Path(exe_input).stem + ".pdb"
            module_guid = module_age = recorded = None
            readobj_error = str(exc)
        evidence.image_guid, evidence.image_age = module_guid, module_age
        recorded_pdb = Path(recorded) if recorded else None
        # Only a PDB whose GUID and age match the image is used. Without the image's identity or
        # without llvm-pdbutil a PDB could be picked only by name, and a stale build's PDB of the
        # same name gives plausible but wrong names.
        image_remedy = (
            "pass profile --image-path <file|dir> naming an image of the captured "
            "build that records a PDB identity (linked with /DEBUG), then re-run "
            "profile; a third-party module with no symbols is a finding, not a "
            "blocker"
        )
        if module_guid is None:
            if readobj is None:
                evidence.note = "llvm-readobj not found beside llvm-symbolizer or on PATH"
                evidence.remedy = _install_remedy(symbolizer_path, "llvm-readobj")
            elif readobj_error:
                evidence.note = readobj_error
                evidence.remedy = image_remedy
            else:
                # No PDB can serve this image, but DWARF or a separate debug file still can, so
                # the default remedy stays unless a --symbol-path PDB was refused below.
                evidence.note = "the image's debug directory records no PDB"
            evidence.outcome = "no_debug_info" if found_debug_info is False else "readobj_failed"
            if pdb_files:
                shown = ", ".join(os.fspath(path) for path in pdb_files)
                evidence.outcome = "no_pdb"
                evidence.note = (
                    f"the image records no PDB identity ({evidence.note}), so "
                    f"--symbol-path {shown} cannot be verified and is not used"
                )
                evidence.remedy = evidence.remedy or image_remedy
            pdb = None
        elif pdbutil:
            evidence.search = _choose_pdb(
                pdbutil,
                exe_path,
                pdb_name,
                module_guid,
                module_age,
                search_dirs,
                pdb_files,
                symbol_stores,
                recorded_pdb=recorded_pdb,
            )
            pdb = evidence.search.pdb
        else:
            probes = [
                PdbProbe(
                    path,
                    "unverified" if path.is_file() else "not_found",
                    detail="not used: no llvm-pdbutil to verify its GUID" if path.is_file() else "",
                )
                for path in dict.fromkeys(
                    _candidate_pdbs(
                        exe_path,
                        pdb_name,
                        module_guid,
                        module_age,
                        search_dirs,
                        pdb_files,
                        symbol_stores,
                        recorded_pdb,
                    )
                )
            ]
            evidence.search = PdbSearch(probes=probes)
            pdb = None
            evidence.outcome = "no_pdb"
            evidence.note = (
                "llvm-pdbutil not found beside llvm-symbolizer or on PATH, so no PDB "
                "can be GUID-verified and none is used"
            )
            evidence.remedy = _install_remedy(symbolizer_path, "llvm-pdbutil")
        if not pdb:
            if evidence.remedy is None and module_guid is not None:
                evidence.outcome = "no_debug_info" if found_debug_info is False else "no_pdb"
            # Without --pdb, llvm-symbolizer still loads the PDB the image's CodeView entry
            # names (at the recorded path or beside the image) with no GUID check. So it runs
            # only when llvm-readobj showed that the image records no PDB; then DWARF and the
            # COFF symbol table are its only sources.
            if readobj is not None and readobj_error is None and module_guid is None:
                evidence.symbolizer = _run_symbolizer(
                    symbolizer_path,
                    exe_input,
                    None,
                    rva_path,
                    jsonl_file,
                    warn_failure=False,
                    debug_dirs=debug_dirs,
                    fallback_paths=fallback_paths,
                    stream=stream,
                )
            else:
                jsonl_file.write_text("", encoding="utf-8")
            print("PDB=none", file=stream)
            parquet = _write_parquet(jsonl_file, parquet_file, stream=stream)
            print("FOLDS: skipped (no PDB; no public symbols to enumerate)", file=stream)
            return SymbolizedModule(parquet, None, None, evidence)
        print("PDB_MATCH=yes", file=stream)
        evidence.pdb = str(pdb)

    evidence.symbolizer = _run_symbolizer(
        symbolizer_path,
        exe_input,
        pdb,
        rva_path,
        jsonl_file,
        debug_dirs=debug_dirs,
        fallback_paths=fallback_paths,
        stream=stream,
    )
    if (
        evidence.symbolizer.returncode == 0
        and evidence.symbolizer.records
        and evidence.symbolizer.empty_records == evidence.symbolizer.records
        and evidence.symbolizer.errors
    ):
        # A wrong-build PDB is not reported by the exit status; keep its evidence on the outcome.
        evidence.outcome = "failed"
        evidence.note = evidence.symbolizer.errors[0][1]
    short_file = out_path / "symbols.short.jsonl"
    try:
        _run_symbolizer(
            symbolizer_path,
            exe_input,
            pdb,
            rva_path,
            short_file,
            debug_dirs=debug_dirs,
            fallback_paths=fallback_paths,
            functions="short",
            warn_failure=False,
            stream=stream,
        )
    except UprofError as exc:
        print(
            f"WARNING: llvm-symbolizer short-name pass failed; line function identity "
            f"may be less precise ({exc})",
            file=stream,
        )
        short_file = None
    parquet = _write_parquet(jsonl_file, parquet_file, short_file, stream=stream)
    if os.name == "nt" and pdb:
        publics = _publics(pdb, jsonl_file, symbolizer_path, stream)
    else:
        publics = None
        print(
            "FOLDS: skipped (DWARF symbols; PDB public-symbol fold detection is "
            "Windows-only, unverified)",
            file=stream,
        )
    return SymbolizedModule(parquet, publics, pdb, evidence)


# ------------------------------------------------------------- whole-scope symbolization


def _forward_slash(path: Path) -> str:
    """The spelling read_csv reports as its filename, independent of the platform separator."""
    return str(path).replace("\\", "/")


def _combine_artifacts(
    parquet_inputs: list[tuple[str, Path]], public_inputs: list[tuple[str, Path]], out_dir: Path
) -> tuple[Path, Path | None]:
    """Write one symbols.parquet and, when any module has publics, one symbols.publics.csv."""
    try:
        import duckdb
    except ImportError as exc:
        raise UprofError(
            "the DuckDB Python package is not installed; install it with: "
            f"{sys.executable} -m pip install duckdb"
        ) from exc
    symbols_file = out_dir / "symbols.parquet"
    publics_file = out_dir / "symbols.publics.csv"
    try:
        with uprof_db.memory_connection() as connection:
            connection.execute(
                "CREATE TEMP TABLE combined(module_name VARCHAR, address VARCHAR, "
                "function VARCHAR, function_short VARCHAR, "
                "file VARCHAR, line INTEGER, containing_function VARCHAR)"
            )
            for module_name, parquet in parquet_inputs:
                connection.execute(
                    "INSERT INTO combined SELECT CAST(? AS VARCHAR), address, function, "
                    "function_short, file, line, containing_function "
                    "FROM read_parquet(?)",
                    [module_name, str(parquet)],
                )
            connection.execute("COPY combined TO ? (FORMAT parquet)", [str(symbols_file)])
            if public_inputs:
                connection.execute(
                    "CREATE TEMP TABLE public_files(module_name VARCHAR, path VARCHAR, "
                    "position BIGINT)"
                )
                # Forward-slash spellings are what read_csv reports as filename, so the join
                # cannot miss a file because of the platform's separator.
                paths = [_forward_slash(path) for _module_name, path in public_inputs]
                connection.executemany(
                    "INSERT INTO public_files VALUES (?, ?, ?)",
                    [
                        (module_name, paths[position], position)
                        for position, (module_name, _path) in enumerate(public_inputs)
                    ],
                )
                connection.execute(
                    """
                    COPY (
                      SELECT f.module_name, c.rva,
                             CAST(c.name_count AS INTEGER) AS name_count, c.names
                      FROM read_csv(?, header = true, all_varchar = true, filename = true) c
                      JOIN public_files f ON f.path = c.filename
                      ORDER BY f.position, CAST(c.rva AS BIGINT)
                    ) TO ? (FORMAT csv, HEADER)
                """,
                    [paths, str(publics_file)],
                )
    except (duckdb.Error, OSError) as exc:
        raise UprofError(f"cannot combine the symbol artifacts in {out_dir}: {exc}") from exc
    return symbols_file, (publics_file if public_inputs else None)


def _probe_text(probe: PdbProbe) -> str:
    return f"{probe.path} [{probe.detail or probe.status}]"


def _evidence_line(evidence: ModuleEvidence, stream: TextIO) -> None:
    """One line per module with the image identity, every location searched and the remedy."""
    if evidence.outcome == "resolved":
        return
    facts = [
        f"image GUID {evidence.image_guid or 'unknown'} "
        f"age {evidence.image_age if evidence.image_age is not None else 'unknown'}",
        "debug info recorded: "
        + (
            "no"
            if evidence.found_debug_info is False
            else "yes"
            if evidence.found_debug_info
            else "unknown"
        ),
    ]
    if evidence.note:
        facts.append(evidence.note)
    if evidence.symbolizer and evidence.symbolizer.errors:
        address, message = evidence.symbolizer.errors[0]
        prefix = f"llvm-symbolizer exited {evidence.symbolizer.returncode}"
        facts.append(f"{prefix}: {message}" + (f" (address {address})" if address else ""))
    elif evidence.symbolizer and evidence.symbolizer.returncode != 0:
        facts.append(
            f"llvm-symbolizer exited {evidence.symbolizer.returncode}: "
            f"{tail_lines(evidence.symbolizer.stderr) or 'no stderr'}"
        )
    if evidence.search:
        if evidence.search.probes:
            facts.append(
                "searched: " + "; ".join(_probe_text(probe) for probe in evidence.search.probes)
            )
        if evidence.search.urls:
            facts.append("symbol-server URLs not consulted: " + ", ".join(evidence.search.urls))
    if evidence.outcome == "no_pdb" and evidence.image_guid and evidence.search:
        # The searched paths are concrete targets; the module's identity names the file needed.
        remedy = (
            f"copy the PDB with GUID {evidence.image_guid} age {evidence.image_age} to one "
            "of the searched paths, or re-run profile with --symbol-path set to the "
            "directory or file holding it (translate --symbol-server stages it from a "
            "symbol server into the symbol cache profile searches); a third-party module with no symbols "
            "is a finding, not a blocker"
        )
    else:
        remedy = (
            "pass profile --symbol-path <file|dir> for a symbol or debug file or a search "
            "root, --image-path <file|dir> when the module image is not at its recorded "
            "path, or place the matching debug file beside the module or in a local srv* "
            "cache (translate --symbol-path/--symbol-cache/--symbol-server can stage it), "
            "then re-run profile; a third-party module with no symbols is a finding, not a "
            "blocker"
        )
    if evidence.remedy:
        remedy = evidence.remedy
    if evidence.toolchain:
        remedy = None
    if evidence.outcome == "wrong_build":
        # The debug directory of another build is not evidence for the captured one.
        facts = [evidence.note or "the image is a different build than the capture recorded"]
        remedy = (
            "pass profile --image-path <file|dir> naming the captured build (from that "
            "build's output or a symbol server), then re-run profile; or run profile --by "
            "module, which needs no image"
        )
    print(
        f"WARNING: no symbols resolved for {evidence.module_name} "
        f"({evidence.module_path}): {'; '.join(facts)}" + (f"; {remedy}" if remedy else ""),
        file=stream,
    )


def _write_pdb_manifest(inputs: list[tuple[str, str]], out_dir: Path) -> Path:
    """Write the module -> PDB choice as CSV so the instruction context reuses it."""
    path = out_dir / "symbols.pdbs.csv"
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["module_name", "pdb"])
            writer.writerows(inputs)
    except OSError as exc:
        raise UprofError(
            f"cannot write the PDB manifest {path}: {exc}; check free space and "
            "permissions on the artifact directory"
        ) from exc
    return path


@dataclass
class _ModuleSymbolization:
    """One module's buffered stream text and artifacts, replayed in module order by the caller.

    output holds the MODULE, PDB, FOLDS and warning lines until every earlier module finished, so
    concurrent workers cannot reorder them. error is the exception the sequential loop would have
    raised at this module; the caller writes output first, then re-raises it.
    """

    module_name: str
    output: str
    evidence: ModuleEvidence | None = None
    parquet: Path | None = None
    publics: Path | None = None
    pdb: str | None = None
    error: Exception | None = None


def _symbolize_worker(
    index: int,
    module_name: str,
    module_path: str,
    rvas: list[str],
    out_path: Path,
    debug_info: dict[str, bool] | None,
    symbol_files: Iterable[str | Path],
    symbol_dirs: Iterable[str | Path],
    image_files: Iterable[str | Path],
    image_dirs: Iterable[str | Path],
    identity: tuple[object, object] | None = None,
    symbol_stores: Iterable[str | Path] = (),
) -> _ModuleSymbolization:
    """Symbolize one module into its own directory; return its text instead of writing it.

    identity is the capture's (Module.checksum, Module.size); an image that disagrees is
    another build and is not symbolized.
    """
    output = io.StringIO()
    evidence: ModuleEvidence | None = None
    try:
        recorded_path = module_path
        resolved, _source = resolve_image(
            module_name, module_path, tuple(image_files), tuple(image_dirs)
        )
        if resolved is None:
            basename = Path(module_path).name if module_path else Path(module_name).name
            if not module_path:
                note = (
                    "the capture records no path for this module and no --image-path image "
                    f"matches {basename}"
                )
            else:
                note = (
                    f"{module_path} is not on this host and no --image-path image matches "
                    f"{basename}"
                )
            evidence = ModuleEvidence(
                module_name,
                recorded_path,
                found_debug_info=(debug_info or {}).get(module_name),
                outcome="module_missing",
                note=note,
            )
            _evidence_line(evidence, output)
            return _ModuleSymbolization(module_name, output.getvalue(), evidence=evidence)
        module_path = os.fspath(resolved)
        if identity is not None:
            mismatch = image_build_mismatch(
                module_path,
                identity[0],
                identity[1],
                find_sibling(require_symbolizer(), "llvm-readobj"),
            )
            if mismatch:
                evidence = ModuleEvidence(
                    module_name,
                    module_path,
                    found_debug_info=(debug_info or {}).get(module_name),
                    outcome="wrong_build",
                    note=mismatch,
                )
                _evidence_line(evidence, output)
                return _ModuleSymbolization(module_name, output.getvalue(), evidence=evidence)
        module_dir = out_path / f"module-{index:04d}"
        rva_file = module_dir / "rvas.txt"
        try:
            module_dir.mkdir()
            with open(rva_file, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(rvas) + "\n")
        except OSError as exc:
            raise UprofError(
                f"cannot write the symbol work files in {module_dir}: {exc}; "
                "check free space and permissions on the artifact directory"
            ) from exc
        print(f"MODULE: {module_name}: {len(rvas)} addresses in {module_path}", file=output)
        try:
            result = symbolize_module(
                module_path,
                rva_file,
                module_dir,
                stream=output,
                module_name=module_name,
                found_debug_info=(debug_info or {}).get(module_name),
                symbol_files=symbol_files,
                symbol_dirs=symbol_dirs,
                symbol_stores=symbol_stores,
            )
        except UprofError as exc:
            evidence = ModuleEvidence(
                module_name,
                module_path,
                found_debug_info=(debug_info or {}).get(module_name),
                outcome="failed",
                note=str(exc),
                toolchain=isinstance(exc, SymbolizerUnsupported),
            )
            _evidence_line(evidence, output)
            return _ModuleSymbolization(module_name, output.getvalue(), evidence=evidence)
        evidence = result.evidence
        _evidence_line(evidence, output)
        if result.parquet is None:
            raise UprofError(f"cannot write the symbol artifact for {module_path} in {module_dir}")
        return _ModuleSymbolization(
            module_name,
            output.getvalue(),
            evidence=evidence,
            parquet=result.parquet,
            publics=result.publics,
            pdb=str(result.pdb) if result.pdb else None,
        )
    except Exception as exc:
        return _ModuleSymbolization(module_name, output.getvalue(), evidence=evidence, error=exc)


def _symbolize_tasks(
    modules: list[tuple[str, str, list[str]]],
    out_path: Path,
    stream: TextIO,
    debug_info: dict[str, bool] | None,
    symbol_files: Iterable[str | Path],
    symbol_dirs: Iterable[str | Path],
    image_files: Iterable[str | Path],
    image_dirs: Iterable[str | Path],
    identities: dict[str, tuple[object, object]] | None = None,
    symbol_stores: Iterable[str | Path] = (),
) -> list[_ModuleSymbolization]:
    """Symbolize modules in a bounded pool; write each module's text in module index order."""
    symbol_files, symbol_dirs = tuple(symbol_files), tuple(symbol_dirs)
    image_files, image_dirs = tuple(image_files), tuple(image_dirs)
    symbol_stores = tuple(symbol_stores)
    workers = max(1, min(len(modules), MAX_SYMBOLIZATION_WORKERS, os.cpu_count() or 1))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="uprof-symbol")
    try:
        futures = [
            pool.submit(
                _symbolize_worker,
                index,
                module_name,
                module_path,
                rvas,
                out_path,
                debug_info,
                symbol_files,
                symbol_dirs,
                image_files,
                image_dirs,
                (identities or {}).get(module_name),
                symbol_stores,
            )
            for index, (module_name, module_path, rvas) in enumerate(modules)
        ]
        outcomes: list[_ModuleSymbolization] = []
        for future in futures:
            outcome = future.result()
            stream.write(outcome.output)
            if outcome.error is not None:
                raise outcome.error
            outcomes.append(outcome)
        return outcomes
    finally:
        # A failed module stops replay: shutdown(wait=True) awaits the workers already running
        # and cancels the ones still queued.
        pool.shutdown(wait=True, cancel_futures=True)


def _warn_unused_pdbs(symbol_files: Iterable[str | Path], used: list[str], stream: TextIO) -> None:
    """One warning per --symbol-path PDB that no module used, so it is not silently ignored."""
    if os.name == "nt":
        cause = (
            "it matches no profiled module's PDB name and GUID; pass the PDB of a profiled "
            "module, or drop it"
        )
    else:
        cause = (
            "PDBs are read only on Windows hosts; profile this capture on Windows with the "
            "same --symbol-path"
        )
    for path in symbol_files:
        if Path(path).suffix.lower() != ".pdb" or any(same_path(path, pdb) for pdb in used):
            continue
        print(
            f"WARNING: --symbol-path {os.fspath(path)} was used for no module: {cause}", file=stream
        )


def symbolize_modules(
    modules: list[tuple[str, str, list[str]]],
    out_dir: str | Path,
    stream: TextIO = sys.stdout,
    debug_info: dict[str, bool] | None = None,
    symbol_files: Iterable[str | Path] = (),
    symbol_dirs: Iterable[str | Path] = (),
    image_files: Iterable[str | Path] = (),
    image_dirs: Iterable[str | Path] = (),
    identities: dict[str, tuple[object, object]] | None = None,
    symbol_stores: Iterable[str | Path] = (),
) -> tuple[Path | None, Path | None, Path | None, list[ModuleEvidence]]:
    """Symbolize every module in scope and combine the artifacts.

    modules holds (module_name, binary_path, [rva hex text]) groups. debug_info maps a module key
    to Module.foundDebugInfo so the evidence separates "no debug info recorded" from "symbol file
    missing". symbol_files are explicit symbol/debug files and symbol_dirs are search roots.
    image_files are explicit module images and image_dirs are searched by the module's basename;
    the caller probes --symbol-path and --image-path once with probe_paths. identities maps a
    module key to the capture's (Module.checksum, Module.size); an image that disagrees is
    another build and gets no names. symbol_stores are the stores translate staged into
    (symbol_stores()). Modules are symbolized
    concurrently, but their stream text and the combined artifacts keep module order. Returns
    (symbols.parquet, symbols.publics.csv, symbols.pdbs.csv, evidence); the parquet is None when
    no module could be symbolized, in which case the SQL reports every address as '<no symbol>'.
    """
    out_path = absolute(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    require_symbolizer()
    symbol_files = tuple(symbol_files)
    parquet_inputs: list[tuple[str, Path]] = []
    public_inputs: list[tuple[str, Path]] = []
    pdb_inputs: list[tuple[str, str]] = []
    evidence: list[ModuleEvidence] = []
    # The public-symbol scratch lives only while the modules are enumerated; a process that
    # profiles twice in a row must not reuse the first run's scratch or memo. The pool joins
    # every worker before _symbolize_tasks returns, so the finally cannot race a worker.
    _PUBLICS_CACHE.reset()
    try:
        outcomes = _symbolize_tasks(
            modules,
            out_path,
            stream,
            debug_info,
            symbol_files,
            symbol_dirs,
            image_files,
            image_dirs,
            identities,
            symbol_stores,
        )
        for outcome in outcomes:
            if outcome.evidence is not None:
                evidence.append(outcome.evidence)
            if outcome.parquet is not None:
                parquet_inputs.append((outcome.module_name, outcome.parquet))
            if outcome.publics:
                public_inputs.append((outcome.module_name, outcome.publics))
            if outcome.pdb:
                pdb_inputs.append((outcome.module_name, outcome.pdb))
    finally:
        _PUBLICS_CACHE.reset()
    _warn_unused_pdbs(symbol_files, [pdb for _name, pdb in pdb_inputs], stream)
    if not parquet_inputs:
        return None, None, None, evidence
    symbols_file, publics_file = _combine_artifacts(parquet_inputs, public_inputs, out_path)
    pdbs_file = _write_pdb_manifest(pdb_inputs, out_path) if pdb_inputs else None
    return symbols_file, publics_file, pdbs_file, evidence
