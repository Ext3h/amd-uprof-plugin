"""Addresses -> file:line plus the PDB public-symbol fold cache (Windows PDB / Linux DWARF)."""

from __future__ import annotations

import contextlib
import csv
import hashlib
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

from uprof_native import find_sibling, require_symbolizer, run, symbol_cache_dir
from uprof_util import UprofError, absolute, read_text, require_file, tail_lines

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

PDB_PROBE_TIMEOUT: Final = 5 * 60
# Module work is native-tool and DuckDB bound and releases the GIL, so a small fixed pool stops
# short of oversubscribing instead of scaling with the module count.
MAX_SYMBOLIZATION_WORKERS: Final = 8


def symbolizer_search_paths(symbol_files: Iterable[str | Path],
                            symbol_dirs: Iterable[str | Path]
                            ) -> tuple[list[str], list[str]]:
    """The debug-file search directories a symbol-path set supplies.

    Every search directory is both a primary and a fallback root, so one flag covers both
    symbolizer lookups. An explicit non-PDB symbol file contributes its directory, which is how a
    caller supplies a separate DWARF/debug file.
    """
    roots = [os.fspath(path) for path in symbol_dirs]
    parents = [os.fspath(Path(path).parent) for path in symbol_files
               if Path(path).suffix.lower() != ".pdb"]
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
        return tuple(Path(path) for path in symbolizer_search_paths(
            self.symbol_files, self.symbol_dirs)[0])


