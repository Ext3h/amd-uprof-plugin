"""Shared primitives: the CLI error type, stream setup, session copies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

DUMP_FILENAMES: Final = MappingProxyType(
    {
        "ibsop-event-dump": "IbsOpDump.csv",
        "ibsfetch-event-dump": "IbsFetchDump.csv",
    }
)

COPY_DIRS: Final = ("cpu", "metadata", ".config", ".log", "sources", "binaries")

# Used when the skill is copied loose and no .claude-plugin/plugin.json sits above it. Keep it in
# step with the released plugin version.
_BUILTIN_VERSION: Final = "2.1.0"
_PLUGIN_MANIFEST: Final = Path(".claude-plugin") / "plugin.json"


class UprofError(RuntimeError):
    """A failure the CLI prints as `error: ...` and exits 2 for."""


def skill_identity(skill_dir: Path | None = None) -> str:
    """Name the skill version and the installed copy that produced an output line.

    Two copies can share a version and still differ, so the skill directory is part of the value.
    The nearest plugin.json above the skill supplies the version; without one (a loose copy) the
    built-in version is reported.
    """
    root = (skill_dir or Path(__file__).resolve().parent.parent).resolve()
    for candidate in (root, *root.parents):
        manifest = candidate / _PLUGIN_MANIFEST
        if not manifest.is_file():
            continue
        try:
            version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError, TypeError):
            break
        if isinstance(version, str) and version:
            return f"amd-uprof {version} (plugin.json {manifest}); skill dir {root}"
        break
    return f"amd-uprof {_BUILTIN_VERSION} (built-in); skill dir {root}"


_TIME_FILTER = re.compile(r"(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)")


def parse_time_filter(value: str) -> tuple[Decimal, Decimal] | None:
    """Parse T1:T2 seconds into Decimal bounds; None when the text is not T1:T2."""
    match = _TIME_FILTER.fullmatch(value.strip())
    if not match:
        return None
    return Decimal(match.group(1)), Decimal(match.group(2))


_CONTROL_ESCAPES: Final = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
# Zero-width and bidi controls hide or reorder the text around them.
_HIDDEN: Final = frozenset([*range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2066, 0x206A)])
INLINE_LIMIT: Final = 512


def _unsafe(char: str) -> bool:
    """C0 controls, DEL, C1 controls and zero-width or bidi controls."""
    code = ord(char)
    return code < 0x20 or 0x7F <= code <= 0x9F or code in _HIDDEN


def has_controls(text: str) -> bool:
    return any(_unsafe(char) for char in text)


def escape_controls(text: str, keep: str = "") -> str:
    """Replace every character that can split a line, start a terminal escape or hide or reorder
    text with a visible escape: \\n, \\r, \\t, \\xNN up to U+00FF, else \\uNNNN. Characters in
    keep pass unchanged."""
    if not has_controls(text):
        return text
    parts: list[str] = []
    for char in text:
        if char in keep or not _unsafe(char):
            parts.append(char)
        elif char in _CONTROL_ESCAPES:
            parts.append(_CONTROL_ESCAPES[char])
        elif ord(char) <= 0xFF:
            parts.append(f"\\x{ord(char):02X}")
        else:
            parts.append(f"\\u{ord(char):04X}")
    return "".join(parts)


# The userinfo of a URL: after "scheme://", everything up to the last "@" before the first "/" or
# whitespace, so a raw "@" in a password is covered and an "@" in a path or in prose is not.
_CREDENTIALS: Final = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/]+@")


def redact_credentials(text: str) -> str:
    """text with the user and password of every URL replaced by ***, before it is printed or
    stored; symbol-server URLs can carry credentials."""
    return _CREDENTIALS.sub(r"\1***@", text)


def one_line(text: object) -> str:
    """text escaped onto one line, for a prose line that embeds capture or tool text."""
    return escape_controls(str(text))


def inline(value: object) -> str:
    """A capture value (name, path) embedded in a prose line: on one line, at most INLINE_LIMIT
    characters, so a hostile value can neither forge a line nor flood the output."""
    text = str(value)
    cut = len(text) - INLINE_LIMIT
    if cut <= 0:
        return one_line(text)
    return one_line(text[:INLINE_LIMIT]) + f"...[truncated {cut} chars]"


class LinesError(UprofError):
    """An error of several lines, each escaped on its own: capture text inside a line cannot
    start a new one, while the tool's own ALTERNATIVE and data lines stay separate."""

    def __init__(self, lines: Iterable[str]) -> None:
        super().__init__("\n".join(one_line(line) for line in lines if line))


