"""Addresses -> file:line plus the PDB public-symbol fold cache (Windows PDB / Linux DWARF)."""

from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Final, TextIO

from uprof_native import find_sibling, require_symbolizer, run
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
    return pdb_name, guid, age


def _pdb_signature(pdbutil: Path | None, path: Path) -> tuple[str | None, str | None] | None:
    if not pdbutil:
        return None
    completed = run([str(pdbutil), "dump", "--summary", str(path)],
                    timeout=PDB_PROBE_TIMEOUT)
    guid = age = None
    for line in (completed.stdout or "").splitlines():
        if match := _GUID.match(line):
            guid = _normalize_guid(match.group(1))
        elif match := _AGE.match(line):
            age = match.group(1)
    return (guid, age) if guid and age else None


def _pdb_matches(pdbutil: Path | None, path: Path, module_guid: str | None,
                 module_age: int | None) -> bool:
    # A PDB is accepted only against the image's debug-directory GUID. Without that GUID
    # (llvm-readobj missing or failed) a same-named PDB cannot be verified, so it is rejected.
    if not module_guid or not pdbutil:
        return False
    signature = _pdb_signature(pdbutil, path)
    if not signature or signature[0] != module_guid:
        return False
    if signature[1] == str(module_age):
        return True
    # Microsoft's symbol cache stores the file under <GUID><age> taken from the image, while the
    # PDB's own age can differ on this host (measured: ntdll image age 1, cached PDB age 4). The
    # key directory is then the authoritative age check; any other candidate must match exactly.
    key = f"{module_guid}{module_age:X}" if module_age is not None else None
    return key is not None and path.parent.name.upper() == key.upper()


def _candidate_pdbs(exe: Path, pdb_name: str, module_guid: str | None,
                    age: int | None) -> list[Path]:
    module_name = exe.stem
    candidates = [exe.parent / name for name in dict.fromkeys([pdb_name, module_name + ".pdb"])]
    for entry in (os.environ.get("_NT_SYMBOL_PATH") or "").split(";"):
        parts = entry.split("*")
        if len(parts) < 3 or parts[0].strip().lower() != "srv":
            continue
        cache_dir = Path(parts[1]) / pdb_name
        if module_guid and age is not None:
            candidates.append(cache_dir / f"{module_guid}{age:X}" / pdb_name)
        try:
            discovered = list(cache_dir.rglob(pdb_name))
        except OSError:
            # An unreachable or unreadable cache path contributes nothing.
            continue
        candidates.extend(discovered)
    return candidates


def _choose_pdb(pdbutil: Path, exe_path: Path, pdb_name: str, module_guid: str | None,
                module_age: int | None) -> Path | None:
    """The first candidate whose PDB matches the image, skipping unreadable or failed probes."""
    for path in _candidate_pdbs(exe_path, pdb_name, module_guid, module_age):
        try:
            if path.is_file() and _pdb_matches(pdbutil, path, module_guid, module_age):
                return path
        except (UprofError, OSError):
            # A probe timeout or an unreadable cache entry rejects this candidate only.
            continue
    return None


def _write_error_records(out_file: Path, rva_file: Path, exe: str, pdb_name: str) -> None:
    records = []
    for line in read_text(rva_file).splitlines():
        address = line.strip()
        if address:
            records.append(json.dumps({
                "Address": address,
                "Error": {"Message": f"PDB {pdb_name} not found or GUID/age mismatch"},
                "ModuleName": exe,
            }, separators=(",", ":")))
    try:
        with open(out_file, "w", encoding="utf-8", newline="\n") as handle:
            if records:
                handle.write("\n".join(records) + "\n")
    except OSError as exc:
        raise UprofError(f"cannot write symbol records to {out_file}: {exc}; check free space "
                         "and permissions on the symbol artifact directory") from exc


