"""Keep only named processes of a translated AMD uProf capture and remove what names the host.

Usage:
    python trials/anonymize_capture.py <session_dir> --keep-process <name> [<name> ...]
        --out <dir> [--bundle <image or PDB> ...]

<session_dir> is a translated session (session.uprof and cpu.db, as `uprof.py translate`
writes it). <out_dir> must be new or empty. It receives:

- cpu.db: a copy where every process other than the kept ones is deleted with its threads,
  module instances and samples, modules and functions no kept sample uses are deleted, HostInfo
  is emptied, thread names are blanked, and every path outside the Windows and system library
  folders becomes c:\\capture\\<file name> (or /capture/<file name>).
- session.uprof: only the keys the tools read, with host values and paths replaced.
- review.txt: every table with every column as plain text, then the sorted distinct strings of
  the capture, for a person to review before the files leave the machine.
- images/: each --bundle image the capture names, if its size and checksum match the module,
  and its PDB, if GUID and age match the image; review.txt lists their strings.

It exits 1 when review.txt still contains this machine's name, the user's name or a user
profile path, and 2 when the input is not usable (missing files, an unknown process, or a
cpu.db schema the tools do not read). Needs Python 3.9+ and the duckdb package.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

# Columns the tools read; a cpu.db without them is refused rather than half-filtered.
REQUIRED = {
    "Process": ("id", "name"),
    "Module": ("id", "path"),
    "Function": ("id", "moduleId", "name"),
    "UnifiedSampleSeries": ("processId", "moduleId", "functionId", "moduleRVA"),
}
# Folders whose paths name no user: kept as they are.
SYSTEM_PREFIXES = ("c:\\windows\\", "/usr/", "/lib/", "/lib64/")
WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
PROFILE_PATH = re.compile(r"[\\/](users|home)[\\/][^\\/\s\"']+", re.IGNORECASE)
SCHEDULE_KEYS = ("elapsed", "start", "end", "startTs", "endTs", "profileDuration")


class Refused(Exception):
    """The input cannot be anonymized; the message is the cause."""


def host_names() -> list[str]:
    """This machine's name and the user's name, as a capture would spell them."""
    names = {
        os.environ.get("COMPUTERNAME", ""),
        socket.gethostname(),
        os.environ.get("USERNAME", ""),
        os.environ.get("USER", ""),
    }
    # Without a user name there is nothing to add.
    with contextlib.suppress(Exception):
        names.add(getpass.getuser())
    return sorted(name for name in names if name)


def rewrite_path(value: str) -> str:
    """A path outside the system folders becomes c:\\capture\\<name>; other text is unchanged."""
    if WINDOWS_PATH.match(value):
        if value.lower().replace("/", "\\").startswith(SYSTEM_PREFIXES):
            return value
        return "c:\\capture\\" + PureWindowsPath(value.replace("/", "\\")).name.lower()
    if value.startswith("/"):
        if value.startswith(SYSTEM_PREFIXES):
            return value
        return "/capture/" + PurePosixPath(value).name
    return value


def columns_of(connection, table: str) -> dict[str, str]:
    return {
        name: kind
        for name, kind in connection.execute(
            "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ?", [table]
        ).fetchall()
    }


def tables_of(connection) -> list[str]:
    return [
        name
        for (name,) in connection.execute(
            "SELECT table_name FROM duckdb_tables() ORDER BY table_name"
        ).fetchall()
    ]


def check_schema(connection) -> None:
    present = set(tables_of(connection))
    for table, needed in REQUIRED.items():
        columns = columns_of(connection, table) if table in present else {}
        for column in needed:
            if column not in columns:
                raise Refused(
                    f"cpu.db lacks {table}.{column}; the tools read Process, Module, "
                    "Function and UnifiedSampleSeries as AMD uProf's translate "
                    "writes them, so this capture is not one they can profile"
                )


def kept_processes(connection, names: list[str]) -> list[int]:
    wanted = {name.lower() for name in names}
    found: dict[str, list[int]] = {}
    for pid, process in connection.execute("SELECT id, name FROM Process").fetchall():
        base = PureWindowsPath(str(process or "").replace("/", "\\")).name.lower()
        if base in wanted:
            found.setdefault(base, []).append(int(pid))
    missing = sorted(wanted - set(found))
    if missing:
        raise Refused(
            f"no process named {', '.join(missing)} in the capture; name the process "
            "image as `uprof.py profile --by process` prints it"
        )
    return sorted(pid for pids in found.values() for pid in pids)


def run(connection, sql: str, step: str, params=()):
    """Execute sql; a DuckDB failure becomes a Refused with one cause line."""
    import duckdb

    try:
        return connection.execute(sql, list(params))
    except duckdb.Error as exc:
        detail = " ".join(str(exc).split())[:300]
        raise Refused(
            f"DuckDB {duckdb.__version__} failed while {step}: {detail}; send this "
            "line and the table list `review.txt` would show (or the output of "
            "`SELECT table_name, column_name, data_type FROM duckdb_columns()` on "
            "the capture's cpu.db)"
        ) from exc


def stack_functions(session: Path, pids: list[int]) -> set[int]:
    """Function ids the kept processes' call stacks reach, so cpu.db keeps them."""
    import duckdb

    source = session / "callstack.db"
    if not source.is_file():
        return set()
    pid_list = ", ".join(str(pid) for pid in pids)
    connection = duckdb.connect(str(source), read_only=True)
    try:
        functions: set[int] = set()
        tables = tables_of(connection)
        stacks = []
        for table in tables:
            names = set(columns_of(connection, table))
            stack = next((n for n in ("callstackId", "callStackId") if n in names), None)
            if "processId" in names and stack:
                stacks.append(f'SELECT "{stack}" FROM "{table}" WHERE "processId" IN ({pid_list})')
            if "processId" in names and "functionId" in names:
                functions |= {
                    row[0]
                    for row in run(
                        connection,
                        f'SELECT DISTINCT "functionId" FROM "{table}" WHERE '
                        f'"processId" IN ({pid_list}) AND "functionId" IS NOT NULL',
                        f"reading the functions of {table}",
                    ).fetchall()
                }
        for table in tables:
            names = set(columns_of(connection, table))
            if (
                stacks
                and "callstackId" in names
                and "functionId" in names
                and "processId" not in names
            ):
                functions |= {
                    row[0]
                    for row in run(
                        connection,
                        f'SELECT DISTINCT "functionId" FROM "{table}" WHERE '
                        f'"callstackId" IN ({" UNION ".join(stacks)}) AND "functionId" IS NOT NULL',
                        f"reading the frames of {table}",
                    ).fetchall()
                }
        return functions
    finally:
        connection.close()


