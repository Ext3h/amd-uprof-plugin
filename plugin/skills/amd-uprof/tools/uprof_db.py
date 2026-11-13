"""DuckDB 2.x access: open the capture read-only, bind the uprof_* values, run the SQL files.

Values reach DuckDB through prepared parameters (`SET VARIABLE name = ?`); paths, patterns and
numbers stay bound. views.sql reads them with getvariable() and guards the capture's tables and
columns before any query runs. Rendering is uprof_output's job; this module only fetches.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final

import uprof_shell
from uprof_output import header_text, json_document, markdown_table, write_payload
from uprof_util import UprofError


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


# No connection installs or loads a DuckDB extension on its own: a capture file or a path
# must never make DuckDB fetch or run code that the skill did not ask for.
_NO_AUTO_EXTENSIONS: Final = {"autoinstall_known_extensions": False,
                              "autoload_known_extensions": False}
CAPTURE_ALIAS: Final = "uprof_capture"
_SHOWN_OBJECTS: Final = 5

# A uProf translation writes base tables only. Any macro, view or generated column in a capture
# is foreign SQL; once the capture is on the search path, a macro named like a builtin would
# replace that builtin in every query. The lookup runs before the capture is on the search path,
# so its own function calls resolve to the builtins.
_STORED_SQL: Final = """
SELECT kind, name FROM (
    SELECT 'macro' AS kind, function_name AS name FROM duckdb_functions()
    WHERE database_name = $alias AND NOT internal
    UNION ALL
    SELECT 'view', view_name FROM duckdb_views() WHERE database_name = $alias AND NOT internal
    UNION ALL
    SELECT 'generated column', table_name || '.' || column_name FROM duckdb_columns()
    WHERE database_name = $alias AND is_generated)
ORDER BY kind, name
"""


def memory_connection() -> Any:
    """An in-memory DuckDB connection that installs and loads no extension on its own."""
    import duckdb
    return duckdb.connect(config=dict(_NO_AUTO_EXTENSIONS))


def file_connection(path: Path | str) -> Any:
    """A read-only connection to one of the skill's own DuckDB files, without extension autoload.

    DuckDB errors pass through unchanged so the caller can tell a storage version mismatch.
    """
    import duckdb
    return duckdb.connect(str(path), read_only=True, config=dict(_NO_AUTO_EXTENSIONS))


def _shown_name(name: object) -> str:
    """A catalog name as one printable token: a hostile name cannot add or forge output lines."""
    return str(name).encode("unicode_escape").decode("ascii")[:80]


def _stored_sql_error(cpu_db: Path | str, objects: list[tuple[str, str]]) -> UprofError:
    shown = ", ".join(f"{kind} {_shown_name(name)}" for kind, name in objects[:_SHOWN_OBJECTS])
    more = len(objects) - _SHOWN_OBJECTS
    if more > 0:
        shown += f" and {more} more"
    script, script_data = uprof_shell.path_arg(Path(__file__).resolve().parent / "uprof.py",
                                               "<UPROF_PY>")
    lines = [
        f"{cpu_db} defines SQL objects that a uProf translation never writes ({shown}); the "
        "skill does not run SQL stored in a capture, so it is not queried",
        f"ALTERNATIVE: translate the raw capture again: python {script} translate --session "
        "<RAW_SESSION_DIR> --out-dir <NEW_TR_DIR>"]
    # A path that is not plain is a placeholder in the command and named here as data only.
    if script_data:
        lines.append(script_data)
    return UprofError("\n".join(lines))


def connect(cpu_db: Path | str) -> Any:
    """The capture attached read-only to an in-memory database, refused if it stores SQL."""
    duckdb = _duckdb()
    connection = memory_connection()
    try:
        quoted = str(cpu_db).replace("'", "''")
        connection.execute(f"ATTACH '{quoted}' AS {CAPTURE_ALIAS} (READ_ONLY)")
        objects = connection.execute(_STORED_SQL, {"alias": CAPTURE_ALIAS}).fetchall()
        if not objects:
            connection.execute(f"USE {CAPTURE_ALIAS}")
    except (duckdb.Error, OSError) as exc:
        connection.close()
        raise UprofError(f"cannot open {cpu_db} read-only: {exc}") from exc
    if objects:
        connection.close()
        raise _stored_sql_error(cpu_db, objects)
    return connection


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


def run_script(connection: Any, sql_path: Path) -> None:
    text = _read_sql(sql_path)
    for statement in connection.extract_statements(text):
        connection.execute(statement)


@contextlib.contextmanager
def open_query(cpu_db: Path, view_files: list[Path],
               variables: dict[str, str]) -> Iterator[Any]:
    duckdb = _duckdb()
    try:
        with connect(cpu_db) as connection:
            set_variables(connection, variables)
            for view_file in view_files:
                run_script(connection, view_file)
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
