"""Text that goes into printed, runnable commands.

A printed command is pasted into Git Bash, PowerShell 5.1 or cmd.exe. A string the caller did not
type (a path or name from the capture, the disk or this install) enters such a command only when
every character is on PLAIN: ASCII letters and digits plus characters that are inert inside a
double-quoted argument in all three shells. PLAIN is an allow-list because each shell adds its
own live characters ($ and backticks in bash and PowerShell, % and ! in cmd.exe, PowerShell's
typographic quotes). A string that is not plain is replaced by a <PLACEHOLDER> and printed as
data on its own line.
"""

from __future__ import annotations

import os
import string
from typing import Final

# Inert inside "..." in Git Bash, PowerShell 5.1 and cmd.exe; each one is exercised through the
# real shells by the test suite. No other character, and no non-ASCII character, is on the list.
PLAIN: Final = frozenset(string.ascii_letters + string.digits + " ._-+,=@#~()\\/:'&;[]{}")
# Characters that need no quotes at all in any of the three shells (a backslash and a comma do:
# bash reads an unquoted backslash as an escape, PowerShell a bare comma as an array).
_BARE: Final = frozenset(string.ascii_letters + string.digits + "._-/:+=")


def is_plain(text: str) -> bool:
    """True when text may go into a printed command.

    Besides the character set: a leading - or @ would be read as an option or a response file,
    a trailing backslash would escape the closing quote for the Windows argument parser, and
    bash collapses two backslashes inside "..." into one.
    """
    return (bool(text) and all(char in PLAIN for char in text)
            and text[0] not in "-@" and not text.endswith("\\") and "\\\\" not in text)


def quote(text: str) -> str:
    """A plain string as one argument: bare when no shell needs quotes, else in "...".

    Only plain text has an inert quoting; callers check is_plain first.
    """
    if not is_plain(text):
        raise ValueError(f"not plain, cannot be quoted for every shell: {text!r}")
    return text if all(char in _BARE for char in text) else f'"{text}"'


def path_arg(path: str | os.PathLike[str], placeholder: str) -> tuple[str, str | None]:
    """(argument, data line): the quoted path when plain, else the placeholder and one line
    NAME=<escaped path> that names the value as data, never inside a command."""
    text = os.fspath(path)
    if is_plain(text):
        return quote(text), None
    return placeholder, f"{placeholder.strip('<>')}={text!r}"


def image_pattern(name: str) -> str:
    """An ILIKE pattern that matches name and is plain: every character off the list, and a
    leading - or @, becomes the one-character wildcard _."""
    pattern = "".join(char if char in PLAIN and char != "\\" else "_" for char in name)
    if pattern and pattern[0] in "-@":
        pattern = "_" + pattern[1:]
    return pattern or "_"