def _probe_paths(values: list[str] | tuple[str, ...] | None, option: str
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


def probe_paths(symbol_paths: list[str] | tuple[str, ...] | None = None,
                image_paths: list[str] | tuple[str, ...] | None = None) -> SourcePaths:
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


def resolve_image(module_name: str, module_path: str | None,
                  image_files: tuple[Path, ...] | list[Path] = (),
                  image_dirs: tuple[Path, ...] | list[Path] = ()
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
    status: str  # accepted, not_found, mismatch, probe_failed
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
    outcome: str = "resolved"  # resolved, no_debug_info, no_pdb, readobj_failed, module_missing, failed
    note: str | None = None


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


def _readobj_info(exe: str, readobj: Path | None) -> tuple[str, str | None, int | None]:
    pdb_name = Path(exe).stem + ".pdb"
    guid = age = None
    if not readobj:
        return pdb_name, guid, age
    # The debug directory belongs to the image bytes, so the probe result is keyed on the image
    # and readobj build and survives until either changes.
    image_id = artifact_file_identity(exe)
    readobj_id = artifact_file_identity(readobj)
    cache_key = None
    if artifact_identity_present(image_id) and artifact_identity_present(readobj_id):
        cache_key = artifact_key({"kind": "readobj", "image": image_id, "readobj": readobj_id})
        cached = load_cached_artifacts("readobj", cache_key, ("info.json",))
        if cached is not None:
            try:
                data = json.loads((cached[0] / "info.json").read_text(encoding="utf-8"))
                return (str(data["pdb_name"]),
                        None if data.get("guid") is None else str(data["guid"]),
                        None if data.get("age") is None else int(data["age"]))
            except (OSError, ValueError, KeyError, TypeError):
                pass
    completed = run([str(readobj), "--coff-debug-directory", exe],
                    timeout=PDB_PROBE_TIMEOUT)
    if completed.returncode != 0:
        raise UprofError(f"llvm-readobj exited {completed.returncode}: "
                         f"{tail_lines(completed.stderr)}")
    for line in (completed.stdout or "").splitlines():
        for key, pattern in _READOBJ.items():
            match = pattern.match(line)
            if not match:
                continue
            value = match.group(1).strip()
            if key == "name":
                pdb_name = Path(value).name
            elif key == "guid":
                guid = _normalize_guid(value)
            else:
                age = _as_int(value)
    if cache_key is not None:
        store_cached_text("readobj", cache_key, "info.json",
                          json.dumps({"pdb_name": pdb_name, "guid": guid, "age": age},
                                     sort_keys=True) + "\n")
    return pdb_name, guid, age


def _pdb_signature(pdbutil: Path | None, path: Path
                   ) -> tuple[str | None, str | None, str | None]:
    """(GUID, age, error) for a candidate PDB; the error carries the probe exit and stderr."""
    if not pdbutil:
        return None, None, "llvm-pdbutil not found"
    completed = run([str(pdbutil), "dump", "--summary", str(path)],
                    timeout=PDB_PROBE_TIMEOUT)
    if completed.returncode != 0:
        return None, None, (f"llvm-pdbutil exited {completed.returncode}: "
                            f"{tail_lines(completed.stderr)}")
    guid = age = None
    for line in (completed.stdout or "").splitlines():
        if match := _GUID.match(line):
            guid = _normalize_guid(match.group(1))
        elif match := _AGE.match(line):
            age = match.group(1)
    if not guid or age is None:
        return guid, age, "llvm-pdbutil reported no GUID/age for this candidate"
    return guid, age, None


def _probe_pdb(pdbutil: Path, path: Path, module_guid: str,
               module_age: int | None) -> PdbProbe:
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
        probe.detail = (f"PDB GUID {guid} != image GUID {module_guid}; "
                        f"PDB age {age}, image age {module_age}")
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


def _symbol_server_entries() -> list[tuple[Path, str]]:
    """(cache root, URL) for every _NT_SYMBOL_PATH srv entry; only the root is ever read."""
    entries: list[tuple[Path, str]] = []
    for entry in (os.environ.get("_NT_SYMBOL_PATH") or "").split(";"):
        parts = entry.split("*")
        if len(parts) < 3 or parts[0].strip().lower() != "srv":
            continue
        entries.append((Path(parts[1]), parts[2].strip()))
    return entries


def _candidate_pdbs(exe: Path, pdb_name: str, module_guid: str | None,
                    age: int | None, symbol_dirs: Iterable[str | Path] = (),
                    symbol_files: Iterable[str | Path] = ()) -> list[Path]:
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
    for cache_root, _url in _symbol_server_entries():
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


def _choose_pdb(pdbutil: Path, exe_path: Path, pdb_name: str, module_guid: str,
                module_age: int | None,
                symbol_dirs: Iterable[str | Path] = (),
                symbol_files: Iterable[str | Path] = ()) -> PdbSearch:
    """Probe every candidate once; the search carries each rejection as evidence.

    The result is cached on the pdbutil build and every candidate's identity, so a PDB that
    appears, disappears or is rebuilt changes the key. A tool-level probe failure is not stored.
    """
    candidates = list(dict.fromkeys(_candidate_pdbs(
        exe_path, pdb_name, module_guid, module_age, symbol_dirs, symbol_files)))
    urls = [url for _root, url in _symbol_server_entries()]
    pdbutil_id = artifact_file_identity(pdbutil)
    cache_key = None
    if artifact_identity_present(pdbutil_id):
        cache_key = artifact_key({
            "kind": "pdb-search",
            "pdbutil": pdbutil_id,
            "pdb_name": pdb_name,
            "guid": module_guid,
            "age": module_age,
            "symbol_dirs": [os.path.normcase(os.path.abspath(os.fspath(path)))
                            for path in symbol_dirs],
            "symbol_files": [os.path.normcase(os.path.abspath(os.fspath(path)))
                             for path in symbol_files],
            "urls": urls,
            "candidates": [[os.path.normcase(os.path.abspath(os.fspath(path))),
                            artifact_file_identity(path)] for path in candidates],
        })
        cached = load_cached_artifacts("pdb-search", cache_key, ("search.json",))
        if cached is not None:
            search = _search_from_data(_read_json(cached[0] / "search.json"))
            if search is not None:
                return search
    search = PdbSearch(urls=urls)
    for path in candidates:
        probe = _probe_pdb(pdbutil, path, module_guid, module_age)
        search.probes.append(probe)
        if probe.status == "accepted":
            search.pdb = path
            break
    if cache_key is not None and all(probe.status != "probe_failed" for probe in search.probes):
        store_cached_text("pdb-search", cache_key, "search.json",
                          json.dumps(_search_data(search), sort_keys=True) + "\n")
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
                not (frame.get("FunctionName") or "").strip()
                and not (frame.get("Line") or 0)
                for frame in frames if isinstance(frame, dict)):
            empty += 1
    return records, empty


def _run_symbolizer(symbolizer: Path, exe: str, pdb: Path | None, rva_file: Path,
                    out_file: Path, functions: str = "linkage",
                    warn_failure: bool = True,
                    debug_dirs: list[str] | tuple[str, ...] = (),
                    fallback_paths: list[str] | tuple[str, ...] = (),
                    stream: TextIO | None = None) -> SymbolizerRun:
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
    command += ["--relative-address", f"--functions={functions}", "--inlines",
                "--output-style=JSON"]
    completed = run(command, input_text=read_text(rva_file), timeout=4 * 60 * 60)
    records, empty_records = _symbol_record_stats(completed.stdout or "")
    run_result = SymbolizerRun(completed.returncode,
                               _json_errors(completed.stdout or ""),
                               completed.stderr or "", records, empty_records)
    if (completed.returncode == 0 and pdb is not None and records
            and empty_records == records and not run_result.errors):
        run_result.errors.append((None, (
            f"llvm-symbolizer resolved no address ({records} empty Symbol entries); "
            f"the PDB {pdb.name} is probably for another build")))
        if warn_failure:
            print(f"WARNING: {exe}: {run_result.errors[-1][1]}", file=stream)
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        detail = (run_result.errors[0][1] if run_result.errors
                  else tail_lines(run_result.stderr) or "no output")
        raise UprofError(f"llvm-symbolizer exited {completed.returncode}: {detail}")
    if completed.returncode != 0 and warn_failure:
        reason = (run_result.errors[0][1] if run_result.errors
                  else tail_lines(run_result.stderr))
        suffix = f": {reason}" if reason else ""
        print(f"WARNING: llvm-symbolizer exited {completed.returncode} for {exe}{suffix}; the "
              "addresses it could not symbolize become '<no symbol>'", file=stream)
    if completed.returncode == 0 and run_result.stderr.strip() and warn_failure:
        print(f"WARNING: llvm-symbolizer: {tail_lines(run_result.stderr)}", file=stream)
    with open(out_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(completed.stdout or "")
    return run_result


def _write_parquet(out_file: Path, parquet_file: Path,
                   short_file: Path | None = None, stream: TextIO | None = None) -> Path | None:
    if stream is None:
        stream = sys.stderr
    try:
        import duckdb
    except ImportError:
        print(f"WARNING: DuckDB not found; cannot write {parquet_file}, so symbolization fails",
              file=stream)
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
        with duckdb.connect() as connection:
            connection.execute(sql, parameters)
        os.replace(temp_file, parquet_file)
        return parquet_file
    except (duckdb.Error, OSError) as exc:
        with contextlib.suppress(OSError):
            temp_file.unlink()
        print(f"WARNING: could not write {parquet_file}; symbolization fails ({exc})",
              file=stream)
        return None

# ---------------------------------------------------------------- artifact cache

# Bump when a cached artifact's content or layout changes; other versions are ignored.
ARTIFACT_CACHE_FORMAT: Final = 1
_ARTIFACT_CACHE_DIR: Final = "uprof-profile-artifacts"


def _cache_root(kind: str) -> Path | None:
    """The per-user cache directory for one artifact kind, or None when it is unusable.

    It sits beside the symbol cache the tool already uses; cache I/O is best-effort and never
    fails a profile.
    """
    try:
        root = symbol_cache_dir() / _ARTIFACT_CACHE_DIR / f"v{ARTIFACT_CACHE_FORMAT}" / kind
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError:
        return None


def artifact_key(payload: object) -> str:
    """A stable cache key for a JSON-serializable description of every artifact input."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_file_identity(path: str | Path | None) -> dict[str, object]:
    """Cheap build identity for a file: normalized absolute path, size and mtime.

    A rebuilt file changes size or mtime; a missing path records that instead of a build.
    """
    if path is None:
        return {"path": None}
    resolved = os.path.normcase(os.path.abspath(os.fspath(path)))
    try:
        stat = os.stat(resolved)
    except OSError:
        return {"path": resolved, "missing": True}
    return {"path": resolved, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def artifact_file_digest(path: str | Path) -> str:
    """sha256 of a file's bytes, read in blocks so a large input never loads at once."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_identity_present(identity: dict[str, object]) -> bool:
    """True when the identity came from an existing file; only those may key a cache entry.

    This also keeps tests that substitute fake tool paths from touching the cache.
    """
    return identity.get("path") is not None and not identity.get("missing")


def load_cached_artifacts(kind: str, key: str,
                          names: Iterable[str]) -> tuple[Path, dict[str, object]] | None:
    """The directory and metadata of a complete cache entry, or None when it is absent.

    The metadata is written last, so its presence is what makes an entry readable.
    """
    root = _cache_root(kind)
    if root is None:
        return None
    directory = root / key[:2] / key
    try:
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("format") != ARTIFACT_CACHE_FORMAT:
        return None
    if any(not (directory / name).is_file() for name in names):
        return None
    return directory, meta


def _temporary_beside(directory: Path, name: str) -> Path:
    """An unused temp path in directory, unique to this writer."""
    handle, path = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=directory)
    os.close(handle)
    return Path(path)


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _write_cache_meta(directory: Path, meta: dict[str, object]) -> None:
    temporary = _temporary_beside(directory, "meta.json")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({**meta, "format": ARTIFACT_CACHE_FORMAT}, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, directory / "meta.json")
    except OSError:
        _remove_quietly(temporary)
        raise


def store_cached_artifacts(kind: str, key: str, files: dict[str, Path],
                           meta: dict[str, object] | None = None) -> None:
    """Copy finished artifacts into the cache, metadata last so a reader sees a whole entry.

    Concurrent writers to one key are safe: each store uses its own temp file and replaces
    whole artifacts; a failed store leaves no metadata, so a reader ignores the entry.
    """
    root = _cache_root(kind)
    if root is None:
        return
    directory = root / key[:2] / key
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for name, source in files.items():
            temporary = _temporary_beside(directory, name)
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, directory / name)
            except OSError:
                _remove_quietly(temporary)
                raise
        _write_cache_meta(directory, meta or {})
    except OSError:
        # Best-effort: recomputing next run is always correct, so a failed write is not an error.
        return


