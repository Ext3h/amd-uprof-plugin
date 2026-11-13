"""Keep only named processes of a translated AMD uProf capture and remove what names the host.

Usage:
    python trials/anonymize_capture.py <session_dir> --keep-process <name> [<name> ...]
        --out <dir>

<session_dir> is a translated session (session.uprof and cpu.db, as `uprof.py translate`
writes it). <out_dir> must be new or empty. It receives:

- cpu.db: a copy where every process other than the kept ones is deleted with its threads,
  module instances and samples, modules and functions no kept sample uses are deleted, HostInfo
  is emptied, thread names are blanked, and every path outside the Windows and system library
  folders becomes c:\\capture\\<file name> (or /capture/<file name>).
- session.uprof: only the keys the tools read, with host values and paths replaced.
- review.txt: every table with every column as plain text, then the sorted distinct strings of
  the capture, for a person to review before the files leave the machine.

It exits 1 when review.txt still contains this machine's name, the user's name or a user
profile path, and 2 when the input is not usable (missing files, an unknown process, or a
cpu.db schema the tools do not read). Needs Python 3.9+ and the duckdb package.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

# Columns the tools read; a cpu.db without them is refused rather than half-filtered.
REQUIRED = {"Process": ("id", "name"), "Module": ("id", "path"),
            "Function": ("id", "moduleId", "name"),
            "UnifiedSampleSeries": ("processId", "moduleId", "functionId", "moduleRVA")}
# Folders whose paths name no user: kept as they are.
SYSTEM_PREFIXES = ("c:\\windows\\", "/usr/", "/lib/", "/lib64/")
WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
PROFILE_PATH = re.compile(r"[\\/](users|home)[\\/][^\\/\s\"']+", re.IGNORECASE)
SCHEDULE_KEYS = ("elapsed", "start", "end", "startTs", "endTs", "profileDuration")


class Refused(Exception):
    """The input cannot be anonymized; the message is the cause."""


def host_names() -> list[str]:
    """This machine's name and the user's name, as a capture would spell them."""
    names = {os.environ.get("COMPUTERNAME", ""), socket.gethostname(),
             os.environ.get("USERNAME", ""), os.environ.get("USER", "")}
    try:
        names.add(getpass.getuser())
    except Exception:  # noqa: BLE001 - no user name to add
        pass
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
    return {name: kind for name, kind in connection.execute(
        "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ?",
        [table]).fetchall()}


def tables_of(connection) -> list[str]:
    return [name for (name,) in connection.execute(
        "SELECT table_name FROM duckdb_tables() ORDER BY table_name").fetchall()]


def check_schema(connection) -> None:
    present = set(tables_of(connection))
    for table, needed in REQUIRED.items():
        columns = columns_of(connection, table) if table in present else {}
        for column in needed:
            if column not in columns:
                raise Refused(f"cpu.db lacks {table}.{column}; the tools read Process, Module, "
                              "Function and UnifiedSampleSeries as AMD uProf's translate "
                              "writes them, so this capture is not one they can profile")


def kept_processes(connection, names: list[str]) -> list[int]:
    wanted = {name.lower() for name in names}
    found: dict[str, list[int]] = {}
    for pid, process in connection.execute("SELECT id, name FROM Process").fetchall():
        base = PureWindowsPath(str(process or "").replace("/", "\\")).name.lower()
        if base in wanted:
            found.setdefault(base, []).append(int(pid))
    missing = sorted(wanted - set(found))
    if missing:
        raise Refused(f"no process named {', '.join(missing)} in the capture; name the process "
                      "image as `uprof.py profile --by process` prints it")
    return sorted(pid for pids in found.values() for pid in pids)


def filter_rows(connection, pids: list[int]) -> None:
    pid_list = ", ".join(str(pid) for pid in pids)
    tables = tables_of(connection)
    for table in tables:
        columns = columns_of(connection, table)
        if table == "Process":
            connection.execute(f'DELETE FROM "Process" WHERE id NOT IN ({pid_list})')
        elif "processId" in columns:
            connection.execute(f'DELETE FROM "{table}" WHERE "processId" IS NULL '
                               f'OR "processId" NOT IN ({pid_list})')
        if "host" in table.lower():
            connection.execute(f'DELETE FROM "{table}"')
    modules = {row[0] for row in connection.execute(
        "SELECT DISTINCT moduleId FROM UnifiedSampleSeries WHERE moduleId IS NOT NULL").fetchall()}
    if "ModuleInstance" in tables:
        modules |= {row[0] for row in connection.execute(
            "SELECT DISTINCT moduleId FROM ModuleInstance").fetchall()}
    module_list = ", ".join(str(int(m)) for m in modules) or "NULL"
    for table in tables:
        columns = columns_of(connection, table)
        if table == "Module":
            connection.execute(f'DELETE FROM "Module" WHERE id NOT IN ({module_list})')
        elif "moduleId" in columns and table not in ("UnifiedSampleSeries", "ModuleInstance"):
            connection.execute(f'DELETE FROM "{table}" WHERE "moduleId" IS NULL '
                               f'OR "moduleId" NOT IN ({module_list})')


def rewrite_strings(connection) -> None:
    for table in tables_of(connection):
        for column, kind in columns_of(connection, table).items():
            if kind != "VARCHAR":
                continue
            if table == "ProcessThread" and column == "threadName":
                connection.execute(f'UPDATE "{table}" SET "{column}" = \'\'')
                continue
            values = [value for (value,) in connection.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            ).fetchall()]
            for value in values:
                replaced = rewrite_path(value)
                if replaced != value:
                    connection.execute(f'UPDATE "{table}" SET "{column}" = ? '
                                       f'WHERE "{column}" = ?', [replaced, value])


def sanitize_command(command: object) -> object:
    if not isinstance(command, str):
        return command
    return " ".join("<PATH>" if WINDOWS_PATH.match(token.strip('"'))
                    or token.strip('"').startswith("/") else token
                    for token in command.split())


def reduced_meta(meta: dict) -> dict:
    collection = meta.get("collection") or {}
    target = collection.get("target") or {}
    schedule = collection.get("schedule") or {}
    auxiliary = meta.get("auxiliary") or {}
    reduced_target: dict[str, object] = {"systemWide": target.get("systemWide")}
    if target.get("attachedProcesses") is not None:
        reduced_target["attachedProcesses"] = [
            {"processId": item.get("processId")} for item in target["attachedProcesses"]
            if isinstance(item, dict)]
    if target.get("launchApp"):
        reduced_target["launchApp"] = PureWindowsPath(
            str(target["launchApp"]).replace("/", "\\")).name
    result: dict[str, object] = {
        "collection": {"events": collection.get("events") or {},
                       "schedule": {key: schedule[key] for key in SCHEDULE_KEYS
                                    if key in schedule},
                       "target": reduced_target},
        "auxiliary": {key: auxiliary[key] for key in ("eventColumnIds", "ibsOpSampling")
                      if key in auxiliary},
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


def anonymize(session: Path, keep: list[str], out: Path) -> Path:
    import duckdb

    for name in ("session.uprof", "cpu.db"):
        if not (session / name).is_file():
            raise Refused(f"{session / name} is missing; pass a translated session directory")
    if out.exists() and any(out.iterdir()):
        raise Refused(f"{out} is not empty; pass a new directory")
    source = duckdb.connect(str(session / "cpu.db"), read_only=True)
    try:
        check_schema(source)
        kept = kept_processes(source, keep)
    finally:
        source.close()
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work.db"
    shutil.copyfile(session / "cpu.db", work)
    meta = reduced_meta(json.loads((session / "session.uprof").read_text(
        encoding="utf-8-sig")))
    (out / "session.uprof").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    try:
        connection = duckdb.connect(str(work))
        try:
            filter_rows(connection, kept)
            rewrite_strings(connection)
        finally:
            connection.close()
        # A fresh file holds only the kept rows; the working copy keeps the deleted rows' space.
        # Small blocks keep a capture of a few thousand rows at about 0.5 MB instead of ~5 MB.
        connection = duckdb.connect()
        try:
            connection.execute(f"ATTACH '{work.as_posix()}' AS work (READ_ONLY)")
            connection.execute(f"ATTACH '{(out / 'cpu.db').as_posix()}' AS packed "
                               "(BLOCK_SIZE 16384)")
            connection.execute("COPY FROM DATABASE work TO packed")
        finally:
            connection.close()
    finally:
        work.unlink(missing_ok=True)
        Path(str(work) + ".wal").unlink(missing_ok=True)
    connection = duckdb.connect(str(out / "cpu.db"), read_only=True)
    try:
        review = dump(connection, meta)
    finally:
        connection.close()
    (out / "review.txt").write_text(review, encoding="utf-8")
    return out / "review.txt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", type=Path, help="translated session directory")
    parser.add_argument("--keep-process", nargs="+", required=True, metavar="NAME",
                        help="process image names to keep, e.g. defect_lab.exe")
    parser.add_argument("--out", type=Path, required=True, help="new or empty directory")
    args = parser.parse_args(argv)
    try:
        review = anonymize(args.session, args.keep_process, args.out)
    except Refused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    found = disclosures(review.read_text(encoding="utf-8"), host_names())
    if found:
        print(f"error: {review} still contains {'; '.join(found)}; do not hand these files "
              "over, and report the table and column review.txt shows it in", file=sys.stderr)
        return 1
    print(review)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