def _run_symbolizer(symbolizer: Path, exe: str, pdb: Path | None, rva_file: Path,
                    out_file: Path, functions: str = "linkage") -> None:
    command = [str(symbolizer), f"--obj={exe}"]
    if pdb:
        command.append(f"--pdb={pdb}")
    command += ["--relative-address", f"--functions={functions}", "--inlines",
                "--output-style=JSON"]
    completed = run(command, input_text=read_text(rva_file), timeout=4 * 60 * 60)
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        raise UprofError("llvm-symbolizer failed with "
                         f"{completed.returncode}: {tail_lines(completed.stderr)}")
    if completed.returncode != 0:
        print(f"WARNING: llvm-symbolizer exited {completed.returncode} for {exe}; the addresses "
              "it could not symbolize become '<no symbol>'", file=sys.stderr)
    with open(out_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(completed.stdout or "")
    if completed.stderr and completed.stderr.strip():
        print(f"WARNING: llvm-symbolizer: {tail_lines(completed.stderr)}", file=sys.stderr)


def _write_parquet(out_file: Path, parquet_file: Path,
                   short_file: Path | None = None) -> Path | None:
    try:
        import duckdb
    except ImportError:
        print(f"WARNING: DuckDB not found; cannot write {parquet_file}, so symbolization fails",
              file=sys.stderr)
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
              file=sys.stderr)
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

    sections_run = run([str(pdbutil_path), "dump", "-section-headers", str(pdb_path)])
    publics_run = run([str(pdbutil_path), "dump", "-publics", str(pdb_path)])
    pairs = _pairs((publics_run.stdout or "").splitlines(),
                   _sections((sections_run.stdout or "").splitlines()))
    with tempfile.TemporaryDirectory(prefix="uprof-publics-") as directory:
        pairs_path = Path(directory) / "pairs.csv"
        _write_pairs(pairs_path, pairs)
        return _aggregate_publics(pairs_path, out_path)


def _publics(pdb: Path, out_file: Path, symbolizer: Path, stream: TextIO) -> Path | None:
    publics_file = out_file.with_suffix(".publics.csv")
    pdbutil = find_sibling(symbolizer, "llvm-pdbutil")
    if not pdbutil:
        print("FOLDS: skipped (llvm-pdbutil not found; install LLVM to label folded addresses)",
              file=stream)
        return None
    try:
        starts, shared = public_symbols(publics_file, pdb_file=pdb, pdbutil=pdbutil)
    except UprofError as exc:
        print(f"WARNING: public-symbol enumeration failed; fold labelling unavailable ({exc})",
              file=stream)
        print("FOLDS: skipped (enumeration failed)", file=stream)
        return None
    print(f"FOLDS: {shared} shared starts of {starts} in {publics_file}", file=stream)
    return publics_file

# ---------------------------------------------------------------- symbolization