def store_cached_text(kind: str, key: str, name: str, text: str,
                      meta: dict[str, object] | None = None) -> None:
    """Store one small text artifact and its metadata; see store_cached_artifacts."""
    root = _cache_root(kind)
    if root is None:
        return
    directory = root / key[:2] / key
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_beside(directory, name)
        try:
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(temporary, directory / name)
        except OSError:
            _remove_quietly(temporary)
            raise
        _write_cache_meta(directory, meta or {})
    except OSError:
        # Best-effort: see store_cached_artifacts.
        return


def _symbolizer_run_data(value: SymbolizerRun | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"returncode": value.returncode,
            "errors": [[address, message] for address, message in value.errors],
            "stderr": value.stderr,
            "records": value.records,
            "empty_records": value.empty_records}


def _symbolizer_run_from_data(data: object) -> SymbolizerRun | None:
    if not isinstance(data, dict):
        return None
    errors: list[tuple[str | None, str]] = []
    for item in data.get("errors") or []:
        if isinstance(item, list) and len(item) == 2:
            errors.append((None if item[0] is None else str(item[0]), str(item[1])))
    return SymbolizerRun(int(data.get("returncode") or 0), errors,
                         str(data.get("stderr") or ""), int(data.get("records") or 0),
                         int(data.get("empty_records") or 0))


