"""Build the committed timer capture (tests/fixtures/timer) as a session directory.

session.uprof is copied; cpu.db is rebuilt from cpu_tables.json, so no DuckDB file is committed.
The image heap_churn.exe and its PDB stay in the fixture directory; pass that directory with
--image-path, because Module.path holds a placeholder, not a path on this host.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "timer"
IMAGE_DIR = FIXTURE
EVENT_COLUMN = "event#0x200000000e000"
IMAGE_KEY = "c:/fixture/heap_churn.exe"
NTDLL_KEY = "c:/windows/system32/ntdll.dll"


def tables() -> dict:
    return json.loads((FIXTURE / "cpu_tables.json").read_text(encoding="utf-8"))


def meta() -> dict:
    return json.loads((FIXTURE / "session.uprof").read_text(encoding="utf-8"))


def build(session: Path) -> Path:
    """Write the fixture session into session (created); return it."""
    import duckdb

    session.mkdir(parents=True)
    shutil.copyfile(FIXTURE / "session.uprof", session / "session.uprof")
    connection = duckdb.connect(str(session / "cpu.db"))
    try:
        for name, table in tables().items():
            columns = ", ".join(f'"{column}" {kind}' for column, kind in table["columns"])
            connection.execute(f'CREATE TABLE "{name}" ({columns})')
            rows = table["rows"]
            if rows:
                # One multi-row INSERT: executemany costs seconds for the sample table.
                row = "(" + ", ".join("?" for _ in table["columns"]) + ")"
                connection.execute(
                    f'INSERT INTO "{name}" VALUES ' + ", ".join(row for _ in rows),
                    [value for source in rows for value in source],
                )
    finally:
        connection.close()
    return session


def derive(
    base: Path,
    session: Path,
    meta_override: dict | None = None,
    drop_columns: tuple[str, ...] = (),
    keep_rows: int | None = None,
) -> Path:
    """Copy the built session base into session (created) and apply one variant; return it.

    meta_override replaces session.uprof; drop_columns removes those UnifiedSampleSeries
    columns from cpu.db; keep_rows keeps only the first that many UnifiedSampleSeries rows.
    """
    import duckdb

    shutil.copytree(base, session)
    if meta_override is not None:
        (session / "session.uprof").write_text(json.dumps(meta_override), encoding="utf-8")
    if drop_columns or keep_rows is not None:
        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            for column in drop_columns:
                connection.execute(f'ALTER TABLE UnifiedSampleSeries DROP COLUMN "{column}"')
            if keep_rows is not None:
                # build inserts the rows in cpu_tables.json order, so rowid is that order.
                connection.execute("DELETE FROM UnifiedSampleSeries WHERE rowid >= ?", [keep_rows])
        finally:
            connection.close()
    return session