def copy_filtered(connection, pids: list[int], reached: set[int] = frozenset()) -> None:
    """Copy every table of src into packed, keeping only the kept processes' rows.

    Copying into a fresh database instead of deleting from a copy keeps no key or foreign-key
    constraint of the capture, so removing another process's rows cannot violate one.
    """
    pid_list = ", ".join(str(pid) for pid in pids)
    tables = [
        name
        for (name,) in run(
            connection,
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src' "
            "ORDER BY table_name",
            "listing the tables",
        ).fetchall()
    ]

    def columns(table: str) -> set[str]:
        return {
            name
            for (name,) in run(
                connection,
                "SELECT column_name FROM duckdb_columns() WHERE database_name = 'src' "
                "AND table_name = ?",
                f"reading the columns of {table}",
                [table],
            ).fetchall()
        }

    modules = {
        row[0]
        for row in run(
            connection,
            f"SELECT DISTINCT moduleId FROM src.UnifiedSampleSeries WHERE moduleId IS "
            f"NOT NULL AND processId IN ({pid_list})",
            "selecting the kept modules",
        ).fetchall()
    }
    if "ModuleInstance" in tables:
        modules |= {
            row[0]
            for row in run(
                connection,
                f"SELECT DISTINCT moduleId FROM src.ModuleInstance WHERE moduleId IS "
                f"NOT NULL AND processId IN ({pid_list})",
                "selecting the kept module instances",
            ).fetchall()
        }
    if reached:
        # Modules only the kept call stacks reach, so their frames still resolve.
        reached_list = ", ".join(str(int(f)) for f in reached)
        modules |= {
            row[0]
            for row in run(
                connection,
                f"SELECT DISTINCT moduleId FROM src.Function WHERE id IN "
                f"({reached_list}) AND moduleId IS NOT NULL",
                "selecting the modules the call stacks reach",
            ).fetchall()
        }
    module_list = ", ".join(str(int(m)) for m in modules) or "NULL"
    for table in tables:
        names = columns(table)
        if "host" in table.lower():
            where = "FALSE"
        elif table == "Process":
            where = f"id IN ({pid_list})"
        elif "processId" in names:
            where = f'"processId" IN ({pid_list})'
        elif table == "Module":
            where = f"id IN ({module_list})"
        elif "moduleId" in names:
            where = f'"moduleId" IN ({module_list})'
        else:
            where = "TRUE"
        run(
            connection,
            f'CREATE TABLE packed."{table}" AS SELECT * FROM src."{table}" WHERE {where}',
            f"copying table {table}",
        )


