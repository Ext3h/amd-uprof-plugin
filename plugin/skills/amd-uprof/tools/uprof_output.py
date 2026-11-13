"""Render query rows as a Markdown pipe table or a JSON document and write them as UTF-8 with LF.

Both renderers walk the same column list and row tuples, so the table and the JSON document carry
the same values; only the Markdown escaping differs. Control characters are display-escaped in
the table and header lines so no value can split a row or a line; JSON carries the true value.
stdout is written through its binary buffer so Windows does not translate newlines, and
--out-file writes exactly those bytes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from uprof_util import UprofError

_NUMERIC_TYPE_PREFIXES: Final = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
    "FLOAT",
    "REAL",
    "DOUBLE",
    "DECIMAL",
)
_NULL_CELL: Final = "NULL"
_CONTROL_ESCAPES: Final = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _is_numeric(type_name: str) -> bool:
    return type_name.upper().startswith(_NUMERIC_TYPE_PREFIXES)


def _escape_controls(text: str) -> str:
    """Replace C0 controls and DEL with visible escapes so a value stays on one line.

    JSON carries the original characters; the table shows \\n, \\r, \\t or \\xNN. Escapes are
    introduced after backslash doubling, so a literal backslash before one stays unambiguous.
    """
    parts: list[str] = []
    for char in text:
        escape = _CONTROL_ESCAPES.get(char)
        if escape is not None:
            parts.append(escape)
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            parts.append(f"\\x{ord(char):02X}")
        else:
            parts.append(char)
    return "".join(parts)


def _header_value(text: str) -> str:
    """Control-escape a header value; values with controls also double backslashes.

    A control-free path keeps its single backslashes, so existing header bytes do not change.
    """
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        text = text.replace("\\", "\\\\")
    return _escape_controls(text)


def _cell(value: object) -> str:
    if value is None:
        return _NULL_CELL
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _escape(text: str) -> str:
    """Escape one Markdown table cell so the original text round-trips.

    HTML-like text is entity-escaped rather than wrapped in a code span, so one rule covers every
    cell. Backslashes are doubled before the pipe escape and before control escapes; a lone
    backslash would otherwise consume the backslash that starts an escape.
    """
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _escape_controls(text.replace("\\", "\\\\"))
    return text.replace("|", "\\|")


def markdown_table(
    columns: Sequence[str], type_names: Sequence[str], rows: Sequence[Sequence[object]]
) -> str:
    """A pipe table with right-aligned numeric columns and escaped text cells."""
    numeric = [_is_numeric(name) for name in type_names]
    lines = [
        "| " + " | ".join(_escape(column) for column in columns) + " |",
        "| " + " | ".join("---:" if right else "---" for right in numeric) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape(_cell(value)) for value in row) + " |")
    return "\n".join(lines) + "\n"


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def json_document(
    fields: Sequence[tuple[str, str]],
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    legend: dict[str, object] | None = None,
    contexts: Sequence[object | None] | None = None,
    context_help: dict[str, object] | None = None,
) -> str:
    """One object with the header fields, the legend and a rows array keyed by column names.

    contexts, when given, holds one entry per row; a non-None entry becomes that row's
    "context" object. context_help is emitted once, before the rows. Markdown rendering never
    receives either.
    """
    if contexts is not None and len(contexts) != len(rows):
        raise ValueError(f"{len(contexts)} contexts for {len(rows)} rows")
    payload: dict[str, object] = {name: text for name, text in fields}
    if legend is not None:
        payload["legend"] = legend
    if context_help is not None:
        payload["context_help"] = context_help
    records: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        record = {column: _json_value(value) for column, value in zip(columns, row)}
        if contexts is not None and contexts[index] is not None:
            record["context"] = contexts[index]
        records.append(record)
    payload["rows"] = records
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def header_text(fields: Sequence[tuple[str, str]]) -> str:
    """Header lines with control characters display-escaped so each field stays on one line."""
    return "".join(f"{name}: {_header_value(value)}\n" for name, value in fields)


def write_payload(text: str, out_path: Path | None) -> None:
    """Write text as UTF-8 (no BOM) with LF newlines, to stdout or out_path."""
    data = text.encode("utf-8")
    if out_path is not None:
        _write_file(data, out_path)
        return
    buffer = getattr(sys.stdout, "buffer", None)
    try:
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:
            sys.stdout.write(data.decode("utf-8"))
            sys.stdout.flush()
    except BrokenPipeError:
        raise
    except OSError as exc:
        raise UprofError(f"cannot write to stdout: {exc}") from exc


def _write_file(data: bytes, out_path: Path) -> None:
    """Write through a new temporary file so a partial table never replaces the destination.

    The temporary file is created exclusively under a unique name, so a link planted beside the
    destination is never written through and concurrent runs never share one.
    """
    temp_path: Path | None = None
    try:
        handle, name = tempfile.mkstemp(
            prefix=f".{out_path.name}-", suffix=".tmp", dir=out_path.parent
        )
        temp_path = Path(name)
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        if os.name != "nt":
            # mkstemp creates 0600; give the table the mode a plain create would.
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(temp_path, 0o666 & ~umask)
        os.replace(temp_path, out_path)
    except OSError as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise UprofError(f"cannot write {out_path}: {exc}") from exc