class _ControlSafeStream:
    """A text stream that writes control characters visibly; newlines and tabs pass."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        self._stream.write(escape_controls(text, keep="\n\t"))
        return len(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def configure_streams() -> None:
    """Keep non-ASCII symbol names readable on a legacy console code page, and never let a
    control character from capture or tool text reach the terminal raw."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not isinstance(sys.stdout, _ControlSafeStream):
        sys.stdout = _ControlSafeStream(sys.stdout)
    if not isinstance(sys.stderr, _ControlSafeStream):
        sys.stderr = _ControlSafeStream(sys.stderr)


def absolute(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def canonical(path: os.PathLike[str] | str) -> Path:
    """path absolute, with links, junctions and 8.3 names resolved and case folded where the
    platform folds case.

    Not unique: a UNC path, a mapped drive or a subst drive keeps its own spelling of a local
    directory, and missing trailing components keep theirs. Decide identity with same_path or
    contains, which also compare volume and file IDs.
    """
    full = absolute(path)
    try:
        full = Path(os.path.realpath(full))
    except (OSError, ValueError):
        pass
    return Path(os.path.normcase(os.fspath(full)))


# GetDriveTypeW's value for a mapped network drive.
_DRIVE_REMOTE: Final = 4

# Upper-case prefixes that Win32 resolves in the NT object namespace (\??\UNC\host\share,
# \Device\Mup\host\share), where a path may name a share or a device.
_NT_PREFIXES: Final = ("\\??\\", "\\GLOBAL??\\", "\\DEVICE\\")


def is_remote_path(path: os.PathLike[str] | str) -> bool:
    """Whether path is a UNC, NT-namespace, device or mapped network path.

    Paths from capture data are never opened when this holds: opening a UNC path makes Windows
    authenticate to that host with the user's credentials. NT-namespace paths count even when
    they name a local drive. The user's own --image-path and --symbol-path arguments are not
    checked.
    """
    text = os.fspath(path)
    # Win32 accepts either slash in each separator, so /\host\share is UNC too.
    spelled = text.replace("/", "\\").upper()
    if spelled.startswith(("\\\\", *_NT_PREFIXES)):
        return True
    if os.name == "nt" and len(text) >= 2 and text[1] == ":":
        import ctypes

        try:
            return ctypes.windll.kernel32.GetDriveTypeW(text[:2] + "\\") == _DRIVE_REMOTE
        except (AttributeError, OSError):
            return False
    return False


def same_path(first: os.PathLike[str] | str, second: os.PathLike[str] | str) -> bool:
    """Whether both paths name the same file or directory, however each is spelled.

    Paths that do not exist yet are the same when their leaf names match and their parents are
    the same, so a missing file below a UNC or mapped spelling of its directory still matches.
    """
    try:
        if os.path.samefile(first, second):
            return True
    except (OSError, ValueError):
        pass
    if canonical(first) == canonical(second):
        return True
    first, second = absolute(first), absolute(second)
    if first.parent == first or second.parent == second:
        return False
    if os.path.normcase(first.name) != os.path.normcase(second.name):
        return False
    try:
        if os.path.lexists(first) or os.path.lexists(second):
            return False
    except (OSError, ValueError):
        return False
    return same_path(first.parent, second.parent)


def contains(outer: os.PathLike[str] | str, inner: os.PathLike[str] | str) -> bool:
    """Whether inner is outer or lies below it, however each is spelled.

    Every existing ancestor of inner is compared with outer by volume and file ID, which no
    spelling (UNC, mapped or subst drive, junction, 8.3 name) hides; the canonical string
    prefix covers an outer that does not exist yet.
    """
    if same_path(outer, inner):
        return True
    root, path = canonical(outer), canonical(inner)
    if root in path.parents:
        return True
    try:
        if not os.path.isdir(outer):
            return False
    except (OSError, ValueError):
        return False
    for ancestor in (absolute(inner), *absolute(inner).parents):
        try:
            if os.path.exists(ancestor) and os.path.samefile(outer, ancestor):
                return True
        except (OSError, ValueError):
            continue
    return False


def sha256_file(path: os.PathLike[str] | str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def capture_identity(
    session_dir: os.PathLike[str] | str, known: object = None
) -> list[dict[str, object]] | None:
    """What one capture is: name, size and SHA-256 of every raw cpu/*.prd, in name order.

    Neither the path nor file times identify a capture: copies keep their source's times, and
    another capture can be copied to the same path. session.uprof is not part of it because
    translate rewrites it. known is an earlier result; a file whose volume, file ID, size and
    modification time still match its entry keeps that digest unread, every other file is hashed.
    None when a raw file cannot be read.
    """
    previous = (
        {entry.get("name"): entry for entry in known if isinstance(entry, dict)}
        if isinstance(known, list)
        else {}
    )
    try:
        files = sorted(
            (
                path
                for path in (Path(session_dir) / "cpu").iterdir()
                if path.name.lower().endswith(".prd") and path.is_file()
            ),
            key=lambda path: path.name,
        )
    except FileNotFoundError:
        return []
    except OSError:
        return None
    identity: list[dict[str, object]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            return None
        file_key = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        entry = previous.get(path.name, {})
        digest = (
            entry.get("sha256")
            if stat.st_ino and entry.get("file") == file_key
            else sha256_file(path)
        )
        if not digest:
            return None
        identity.append(
            {"name": path.name, "size": stat.st_size, "sha256": digest, "file": file_key}
        )
    return identity


def same_capture(first: object, second: object) -> bool:
    """Whether two capture_identity results describe the same raw content.

    An empty identity (no raw file) never matches: it cannot tell two captures apart.
    """

    def content(identity: object) -> list[tuple[object, ...]] | None:
        if not isinstance(identity, list):
            return None
        return [
            (entry.get("name"), entry.get("size"), entry.get("sha256"))
            for entry in identity
            if isinstance(entry, dict)
        ]

    left = content(first)
    return bool(left) and left == content(second)


def tail_lines(text: str, count: int = 6) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return " | ".join(lines[-count:])


# DuckDB's read_json refuses a larger object by default, and real session.uprof files are a few
# KiB; a larger file is not capture metadata and is not read into memory.
SESSION_META_LIMIT: Final = 16 * 1024 * 1024


def check_session_meta_size(path: os.PathLike[str] | str) -> None:
    """Refuse a session.uprof above SESSION_META_LIMIT before it is read; a missing or
    unreadable file is left to the reader."""
    try:
        size = os.stat(path).st_size
    except OSError:
        return
    if size > SESSION_META_LIMIT:
        raise UprofError(
            f"{path} is {size} bytes, above the {SESSION_META_LIMIT}-byte limit "
            "(real files are a few KiB); it is not uProf capture metadata: copy the "
            "session again from the machine that captured it"
        )


def read_text(path: os.PathLike[str] | str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.read()
    except OSError as exc:
        raise UprofError(f"cannot read {path}: {exc}") from exc


def _is_link(info: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point such as a junction."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _refuse_links(source: Path) -> None:
    """Fail on the first link in what copy_session copies, before anything is copied.

    Copying follows links, so a link in a capture would copy files from outside it, a whole
    disk, or the capture into itself.
    """
    roots = [source / "session.uprof", *(source / name for name in COPY_DIRS)]
    for root in roots:
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UprofError(
                f"cannot inspect {root}: {exc}; fix its permissions and re-run"
            ) from exc
        pending = [root] if stat.S_ISDIR(info.st_mode) and not _is_link(info) else []
        if _is_link(info):
            _link_error(root)
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                raise UprofError(
                    f"cannot list {directory}: {exc}; fix its permissions and re-run"
                ) from exc
            for entry in entries:
                if _is_link(entry.stat(follow_symlinks=False)):
                    _link_error(Path(entry.path))
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _link_error(link: Path) -> None:
    raise UprofError(
        f"the capture contains a link: {link}; copying would follow it out of the "
        "capture. Replace the link with the files it points to, or remove it, and "
        "re-run"
    )


def copy_session(src: os.PathLike[str] | str, dst: os.PathLike[str] | str) -> None:
    """Copy session.uprof and the raw directories translate reads into dst.

    Existing files in dst are overwritten; callers that need a clean directory create one.
    Filesystem failures are reported with the path and a remedy, never as a traceback.
    """
    source = Path(src)
    target = Path(dst)
    _refuse_links(source)
    if target.exists() and not target.is_dir():
        raise UprofError(
            f"output path exists and is not a directory: {target}; choose another output directory"
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UprofError(
            f"cannot create output directory {target}: {exc}; choose another "
            "output directory or fix its permissions"
        ) from exc
    try:
        shutil.copy2(source / "session.uprof", target / "session.uprof")
    except OSError as exc:
        raise UprofError(
            f"cannot copy {source / 'session.uprof'} to "
            f"{target / 'session.uprof'}: {exc}; choose another output directory "
            "or fix its permissions"
        ) from exc
    for name in COPY_DIRS:
        directory = source / name
        if directory.is_dir():
            try:
                shutil.copytree(directory, target / name, dirs_exist_ok=True)
            except OSError as exc:
                raise UprofError(
                    f"cannot copy {directory} to {target / name}: {exc}; "
                    "choose another output directory or fix its permissions"
                ) from exc


def require_dir(path: os.PathLike[str] | str, what: str) -> Path:
    full = absolute(path)
    if not full.is_dir():
        raise UprofError(
            f"{what} not found: {full} (pass the directory that holds session.uprof and cpu.db)"
        )
    return full


def require_file(path: os.PathLike[str] | str, what: str, hint: str = "") -> Path:
    full = absolute(path)
    if not full.is_file():
        suffix = f"; {hint}" if hint else ""
        raise UprofError(f"{what} not found: {full}{suffix}")
    return full