def rewrite_strings(connection) -> None:
    for table in tables_of(connection):
        for column, kind in columns_of(connection, table).items():
            if kind != "VARCHAR":
                continue
            step = f"rewriting {table}.{column}"
            if table == "ProcessThread" and column == "threadName":
                run(connection, f'UPDATE "{table}" SET "{column}" = \'\'', step)
                continue
            values = [
                value
                for (value,) in run(
                    connection,
                    f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL',
                    step,
                ).fetchall()
            ]
            for value in values:
                replaced = rewrite_path(value)
                if replaced != value:
                    run(
                        connection,
                        f'UPDATE "{table}" SET "{column}" = ? WHERE "{column}" = ?',
                        step,
                        [replaced, value],
                    )


EMBEDDED_PATH = re.compile(r"[A-Za-z]:[\\/]|\\\\|(^|[=;*])/")
# Options whose value names a local cache or a server; their values are replaced outright.
SERVER_OPTIONS = ("--symbol-server",)


def sanitize_command(command: object) -> object:
    """A recorded command with symbol-server values and every token holding a path replaced."""
    if not isinstance(command, str):
        return command
    tokens = command.split()
    result = []
    replace_next = False
    for token in tokens:
        bare = token.strip('"')
        if replace_next:
            result.append("<VALUE>")
            replace_next = False
            continue
        option, equals, _value = bare.partition("=")
        if option in SERVER_OPTIONS:
            if equals:
                result.append(f"{option}=<VALUE>")
            else:
                result.append(option)
                replace_next = True
            continue
        if EMBEDDED_PATH.search(bare):
            result.append(f"{option}=<PATH>" if equals and option.startswith("-") else "<PATH>")
            continue
        result.append(token)
    return " ".join(result)


def reduced_meta(meta: dict) -> dict:
    collection = meta.get("collection") or {}
    target = collection.get("target") or {}
    schedule = collection.get("schedule") or {}
    auxiliary = meta.get("auxiliary") or {}
    reduced_target: dict[str, object] = {"systemWide": target.get("systemWide")}
    if target.get("attachedProcesses") is not None:
        reduced_target["attachedProcesses"] = [
            {"processId": item.get("processId")}
            for item in target["attachedProcesses"]
            if isinstance(item, dict)
        ]
    if target.get("launchApp"):
        reduced_target["launchApp"] = PureWindowsPath(
            str(target["launchApp"]).replace("/", "\\")
        ).name
    result: dict[str, object] = {
        "collection": {
            "events": collection.get("events") or {},
            "schedule": {key: schedule[key] for key in SCHEDULE_KEYS if key in schedule},
            "target": reduced_target,
        },
        "auxiliary": {
            key: auxiliary[key] for key in ("eventColumnIds", "ibsOpSampling") if key in auxiliary
        },
    }
    for key in ("collectCommand", "translateCommand"):
        if key in meta:
            result[key] = sanitize_command(meta[key])
    return result


def walk_strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, str):
        yield value


def dump(connection, meta: dict) -> str:
    """Every table with every column as tab-separated text, then the distinct strings."""
    lines: list[str] = []
    strings: set[str] = set(walk_strings(meta))
    for table in tables_of(connection):
        columns = list(columns_of(connection, table))
        rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY ALL').fetchall()
        lines.append(f"== table {table} ({len(rows)} rows)")
        lines.append("\t".join(columns))
        for row in rows:
            lines.append("\t".join("" if value is None else str(value) for value in row))
            strings.update(value for value in row if isinstance(value, str) and value)
    lines.append(f"== distinct strings ({len(strings)})")
    lines.extend(sorted(strings))
    lines.append("== session.uprof")
    lines.append(json.dumps(meta, indent=1, sort_keys=True))
    return "\n".join(lines) + "\n"


