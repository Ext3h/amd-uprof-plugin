"""DuckDB 2.x access: open the capture read-only, bind the uprof_* values, run the SQL files.

Values reach DuckDB through prepared parameters (`SET VARIABLE name = ?`); paths, patterns and
numbers stay bound. views.sql reads them with getvariable() and guards the capture's tables and
columns before any query runs. Rendering is uprof_output's job; this module only fetches.
"""

from __future__ import annotations

import contextlib
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from uprof_output import header_text, json_document, markdown_table, write_payload
from uprof_util import UprofError

_CREATE_VIEW = re.compile(
    r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?TEMP\s+VIEW\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_VIEW_NAME = re.compile(r"\bv_[a-z0-9_]+\b", re.IGNORECASE)


def _statement_sql(duckdb: Any, statement: Any) -> str:
    """The statement's text up to its terminating semicolon.

    extract_statements() attaches following comments to the statement, so the raw statement text
    is used rather than a re-rendered form. tokenize() reports UTF-8 byte offsets and ignores
    semicolons inside strings and comments.
    """
    text = statement.query
    data = text.encode("utf-8")
    for position, kind in duckdb.tokenize(text):
        if kind == duckdb.token_type.operator and data[position:position + 1] == b";":
            return data[:position].decode("utf-8").rstrip()
    return text.rstrip()


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:
        raise UprofError(
            "the DuckDB Python package is not installed; install it with: "
            f"{sys.executable} -m pip install duckdb") from exc
    try:
        major = int(duckdb.__version__.split(".")[0])
    except (AttributeError, ValueError):
        major = 0
    if major < 2:
        raise UprofError(
            f"DuckDB {duckdb.__version__} is too old; these queries need 2.x. Upgrade with: "
            f"{sys.executable} -m pip install --upgrade duckdb")
    return duckdb


def connect(cpu_db: Path | str) -> Any:
    duckdb = _duckdb()
    try:
        return duckdb.connect(str(cpu_db), read_only=True)
    except (duckdb.Error, OSError) as exc:
        raise UprofError(f"cannot open {cpu_db} read-only: {exc}") from exc


def set_variables(connection: Any, variables: dict[str, str]) -> None:
    duckdb = _duckdb()
    for name, value in variables.items():
        if not name.startswith("uprof_") or not name.replace("_", "").isalnum():
            raise UprofError(f"internal error: invalid SQL variable name {name!r}")
        try:
            connection.execute(f"SET VARIABLE {name} = ?", [str(value)])
        except duckdb.Error as exc:
            raise UprofError(f"cannot set {name}: {exc}") from exc


def _read_sql(sql_path: Path) -> str:
    try:
        with open(sql_path, "r", encoding="utf-8-sig") as handle:
            return handle.read()
    except OSError as exc:
        raise UprofError(f"cannot read SQL file {sql_path}: {exc}") from exc


def _without_comments(duckdb: Any, text: str) -> str:
    """text with SQL comments blanked, so a view named in prose is not read as a dependency.

    tokenize() reports UTF-8 byte offsets, so the masking runs on bytes; newlines are kept.
    """
    data = text.encode("utf-8")
    masked = bytearray(data)
    tokens = duckdb.tokenize(text)
    for index, (start, kind) in enumerate(tokens):
        if kind != duckdb.token_type.comment:
            continue
        end = tokens[index + 1][0] if index + 1 < len(tokens) else len(data)
        for offset in range(start, end):
            if masked[offset] not in (0x0A, 0x0D):
                masked[offset] = 0x20
    return masked.decode("utf-8")


def _view_plan(duckdb: Any, statements: Sequence[Any],
               roots: set[str], source: str) -> tuple[set[str], list[str | None]]:
    """The CREATE VIEW names to execute, and each statement's view name (None when not a view).

    A view name inside a dollar-quoted string counts as a reference: query(CASE ...) chooses that
    branch at run time, so it is a real dependency for one capture kind. A name in a comment does
    not count. Over-retention from a string is safe; a missing dependency is not.
    """
    graph: dict[str, set[str]] = {}
    names: list[str | None] = []
    for statement in statements:
        sql = _without_comments(duckdb, statement.query)
        match = _CREATE_VIEW.match(sql)
        name = None if match is None else match.group(1).lower()
        names.append(name)
        if name is not None:
            graph[name] = {ref.lower() for ref in _VIEW_NAME.findall(sql)} - {name}
    unknown = sorted(root for root in roots if root not in graph)
    if unknown:
        raise UprofError(
            f"internal error: {source} does not define {', '.join(unknown)}")
    needed: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in needed or name not in graph:
            continue
        needed.add(name)
        pending.extend(graph[name] - needed)
    return needed, names


def run_script(connection: Any, sql_path: Path, views: set[str] | None = None) -> None:
    """Execute sql_path's statements in order.

    views restricts the CREATE VIEW statements to those views and the views they reference.
    Everything else, including macros, guards, SET VARIABLE and CREATE TEMP TABLE, always runs so
    a table that a guard refers to exists. None loads every view.
    """
    text = _read_sql(sql_path)
    statements = connection.extract_statements(text)
    if views is None:
        for statement in statements:
            connection.execute(statement)
        return
    duckdb = _duckdb()
    needed, names = _view_plan(duckdb, statements, views, sql_path.name)
    for statement, name in zip(statements, names):
        if name is None or name in needed:
            connection.execute(statement)


@contextlib.contextmanager
def open_query(cpu_db: Path, view_files: list[Path], variables: dict[str, str],
               views: set[str] | None = None) -> Iterator[Any]:
    duckdb = _duckdb()
    try:
        with connect(cpu_db) as connection:
            set_variables(connection, variables)
            for view_file in view_files:
                run_script(connection, view_file, views)
            yield connection
    except UprofError:
        raise
    except duckdb.Error as exc:
        raise UprofError(str(exc)) from exc


def fetch_typed(connection: Any, sql_path: Path) -> tuple[list[str], list[str], list[tuple]]:
    """Column names, type names and rows of the first SELECT in sql_path."""
    duckdb = _duckdb()
    text = _read_sql(sql_path)
    select = next((statement for statement in connection.extract_statements(text)
                   if statement.type == duckdb.StatementType.SELECT), None)
    if select is None:
        return [], [], []
    relation = connection.sql(_statement_sql(duckdb, select))
    return relation.columns, [str(kind) for kind in relation.types], relation.fetchall()


def fetch(connection: Any, sql_path: Path) -> tuple[list[str], list[tuple]]:
    """Columns and rows of the first SELECT in sql_path."""
    columns, _, rows = fetch_typed(connection, sql_path)
    return columns, rows


def output_rows(columns: Sequence[str], types: Sequence[str], rows: Sequence[tuple],
                fields: Sequence[tuple[str, str]], json_mode: bool, out_path: Path | None,
                legend: dict[str, object] | None = None,
                contexts: Sequence[object | None] | None = None,
                context_help: dict[str, object] | None = None) -> None:
    """Render an already fetched result with its header fields and write it.

    contexts adds the per-row context objects to JSON output only; context_help is the
    once-per-document help. The Markdown bytes are the same with or without either.
    """
    if json_mode:
        text = json_document(fields, columns, rows, legend, contexts, context_help)
    else:
        text = header_text(fields) + markdown_table(columns, types, rows)
    write_payload(text, out_path)


def output(connection: Any, query_file: Path, fields: Sequence[tuple[str, str]],
           json_mode: bool, out_path: Path | None,
           legend: dict[str, object] | None = None,
           contexts: Sequence[object | None] | None = None,
           context_help: dict[str, object] | None = None) -> None:
    """Render the first SELECT of query_file with its header fields and write the result."""
    columns, types, rows = fetch_typed(connection, query_file)
    output_rows(columns, types, rows, fields, json_mode, out_path, legend, contexts,
                context_help)