def _read_json(path: Path) -> object:
    """Parsed JSON, or None when the file is missing or malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _search_data(search: PdbSearch | None) -> dict[str, object] | None:
    if search is None:
        return None
    return {
        "pdb": str(search.pdb) if search.pdb else None,
        "probes": [{"path": str(probe.path), "status": probe.status,
                    "pdb_guid": probe.pdb_guid, "pdb_age": probe.pdb_age,
                    "detail": probe.detail} for probe in search.probes],
        "urls": list(search.urls),
    }


def _search_from_data(data: object) -> PdbSearch | None:
    if not isinstance(data, dict) or "probes" not in data:
        return None
    probes = []
    for item in data.get("probes") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        probes.append(PdbProbe(Path(str(item["path"])), str(item.get("status") or ""),
                               pdb_guid=(None if item.get("pdb_guid") is None
                                         else str(item["pdb_guid"])),
                               pdb_age=(None if item.get("pdb_age") is None
                                        else str(item["pdb_age"])),
                               detail=str(item.get("detail") or "")))
    return PdbSearch(pdb=Path(str(data["pdb"])) if data.get("pdb") else None,
                     probes=probes,
                     urls=[str(url) for url in data.get("urls") or []])


def _evidence_data(evidence: ModuleEvidence) -> dict[str, object]:
    """JSON form of the evidence, so a cached module replays the same outcome and cause text."""
    return {
        "module_name": evidence.module_name,
        "module_path": evidence.module_path,
        "found_debug_info": evidence.found_debug_info,
        "image_guid": evidence.image_guid,
        "image_age": evidence.image_age,
        "search": _search_data(evidence.search),
        "symbolizer": _symbolizer_run_data(evidence.symbolizer),
        "pdb": evidence.pdb,
        "outcome": evidence.outcome,
        "note": evidence.note,
    }


def _evidence_from_data(data: object) -> ModuleEvidence:
    if not isinstance(data, dict):
        return ModuleEvidence("", "", outcome="failed")
    age = data.get("image_age")
    found = data.get("found_debug_info")
    return ModuleEvidence(
        module_name=str(data.get("module_name") or ""),
        module_path=str(data.get("module_path") or ""),
        found_debug_info=(None if found is None else bool(found)),
        image_guid=(None if data.get("image_guid") is None else str(data["image_guid"])),
        image_age=(None if age is None else int(age)),
        search=_search_from_data(data.get("search")),
        symbolizer=_symbolizer_run_from_data(data.get("symbolizer")),
        pdb=(None if data.get("pdb") is None else str(data["pdb"])),
        outcome=str(data.get("outcome") or "failed"),
        note=(None if data.get("note") is None else str(data["note"])),
    )

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
        shutil.copyfile(source, out_path)
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
    """Group names by RVA in SQL; return (starts, starts with more than one name)."""
    try:
        import duckdb
    except ImportError as exc:
        raise UprofError(
            "the DuckDB Python package is not installed; install it with: "
            f"{sys.executable} -m pip install duckdb") from exc
    try:
        with duckdb.connect() as connection:
            connection.execute(
                "CREATE TEMP TABLE pairs AS "
                "SELECT CAST(rva AS BIGINT) AS rva, name "
                "FROM read_csv(?, header = true, all_varchar = true)", [str(pairs_path)])
            starts, shared = connection.execute(
                "SELECT count(*), count(*) FILTER (WHERE n > 1) "
                "FROM (SELECT rva, count(*) AS n FROM pairs GROUP BY rva)").fetchone()
            connection.execute("""
                COPY (
                  SELECT rva, count(*) AS name_count,
                         CASE WHEN count(*) > 4 THEN
                           array_to_string(list_slice(list_sort(list(name)), 1, 4), ' | ') ||
                           ' | ... (+' || CAST(count(*) - 4 AS VARCHAR) || ' more)'
                         ELSE array_to_string(list_sort(list(name)), ' | ') END AS names
                  FROM pairs GROUP BY rva ORDER BY rva
                ) TO ? (FORMAT csv, HEADER)
            """, [str(out_path)])
    except duckdb.Error as exc:
        raise UprofError(f"cannot aggregate public symbols: {exc}") from exc
    return int(starts), int(shared)

def public_symbols(out_file: str | Path, pdb_file: str | Path,
                   pdbutil: str | Path) -> tuple[int, int]:
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

    sections_run = run([str(pdbutil_path), "dump", "-section-headers", str(pdb_path)])
    publics_run = run([str(pdbutil_path), "dump", "-publics", str(pdb_path)])
    pairs = _pairs((publics_run.stdout or "").splitlines(),
                   _sections((sections_run.stdout or "").splitlines()))
    pairs_path = _PUBLICS_CACHE.pairs_path()
    _write_pairs(pairs_path, pairs)
    result = _aggregate_publics(pairs_path, out_path)
    _PUBLICS_CACHE.remember(pdb_path, out_path, result)
    return result


def _publics(pdb: Path, out_file: Path, symbolizer: Path, stream: TextIO) -> Path | None:
    publics_file = out_file.with_suffix(".publics.csv")
    pdbutil = find_sibling(symbolizer, "llvm-pdbutil")
    if not pdbutil:
        print("FOLDS: skipped (llvm-pdbutil not found; install LLVM to label folded addresses)",
              file=stream)
        return None
    # The public-symbol table belongs to the PDB alone, so its key carries no capture input and
    # one entry serves every capture that resolves the same PDB.
    pdb_id = artifact_file_identity(pdb)
    pdbutil_id = artifact_file_identity(pdbutil)
    cache_key = None
    if artifact_identity_present(pdb_id) and artifact_identity_present(pdbutil_id):
        cache_key = artifact_key({"kind": "publics", "pdb": pdb_id, "pdbutil": pdbutil_id})
        cached = load_cached_artifacts("publics", cache_key, ("publics.csv",))
        if cached is not None:
            directory, meta = cached
            try:
                shutil.copyfile(directory / "publics.csv", publics_file)
            except OSError:
                cached = None
            else:
                print(f"FOLDS: {int(meta.get('shared') or 0)} shared starts of "
                      f"{int(meta.get('starts') or 0)} in {publics_file}", file=stream)
                return publics_file
    try:
        starts, shared = public_symbols(publics_file, pdb_file=pdb, pdbutil=pdbutil)
    except UprofError as exc:
        print(f"WARNING: public-symbol enumeration failed; fold labelling unavailable ({exc})",
              file=stream)
        print("FOLDS: skipped (enumeration failed)", file=stream)
        return None
    if cache_key is not None:
        store_cached_artifacts("publics", cache_key, {"publics.csv": publics_file},
                               {"starts": starts, "shared": shared})
    print(f"FOLDS: {shared} shared starts of {starts} in {publics_file}", file=stream)
    return publics_file

# ---------------------------------------------------------------- symbolization

def symbolize_module(exe: str, rva_file: str | Path, out_dir: str | Path,
                     stream: TextIO = sys.stdout, module_name: str | None = None,
                     found_debug_info: bool | None = None,
                     symbol_files: Iterable[str | Path] = (),
                     symbol_dirs: Iterable[str | Path] = ()) -> SymbolizedModule:
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
    pdb_files = [Path(path) for path in symbol_files
                 if Path(path).suffix.lower() == ".pdb"]
    search_dirs = [Path(path) for path in symbol_dirs]
    debug_dirs, fallback_paths = symbolizer_search_paths(symbol_files, search_dirs)

    if os.name == "nt":
        readobj = find_sibling(symbolizer_path, "llvm-readobj")
        pdbutil = find_sibling(symbolizer_path, "llvm-pdbutil")
        readobj_error = None
        explicit = next((path for path in pdb_files if path.is_file()), None)
        try:
            pdb_name, module_guid, module_age = _readobj_info(exe_input, readobj)
        except (UprofError, OSError) as exc:
            pdb_name = Path(exe_input).stem + ".pdb"
            module_guid = module_age = None
            readobj_error = str(exc)
        evidence.image_guid, evidence.image_age = module_guid, module_age
        if module_guid is None and explicit is None:
            if readobj is None:
                evidence.note = "llvm-readobj not found beside llvm-symbolizer or on PATH"
            elif readobj_error:
                evidence.note = readobj_error
            else:
                evidence.note = "the image's debug directory records no PDB"
            evidence.outcome = ("no_debug_info" if found_debug_info is False
                                else "readobj_failed")
            if pdb_files:
                evidence.note = ("--symbol-path file not found: "
                                 + ", ".join(os.fspath(path) for path in pdb_files))
            evidence.symbolizer = _run_symbolizer(
                symbolizer_path, exe_input, None, rva_path, jsonl_file, warn_failure=False,
                debug_dirs=debug_dirs, fallback_paths=fallback_paths, stream=stream)
            print("PDB=none", file=stream)
            parquet = _write_parquet(jsonl_file, parquet_file, stream=stream)
            print("FOLDS: skipped (no PDB; no public symbols to enumerate)", file=stream)
            return SymbolizedModule(parquet, None, None, evidence)
        if module_guid is None:
            # An explicit symbol file remains the caller's choice when the image carries no PDB
            # identity; it is used unverified rather than silently ignored.
            probe = PdbProbe(explicit, "accepted",
                             detail="caller-supplied --symbol-path file, not GUID-verified")
            evidence.search = PdbSearch(pdb=explicit, probes=[probe])
            pdb = explicit
            print("WARNING: the image records no PDB identity; using the --symbol-path file "
                  "without GUID verification", file=stream)
        elif pdbutil:
            evidence.search = _choose_pdb(pdbutil, exe_path, pdb_name, module_guid, module_age,
                                          search_dirs, pdb_files)
            pdb = evidence.search.pdb
        else:
            probes = [
                PdbProbe(path, "accepted" if path.is_file() else "not_found",
                         detail=("caller-supplied --symbol-path file, used without GUID "
                                 "verification"
                                 if path in pdb_files else "used without GUID verification"))
                for path in dict.fromkeys(_candidate_pdbs(
                    exe_path, pdb_name, module_guid, module_age, search_dirs, pdb_files))]
            pdb = next((probe.path for probe in probes if probe.status == "accepted"), None)
            evidence.search = PdbSearch(pdb=pdb, probes=probes)
            if pdb:
                print("WARNING: llvm-pdbutil not found; using the PDB without GUID "
                      "verification", file=stream)
        if not pdb:
            evidence.outcome = ("no_debug_info" if found_debug_info is False else "no_pdb")
            evidence.symbolizer = _run_symbolizer(
                symbolizer_path, exe_input, None, rva_path, jsonl_file, warn_failure=False,
                debug_dirs=debug_dirs, fallback_paths=fallback_paths, stream=stream)
            print("PDB=none", file=stream)
            parquet = _write_parquet(jsonl_file, parquet_file, stream=stream)
            print("FOLDS: skipped (no PDB; no public symbols to enumerate)", file=stream)
            return SymbolizedModule(parquet, None, None, evidence)
        print("PDB_MATCH=yes", file=stream)
        evidence.pdb = str(pdb)

    # The symbolizer reads the image, the resolved PDB, the RVA list and the tool, and all four
    # are in the key. An unresolved module is not cached: there is no build identity to key it on.
    cache_key: str | None = None
    image_id = artifact_file_identity(exe_path)
    pdb_id = artifact_file_identity(pdb)
    symbolizer_id = artifact_file_identity(symbolizer_path)
    try:
        rva_digest: str | None = artifact_file_digest(rva_path)
    except OSError:
        rva_digest = None
    if (rva_digest is not None and artifact_identity_present(image_id)
            and artifact_identity_present(pdb_id) and artifact_identity_present(symbolizer_id)):
        cache_key = artifact_key({
            "kind": "symbols",
            "module_image": image_id,
            "module_spelling": os.path.normcase(os.path.abspath(exe_input)),
            "module_name": module_name,
            "pdb": pdb_id,
            "symbolizer": symbolizer_id,
            "rva_sha256": rva_digest,
            "debug_dirs": [os.path.normcase(os.path.abspath(os.fspath(path)))
                           for path in debug_dirs],
            "fallback_paths": [os.path.normcase(os.path.abspath(os.fspath(path)))
                               for path in fallback_paths],
            "found_debug_info": found_debug_info,
        })
    if cache_key is not None:
        cached = load_cached_artifacts("symbols", cache_key, ("symbols.parquet",))
        if cached is not None:
            directory, meta = cached
            try:
                shutil.copyfile(directory / "symbols.parquet", parquet_file)
            except OSError:
                cached = None
            else:
                publics = _publics(pdb, jsonl_file, symbolizer_path, stream)
                return SymbolizedModule(parquet_file, publics, pdb,
                                        _evidence_from_data(meta.get("evidence")))

    evidence.symbolizer = _run_symbolizer(
        symbolizer_path, exe_input, pdb, rva_path, jsonl_file, debug_dirs=debug_dirs,
        fallback_paths=fallback_paths, stream=stream)
    if (evidence.symbolizer.returncode == 0 and evidence.symbolizer.records
            and evidence.symbolizer.empty_records == evidence.symbolizer.records
            and evidence.symbolizer.errors):
        # A wrong-build PDB is not reported by the exit status; keep its evidence on the outcome.
        evidence.outcome = "failed"
        evidence.note = evidence.symbolizer.errors[0][1]
    short_file = out_path / "symbols.short.jsonl"
    try:
        _run_symbolizer(symbolizer_path, exe_input, pdb, rva_path, short_file,
                        debug_dirs=debug_dirs, fallback_paths=fallback_paths,
                        functions="short", warn_failure=False, stream=stream)
    except UprofError as exc:
        print(f"WARNING: llvm-symbolizer short-name pass failed; line function identity "
              f"may be less precise ({exc})", file=stream)
        short_file = None
    parquet = _write_parquet(jsonl_file, parquet_file, short_file, stream=stream)
    if os.name == "nt" and pdb:
        publics = _publics(pdb, jsonl_file, symbolizer_path, stream)
    else:
        publics = None
        print("FOLDS: skipped (DWARF symbols; PDB public-symbol fold detection is "
              "Windows-only, unverified)", file=stream)
    # A failed short-name pass would cache a less precise artifact, so the next run retries it.
    if cache_key is not None and parquet is not None and short_file is not None:
        store_cached_artifacts("symbols", cache_key, {"symbols.parquet": parquet},
                               {"evidence": _evidence_data(evidence)})
    return SymbolizedModule(parquet, publics, pdb, evidence)


# ------------------------------------------------------------- whole-scope symbolization

def _combine_artifacts(parquet_inputs: list[tuple[str, Path]],
                       public_inputs: list[tuple[str, Path]],
                       out_dir: Path) -> tuple[Path, Path | None]:
    """Write one symbols.parquet and, when any module has publics, one symbols.publics.csv."""
    try:
        import duckdb
    except ImportError as exc:
        raise UprofError(
            "the DuckDB Python package is not installed; install it with: "
            f"{sys.executable} -m pip install duckdb") from exc
    symbols_file = out_dir / "symbols.parquet"
    publics_file = out_dir / "symbols.publics.csv"
    try:
        with duckdb.connect() as connection:
            connection.execute(
                "CREATE TEMP TABLE combined(module_name VARCHAR, address VARCHAR, "
                "function VARCHAR, function_short VARCHAR, "
                "file VARCHAR, line INTEGER, containing_function VARCHAR)")
            for module_name, parquet in parquet_inputs:
                connection.execute(
                    "INSERT INTO combined SELECT CAST(? AS VARCHAR), address, function, "
                    "function_short, file, line, containing_function "
                    "FROM read_parquet(?)", [module_name, str(parquet)])
            connection.execute("COPY combined TO ? (FORMAT parquet)", [str(symbols_file)])
            if public_inputs:
                connection.execute(
                    "CREATE TEMP TABLE publics(module_name VARCHAR, rva VARCHAR, "
                    "name_count INTEGER, names VARCHAR)")
                for module_name, path in public_inputs:
                    connection.execute(
                        "INSERT INTO publics SELECT CAST(? AS VARCHAR), rva, "
                        "CAST(name_count AS INTEGER), names FROM read_csv(?, header = true, "
                        "all_varchar = true)", [module_name, str(path)])
                connection.execute("COPY (SELECT * FROM publics) TO ? (FORMAT csv, HEADER)",
                                   [str(publics_file)])
    except (duckdb.Error, OSError) as exc:
        raise UprofError(f"cannot combine the symbol artifacts in {out_dir}: {exc}") from exc
    return symbols_file, (publics_file if public_inputs else None)


def _probe_text(probe: PdbProbe) -> str:
    return f"{probe.path} [{probe.detail or probe.status}]"


def _evidence_line(evidence: ModuleEvidence, stream: TextIO) -> None:
    """One line per module with the image identity, every location searched and the remedy."""
    if evidence.outcome == "resolved":
        return
    facts = [f"image GUID {evidence.image_guid or 'unknown'} "
             f"age {evidence.image_age if evidence.image_age is not None else 'unknown'}",
             "debug info recorded: "
             + ("no" if evidence.found_debug_info is False
                else "yes" if evidence.found_debug_info else "unknown")]
    if evidence.note:
        facts.append(evidence.note)
    if evidence.symbolizer and evidence.symbolizer.errors:
        address, message = evidence.symbolizer.errors[0]
        prefix = f"llvm-symbolizer exited {evidence.symbolizer.returncode}"
        facts.append(f"{prefix}: {message}" + (f" (address {address})" if address else ""))
    elif evidence.symbolizer and evidence.symbolizer.returncode != 0:
        facts.append(f"llvm-symbolizer exited {evidence.symbolizer.returncode}: "
                     f"{tail_lines(evidence.symbolizer.stderr) or 'no stderr'}")
    if evidence.search:
        if evidence.search.probes:
            facts.append("searched: "
                         + "; ".join(_probe_text(probe) for probe in evidence.search.probes))
        if evidence.search.urls:
            facts.append("symbol-server URLs not consulted: "
                         + ", ".join(evidence.search.urls))
    remedy = ("pass profile --symbol-path <file|dir> for a symbol or debug file or a search "
              "root, --image-path <file|dir> when the module image is not at its recorded path, "
              "or place the matching debug file beside the module or in a local srv* cache "
              "(translate --symbol-path/--symbol-cache/--symbol-server can stage it), then re-run "
              "profile; a third-party module with no symbols is a finding, not a blocker")
    print(f"WARNING: no symbols resolved for {evidence.module_name} "
          f"({evidence.module_path}): {'; '.join(facts)}; {remedy}", file=stream)


def _write_pdb_manifest(inputs: list[tuple[str, str]], out_dir: Path) -> Path:
    """Write the module -> PDB choice as CSV so the instruction context reuses it."""
    path = out_dir / "symbols.pdbs.csv"
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["module_name", "pdb"])
            writer.writerows(inputs)
    except OSError as exc:
        raise UprofError(f"cannot write the PDB manifest {path}: {exc}; check free space and "
                         "permissions on the artifact directory") from exc
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


def _symbolize_worker(index: int, module_name: str, module_path: str, rvas: list[str],
                      out_path: Path, debug_info: dict[str, bool] | None,
                      symbol_files: Iterable[str | Path],
                      symbol_dirs: Iterable[str | Path],
                      image_files: Iterable[str | Path],
                      image_dirs: Iterable[str | Path]) -> _ModuleSymbolization:
    """Symbolize one module into its own directory; return its text instead of writing it."""
    output = io.StringIO()
    evidence: ModuleEvidence | None = None
    try:
        recorded_path = module_path
        resolved, _source = resolve_image(module_name, module_path, tuple(image_files),
                                          tuple(image_dirs))
        if resolved is None:
            basename = Path(module_path).name if module_path else Path(module_name).name
            if not module_path:
                note = ("the capture records no path for this module and no --image-path image "
                        f"matches {basename}")
            else:
                note = (f"{module_path} is not on this host and no --image-path image matches "
                        f"{basename}")
            evidence = ModuleEvidence(module_name, recorded_path,
                                      found_debug_info=(debug_info or {}).get(module_name),
                                      outcome="module_missing", note=note)
            _evidence_line(evidence, output)
            return _ModuleSymbolization(module_name, output.getvalue(), evidence=evidence)
        module_path = os.fspath(resolved)
        module_dir = out_path / f"module-{index:04d}"
        rva_file = module_dir / "rvas.txt"
        try:
            module_dir.mkdir()
            with open(rva_file, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(rvas) + "\n")
        except OSError as exc:
            raise UprofError(f"cannot write the symbol work files in {module_dir}: {exc}; "
                             "check free space and permissions on the artifact directory") from exc
        print(f"MODULE: {module_name}: {len(rvas)} addresses in {module_path}", file=output)
        try:
            result = symbolize_module(
                module_path, rva_file, module_dir, stream=output, module_name=module_name,
                found_debug_info=(debug_info or {}).get(module_name),
                symbol_files=symbol_files, symbol_dirs=symbol_dirs)
        except UprofError as exc:
            evidence = ModuleEvidence(module_name, module_path,
                                      found_debug_info=(debug_info or {}).get(module_name),
                                      outcome="failed", note=str(exc))
            _evidence_line(evidence, output)
            return _ModuleSymbolization(module_name, output.getvalue(), evidence=evidence)
        evidence = result.evidence
        _evidence_line(evidence, output)
        if result.parquet is None:
            raise UprofError(f"cannot write the symbol artifact for {module_path} in {module_dir}")
        return _ModuleSymbolization(
            module_name, output.getvalue(), evidence=evidence, parquet=result.parquet,
            publics=result.publics, pdb=str(result.pdb) if result.pdb else None)
    except Exception as exc:
        return _ModuleSymbolization(module_name, output.getvalue(), evidence=evidence,
                                    error=exc)


def _symbolize_tasks(modules: list[tuple[str, str, list[str]]], out_path: Path,
                     stream: TextIO, debug_info: dict[str, bool] | None,
                     symbol_files: Iterable[str | Path],
                     symbol_dirs: Iterable[str | Path],
                     image_files: Iterable[str | Path],
                     image_dirs: Iterable[str | Path]) -> list[_ModuleSymbolization]:
    """Symbolize modules in a bounded pool; write each module's text in module index order."""
    symbol_files, symbol_dirs = tuple(symbol_files), tuple(symbol_dirs)
    image_files, image_dirs = tuple(image_files), tuple(image_dirs)
    workers = max(1, min(len(modules), MAX_SYMBOLIZATION_WORKERS, os.cpu_count() or 1))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="uprof-symbol")
    try:
        futures = [
            pool.submit(_symbolize_worker, index, module_name, module_path, rvas, out_path,
                        debug_info, symbol_files, symbol_dirs, image_files, image_dirs)
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
        # A failed module stops replay, so queued module work is dropped rather than awaited.
        pool.shutdown(wait=True, cancel_futures=True)


def symbolize_modules(modules: list[tuple[str, str, list[str]]], out_dir: str | Path,
                      stream: TextIO = sys.stdout,
                      debug_info: dict[str, bool] | None = None,
                      symbol_files: Iterable[str | Path] = (),
                      symbol_dirs: Iterable[str | Path] = (),
                      image_files: Iterable[str | Path] = (),
                      image_dirs: Iterable[str | Path] = ()
                      ) -> tuple[Path | None, Path | None, Path | None, list[ModuleEvidence]]:
    """Symbolize every module in scope and combine the artifacts.

    modules holds (module_name, binary_path, [rva hex text]) groups. debug_info maps a module key
    to Module.foundDebugInfo so the evidence separates "no debug info recorded" from "symbol file
    missing". symbol_files are explicit symbol/debug files and symbol_dirs are search roots.
    image_files are explicit module images and image_dirs are searched by the module's basename;
    the caller probes --symbol-path and --image-path once with probe_paths. Modules are symbolized
    concurrently, but their stream text and the combined artifacts keep module order. Returns
    (symbols.parquet, symbols.publics.csv, symbols.pdbs.csv, evidence); the parquet is None when
    no module could be symbolized, in which case the SQL reports every address as '<no symbol>'.
    """
    out_path = absolute(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    require_symbolizer()
    parquet_inputs: list[tuple[str, Path]] = []
    public_inputs: list[tuple[str, Path]] = []
    pdb_inputs: list[tuple[str, str]] = []
    evidence: list[ModuleEvidence] = []
    # The public-symbol scratch lives only while the modules are enumerated; a process that
    # profiles twice in a row must not reuse the first run's scratch or memo. The pool joins
    # every worker before _symbolize_tasks returns, so the finally cannot race a worker.
    _PUBLICS_CACHE.reset()
    try:
        outcomes = _symbolize_tasks(modules, out_path, stream, debug_info, symbol_files,
                                    symbol_dirs, image_files, image_dirs)
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
    if not parquet_inputs:
        return None, None, None, evidence
    symbols_file, publics_file = _combine_artifacts(parquet_inputs, public_inputs, out_path)
    pdbs_file = _write_pdb_manifest(pdb_inputs, out_path) if pdb_inputs else None
    return symbols_file, publics_file, pdbs_file, evidence