def disclosures(text: str, names: list[str]) -> list[str]:
    """What in text names this host: its machine or user name, or a user profile path."""
    found = []
    for name in names:
        if re.search(rf"(?i)(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text):
            found.append(f"the name {name!r}")
    home = str(Path.home())
    if home and home.lower() in text.lower():
        found.append(f"the profile path {home!r}")
    match = PROFILE_PATH.search(text)
    if match:
        found.append(f"a user profile path {match.group(0)!r}")
    return found


def anonymize(session: Path, keep: list[str], out: Path, files: list[Path] = ()) -> Path:
    import duckdb

    for name in ("session.uprof", "cpu.db"):
        if not (session / name).is_file():
            raise Refused(f"{session / name} is missing; pass a translated session directory")
    if out.exists() and any(out.iterdir()):
        raise Refused(f"{out} is not empty; pass a new directory")
    try:
        source = duckdb.connect(str(session / "cpu.db"), read_only=True)
    except duckdb.Error as exc:
        detail = " ".join(str(exc).split())[:300]
        raise Refused(
            f"DuckDB {duckdb.__version__} cannot open the capture's cpu.db: {detail}; "
            "it may be written by a uProf whose DuckDB storage version this DuckDB "
            "does not read"
        ) from exc
    try:
        try:
            check_schema(source)
            kept = kept_processes(source, keep)
        except duckdb.Error as exc:
            detail = " ".join(str(exc).split())[:300]
            raise Refused(
                f"DuckDB {duckdb.__version__} failed while reading the capture: {detail}"
            ) from exc
    finally:
        source.close()
    meta = reduced_meta(json.loads((session / "session.uprof").read_text(encoding="utf-8-sig")))
    out.mkdir(parents=True, exist_ok=True)
    try:
        review = write_filtered(session, kept, out, meta)
        review += bundle(list(files), out)
    except BaseException:
        # A failed run leaves nothing that could be mistaken for a finished capture.
        for name in (
            "cpu.db",
            "cpu.db.wal",
            "callstack.db",
            "callstack.db.wal",
            "session.uprof",
            "review.txt",
            *DUMP_NAMES,
        ):
            (out / name).unlink(missing_ok=True)
        shutil.rmtree(out / "images", ignore_errors=True)
        raise
    (out / "review.txt").write_text(review, encoding="utf-8")
    (out / "session.uprof").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return out / "review.txt"


def write_filtered(session: Path, kept: list[int], out: Path, meta: dict) -> str:
    """Write the filtered cpu.db into out and return the review dump."""
    import duckdb

    # The capture is only read. The kept rows go into a fresh file with small blocks (a few
    # thousand rows take about 0.5 MB instead of ~5 MB).
    connection = duckdb.connect()
    try:
        run(
            connection,
            f"ATTACH '{(session / 'cpu.db').as_posix()}' AS src (READ_ONLY)",
            "attaching the capture read-only",
        )
        run(
            connection,
            f"ATTACH '{(out / 'cpu.db').as_posix()}' AS packed (BLOCK_SIZE 16384)",
            "creating the filtered cpu.db",
        )
        copy_filtered(connection, kept, stack_functions(session, kept))
    finally:
        connection.close()
    connection = duckdb.connect(str(out / "cpu.db"))
    try:
        rewrite_strings(connection)
        run(connection, "CHECKPOINT", "writing the filtered cpu.db")
        try:
            review = dump(connection, meta)
        except duckdb.Error as exc:
            detail = " ".join(str(exc).split())[:300]
            raise Refused(
                f"DuckDB {duckdb.__version__} failed while dumping the filtered cpu.db: {detail}"
            ) from exc
    finally:
        connection.close()
    return (
        review + handover_dump(session, kept, out, meta) + handover_callstacks(session, kept, out)
    )


def handover_callstacks(session: Path, kept: list[int], out: Path) -> str:
    """Copy callstack.db like cpu.db: every table, only the kept processes' rows, paths
    rewritten; return its review section. Call stacks are what caller-path attribution needs."""
    import duckdb

    source = session / "callstack.db"
    if not source.is_file():
        return "== callstack.db: absent in the capture\n"
    pid_list = ", ".join(str(pid) for pid in kept)
    connection = duckdb.connect()
    try:
        run(
            connection,
            f"ATTACH '{source.as_posix()}' AS src (READ_ONLY)",
            "attaching callstack.db read-only",
        )
        run(
            connection,
            f"ATTACH '{(out / 'callstack.db').as_posix()}' AS packed (BLOCK_SIZE 16384)",
            "creating the filtered callstack.db",
        )
        tables = [
            name
            for (name,) in run(
                connection,
                "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src' "
                "ORDER BY table_name",
                "listing the callstack.db tables",
            ).fetchall()
        ]
        stacks = []
        for table in tables:
            names = {
                name
                for (name,) in run(
                    connection,
                    "SELECT column_name FROM duckdb_columns() WHERE database_name = "
                    "'src' AND table_name = ?",
                    f"reading the columns of {table}",
                    [table],
                ).fetchall()
            }
            stack_column = (
                "callstackId"
                if "callstackId" in names
                else ("callStackId" if "callStackId" in names else None)
            )
            if "processId" in names and stack_column:
                stacks.append(
                    f'SELECT "{stack_column}" FROM src."{table}" WHERE "processId" IN ({pid_list})'
                )
        kept_stacks = " UNION ".join(stacks) or "SELECT NULL"
        for table in tables:
            names = {
                name
                for (name,) in run(
                    connection,
                    "SELECT column_name FROM duckdb_columns() WHERE database_name = "
                    "'src' AND table_name = ?",
                    f"reading the columns of {table}",
                    [table],
                ).fetchall()
            }
            if "host" in table.lower():
                where = "FALSE"
            elif "processId" in names:
                where = f'"processId" IN ({pid_list})'
            elif "callstackId" in names:
                where = f'"callstackId" IN ({kept_stacks})'
            else:
                where = "TRUE"
            run(
                connection,
                f'CREATE TABLE packed."{table}" AS SELECT * FROM src."{table}" WHERE {where}',
                f"copying callstack.db table {table}",
            )
    finally:
        connection.close()
    connection = duckdb.connect(str(out / "callstack.db"))
    try:
        rewrite_strings(connection)
        run(connection, "CHECKPOINT", "writing the filtered callstack.db")
        try:
            section = dump(connection, {})
        except duckdb.Error as exc:
            detail = " ".join(str(exc).split())[:300]
            raise Refused(
                f"DuckDB {duckdb.__version__} failed while dumping the filtered "
                f"callstack.db: {detail}"
            ) from exc
    finally:
        connection.close()
    return "== callstack.db\n" + section


DUMP_NAMES = ("IbsOpDump.csv", "IbsFetchDump.csv")
UPROF = (
    Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"
)


def is_ibs(meta: dict) -> bool:
    events = (meta.get("collection") or {}).get("events") or {}
    return bool(
        (meta.get("auxiliary") or {}).get("ibsOpSampling")
        or (events.get("ibsEvents") or {}).get("op")
    )


def dump_source(session: Path, meta: dict, workdir: Path) -> Path | None:
    """The capture's IBS dump: beside the session, or generated now from its raw files.

    A handover carries no raw files, so profile could not generate the dump there.
    """
    for name in DUMP_NAMES:
        if (session / name).is_file():
            return session / name
    if not is_ibs(meta):
        return None
    done = subprocess.run(
        [
            sys.executable,
            str(UPROF),
            "translate",
            "--session",
            str(session),
            "--out-dir",
            str(workdir),
            "--ascii",
            "ibsop-event-dump",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    dump = workdir / "IbsOpDump.csv"
    if done.returncode != 0 or not dump.is_file():
        detail = " ".join((done.stderr or done.stdout or "").split())[-300:]
        raise Refused(
            "the capture is IBS but has no IbsOpDump.csv, and generating it with "
            f"`uprof.py translate --ascii ibsop-event-dump` failed: {detail}; run that "
            "translate on the raw session and anonymize its output"
        )
    return dump


def handover_dump(session: Path, kept: list[int], out: Path, meta: dict) -> str:
    """Write the dump's kept rows, paths rewritten, beside the filtered cpu.db; return its
    review section."""
    import duckdb

    with tempfile.TemporaryDirectory(prefix="anonymize-dump-") as directory:
        source = dump_source(session, meta, Path(directory) / "translated")
        if source is None:
            return ""
        target = out / source.name
        pid_list = ", ".join(str(pid) for pid in kept)
        connection = duckdb.connect()
        try:
            run(
                connection,
                "CREATE TABLE dump AS SELECT * FROM read_csv(?, header = true, "
                f"all_varchar = true) WHERE TRY_CAST(PID AS BIGINT) IN ({pid_list})",
                f"reading {source.name}",
                [str(source)],
            )
            rewrite_strings(connection)
            run(
                connection,
                "COPY dump TO ? (HEADER, DELIMITER ',')",
                f"writing {source.name}",
                [str(target)],
            )
            columns = list(columns_of(connection, "dump"))
            rows = run(connection, "SELECT count(*) FROM dump", "counting the dump").fetchone()[0]
            strings: set[str] = set()
            for column in columns:
                strings.update(
                    value
                    for (value,) in run(
                        connection,
                        f'SELECT DISTINCT "{column}" FROM dump WHERE "{column}" IS NOT '
                        f'NULL AND TRY_CAST("{column}" AS DOUBLE) IS NULL',
                        f"reading the strings of {source.name}",
                    ).fetchall()
                )
        finally:
            connection.close()
    lines = [
        f"== dump {source.name} ({rows} rows)",
        ",".join(columns),
        f"== distinct strings in {source.name} ({len(strings)})",
        *sorted(strings),
    ]
    return "\n".join(lines) + "\n"


ASCII_RUN = re.compile(rb"[\x20-\x7e]{4,}")
UTF16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
DRIVE = re.compile(r"(?<![A-Za-z])([A-Za-z]):(?:\\+|/)")
# The compiler and linker record their own install paths in every PDB; LLVM's default install
# folder names no user.
TOOLCHAIN = re.compile(r"[a-z]:\\program files\\llvm\\")
MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"


def file_strings(data: bytes) -> list[str]:
    """The sorted distinct ASCII and UTF-16 strings of four or more characters in data."""
    strings = {match.group().decode("ascii") for match in ASCII_RUN.finditer(data)}
    strings |= {match.group().decode("utf-16-le") for match in UTF16_RUN.finditer(data)}
    return sorted(strings)


def host_paths(strings: list[str]) -> list[str]:
    """Strings that hold a drive path outside LLVM's install folder, or start a UNC path."""
    found = []
    for string in strings:
        if re.match(r"\\\\[A-Za-z0-9]", string):
            found.append(string)
            continue
        for match in DRIVE.finditer(string):
            head = re.sub(r"\\+|/", "\\\\", string[match.start() : match.start() + 60]).lower()
            if not TOOLCHAIN.match(head):
                found.append(string)
                break
    return found


def file_disclosures(path: Path, names: list[str]) -> list[str]:
    """What in a bundled file names this host: the checks of disclosures() and host paths."""
    strings = file_strings(path.read_bytes())
    found = disclosures("\n".join(strings), names)
    found += [f"the path {string[:200]!r}" for string in host_paths(strings)]
    return found


def pe_identity(data: bytes) -> tuple[int, int]:
    """(CheckSum, SizeOfImage) from a PE optional header, as the capture's Module table has them."""
    optional = struct.unpack_from("<I", data, 0x3C)[0] + 24
    return struct.unpack_from("<I", data, optional + 64)[0], struct.unpack_from(
        "<I", data, optional + 56
    )[0]


def debug_record(data: bytes) -> tuple[bytes, int, str] | None:
    """(GUID, age, PDB path) of the image's CodeView debug directory record, if it has one."""
    at = data.find(b"RSDS")
    if at < 0:
        return None
    end = data.index(b"\0", at + 24)
    guid = data[at + 4 : at + 20]
    age = struct.unpack_from("<I", data, at + 20)[0]
    return guid, age, data[at + 24 : end].decode("utf-8", "replace")


def pdb_record(data: bytes) -> tuple[bytes, int] | None:
    """(GUID, age) from a PDB's info stream (stream 1 of the MSF container)."""
    if not data.startswith(MSF_MAGIC):
        return None
    block_size, _, _, directory_bytes, _, block_map = struct.unpack_from("<6I", data, 32)
    count = -(-directory_bytes // block_size)
    blocks = struct.unpack_from(f"<{count}I", data, block_map * block_size)
    directory = b"".join(data[b * block_size : (b + 1) * block_size] for b in blocks)
    streams = struct.unpack_from("<I", directory)[0]
    sizes = struct.unpack_from(f"<{streams}I", directory, 4)
    offset = 4 + 4 * streams
    offset += 4 * -(-sizes[0] // block_size) if sizes[0] != 0xFFFFFFFF else 0
    first = struct.unpack_from("<I", directory, offset)[0]
    _, _, age = struct.unpack_from("<3I", data, first * block_size)
    return data[first * block_size + 12 : first * block_size + 28], age


def bundle(files: list[Path], out: Path) -> str:
    """Copy the images the capture names, and their PDBs, into out/images; return the review.

    An image whose size or checksum differs from the capture's module, or a PDB whose GUID or
    age differs from its image's debug directory, is refused: the views would read another
    build. A file the capture does not name is listed as not bundled.
    """
    import duckdb

    connection = duckdb.connect(str(out / "cpu.db"), read_only=True)
    try:
        modules = connection.execute("SELECT path, size, checksum FROM Module").fetchall()
    finally:
        connection.close()
    by_name = {}
    for path, size, checksum in modules:
        by_name[PureWindowsPath(str(path)).name.casefold()] = (size, checksum)
    pdbs = {path.name.casefold(): path for path in files if path.suffix.lower() == ".pdb"}
    lines, chosen = [], []
    for path in files:
        if not path.is_file():
            raise Refused(f"{path} is missing; pass the images the capture was recorded with")
        if path.suffix.lower() == ".pdb":
            continue
        module = by_name.get(path.name.casefold())
        if module is None:
            lines.append(f"not bundled: {path.name} is not a module of the capture")
            continue
        data = path.read_bytes()
        checksum, size = pe_identity(data)
        if (size, checksum) != (module[0], module[1]):
            raise Refused(
                f"{path.name} has size {size} and checksum {checksum}, the capture's module "
                f"size {module[0]} and checksum {module[1]}; bundle the image the capture was "
                "recorded with"
            )
        lines.append(f"== bundled images/{path.name}")
        lines.append(f"size {size}, checksum {checksum}: matches the capture's module")
        record = debug_record(data)
        chosen.append((path, data))
        if record is None:
            lines.append("debug directory: none")
        else:
            guid, age, pdb_path = record
            lines.append(f"debug directory PDB: {pdb_path}")
            lines.append(f"GUID {guid.hex()} age {age}")
            pdb = pdbs.get(PureWindowsPath(pdb_path).name.casefold())
            if pdb is not None:
                pdb_data = pdb.read_bytes()
                if pdb_record(pdb_data) != (guid, age):
                    raise Refused(
                        f"{pdb.name} has another GUID or age than {path.name}'s debug "
                        "directory; bundle the PDB built with the image"
                    )
                chosen.append((pdb, pdb_data))
        lines.append(f"strings ({len(file_strings(data))})")
        lines += file_strings(data)
    bundled = {path.name.casefold() for path, _ in chosen}
    for name, pdb in pdbs.items():
        if name not in bundled:
            lines.append(f"not bundled: {pdb.name} belongs to no bundled image")
    for path, data in chosen:
        if path.suffix.lower() == ".pdb":
            guid, age = pdb_record(data)
            strings = file_strings(data)
            lines.append(f"== bundled images/{path.name}")
            lines.append(f"GUID {guid.hex()} age {age}: matches the image's GUID and age")
            lines.append(f"strings ({len(strings)})")
            lines += strings
    if chosen:
        (out / "images").mkdir()
        for path, data in chosen:
            (out / "images" / path.name).write_bytes(data)
    return "\n".join(lines) + "\n" if lines else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", type=Path, help="translated session directory")
    parser.add_argument(
        "--keep-process",
        nargs="+",
        required=True,
        metavar="NAME",
        help="process image names to keep, e.g. defect_lab.exe",
    )
    parser.add_argument("--out", type=Path, required=True, help="new or empty directory")
    parser.add_argument(
        "--bundle",
        nargs="+",
        type=Path,
        default=[],
        metavar="FILE",
        help="images and PDBs the capture was recorded with, copied into <out>/images",
    )
    args = parser.parse_args(argv)
    try:
        review = anonymize(args.session, args.keep_process, args.out, args.bundle)
    except Refused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    names = host_names()
    found = disclosures(review.read_text(encoding="utf-8"), names)
    for path in sorted((args.out / "images").glob("*")):
        found += [f"{item} in images/{path.name}" for item in file_disclosures(path, names)]
    if found:
        print(
            f"error: {review} still contains {'; '.join(found)}; do not hand these files "
            "over, and report the table and column review.txt shows it in",
            file=sys.stderr,
        )
        return 1
    print(review)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