def symbolize_module(exe: str, rva_file: str | Path, out_dir: str | Path,
                     stream: TextIO = sys.stdout
                     ) -> tuple[Path | None, Path | None, tuple[str, str | None] | None,
                                Path | None]:
    """Symbolize the RVAs into out_dir; return (parquet, publics, guid_failure, pdb).

    The PDB is found from the module's debug directory: beside the module or in a
    _NT_SYMBOL_PATH cache. publics is the RVA -> public-name cache when a PDB was used; pdb is
    the accepted PDB (None when there is none). guid_failure is None or (reason category, detail)
    when the image GUID was unavailable, so no PDB was accepted; the caller reports these
    failures once per run.
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
    pdb: Path | None = None

    if os.name == "nt":
        readobj = find_sibling(symbolizer_path, "llvm-readobj")
        pdbutil = find_sibling(symbolizer_path, "llvm-pdbutil")
        readobj_error = None
        try:
            pdb_name, module_guid, module_age = _readobj_info(exe_input, readobj)
        except (UprofError, OSError) as exc:
            pdb_name = Path(exe_input).stem + ".pdb"
            module_guid = module_age = None
            readobj_error = str(exc)
        if module_guid is None:
            if readobj is None:
                guid_failure = ("not found", None)
            elif readobj_error:
                guid_failure = ("failed", readobj_error)
            else:
                guid_failure = ("no GUID", None)
            _write_error_records(jsonl_file, rva_path, exe_input, pdb_name)
            print("PDB=none", file=stream)
            parquet = _write_parquet(jsonl_file, parquet_file)
            print("FOLDS: skipped (no PDB; no public symbols to enumerate)", file=stream)
            return parquet, None, guid_failure, None
        if pdbutil:
            pdb = _choose_pdb(pdbutil, exe_path, pdb_name, module_guid, module_age)
        else:
            beside = exe_path.parent / pdb_name
            pdb = beside if beside.is_file() else None
            if pdb:
                print("WARNING: llvm-pdbutil not found; using the PDB beside the module "
                      "without GUID verification", file=stream)
        if not pdb:
            print(f"WARNING: no PDB matching the module's debug-directory GUID ({pdb_name}); "
                  "writing '<no symbol>' records", file=stream)
            _write_error_records(jsonl_file, rva_path, exe_input, pdb_name)
            print("PDB=none", file=stream)
            parquet = _write_parquet(jsonl_file, parquet_file)
            print("FOLDS: skipped (no PDB; no public symbols to enumerate)", file=stream)
            return parquet, None, None, None
        print("PDB_MATCH=yes", file=stream)

    _run_symbolizer(symbolizer_path, exe_input, pdb, rva_path, jsonl_file)
    short_file = out_path / "symbols.short.jsonl"
    try:
        _run_symbolizer(symbolizer_path, exe_input, pdb, rva_path, short_file,
                        functions="short")
    except UprofError as exc:
        print(f"WARNING: llvm-symbolizer short-name pass failed; line function identity "
              f"may be less precise ({exc})", file=stream)
        short_file = None
    parquet = _write_parquet(jsonl_file, parquet_file, short_file)
    if os.name == "nt" and pdb:
        publics = _publics(pdb, jsonl_file, symbolizer_path, stream)
    else:
        publics = None
        print("FOLDS: skipped (DWARF symbols; PDB public-symbol fold detection is "
              "Windows-only, unverified)", file=stream)
    return parquet, publics, None, pdb


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


def _warn_guid_failures(failures: list[tuple[str, str | None]], stream: TextIO) -> None:
    """Report image-GUID failures once for the run, with module counts and remedies."""
    counts: dict[str, int] = {}
    first_detail = None
    for category, detail in failures:
        counts[category] = counts.get(category, 0) + 1
        if category == "failed" and first_detail is None:
            first_detail = detail
    reason = ", ".join(f"{category}: {count}" for category, count in sorted(counts.items()))
    if first_detail:
        reason += f" (first error: {first_detail})"
    remedies = []
    modules = "module" if len(failures) == 1 else "modules"
    images = "image" if len(failures) == 1 else "images"
    if "not found" in counts:
        remedies.append("install LLVM so llvm-readobj sits beside llvm-symbolizer, or set "
                        "LLVM_SYMBOLIZER to an LLVM bin directory that contains it")
    if "failed" in counts:
        remedies.append(f"check that llvm-readobj --coff-debug-directory works on the affected "
                        f"{images}")
    if "no GUID" in counts:
        remedies.append(f"check that the debug directory of the affected {images} records a PDB")
    pronoun = "it" if len(failures) == 1 else "them"
    possessive = "its" if len(failures) == 1 else "their"
    print(f"WARNING: llvm-readobj could not provide an image GUID for {len(failures)} "
          f"{modules} ({reason}); no PDB was accepted for {pronoun}, so {possessive} addresses "
          f"become '<no symbol>'; {'; '.join(remedies)}", file=stream)


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


def symbolize_modules(modules: list[tuple[str, str, list[str]]], out_dir: str | Path,
                      stream: TextIO = sys.stdout) -> tuple[Path | None, Path | None,
                                                             Path | None]:
    """Symbolize every module in scope and combine the artifacts.

    modules holds (module_name, binary_path, [rva hex text]) groups. Returns
    (symbols.parquet, symbols.publics.csv, symbols.pdbs.csv); the parquet is None when no module
    could be symbolized, in which case the SQL reports every address as '<no symbol>'.
    """
    out_path = absolute(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    require_symbolizer()
    parquet_inputs: list[tuple[str, Path]] = []
    public_inputs: list[tuple[str, Path]] = []
    pdb_inputs: list[tuple[str, str]] = []
    guid_failures: list[tuple[str, str | None]] = []
    for index, (module_name, module_path, rvas) in enumerate(modules):
        if not module_path:
            print(f"WARNING: no module path for {module_name!r}; its addresses become "
                  "<no symbol>", file=stream)
            continue
        if not Path(module_path).is_file():
            print(f"WARNING: module not found on disk: {module_path}; its addresses become "
                  "<no symbol>", file=stream)
            continue
        module_dir = out_path / f"module-{index:04d}"
        rva_file = module_dir / "rvas.txt"
        try:
            module_dir.mkdir()
            with open(rva_file, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(rvas) + "\n")
        except OSError as exc:
            raise UprofError(f"cannot write the symbol work files in {module_dir}: {exc}; "
                             "check free space and permissions on the artifact directory") from exc
        print(f"MODULE: {module_name}: {len(rvas)} addresses in {module_path}", file=stream)
        try:
            parquet, publics, guid_failure, pdb = symbolize_module(
                module_path, rva_file, module_dir, stream=stream)
        except UprofError as exc:
            print(f"WARNING: symbolization failed for {module_path}: {exc}; its addresses "
                  "become '<no symbol>'", file=stream)
            continue
        if guid_failure:
            guid_failures.append(guid_failure)
        if parquet is None:
            raise UprofError(f"cannot write the symbol artifact for {module_path} in {module_dir}")
        parquet_inputs.append((module_name, parquet))
        if publics:
            public_inputs.append((module_name, publics))
        if pdb:
            pdb_inputs.append((module_name, str(pdb)))
    if guid_failures:
        _warn_guid_failures(guid_failures, stream)
    if not parquet_inputs:
        return None, None, None
    symbols_file, publics_file = _combine_artifacts(parquet_inputs, public_inputs, out_path)
    pdbs_file = _write_pdb_manifest(pdb_inputs, out_path) if pdb_inputs else None
    return symbols_file, publics_file, pdbs_file
