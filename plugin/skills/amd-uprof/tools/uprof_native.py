"""uProf tool discovery and translation.

Install paths are probed per OS on the absolute PATH entries (never the current directory) plus a
few conventional directories; nothing is installed here. The Linux branches are kept but unverified on this host.
"""

from __future__ import annotations

import csv
import ctypes
import functools
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import uprof_shell
from uprof_session import (
    ContextCadence,
    achieved_summary,
    cycle_frequency_hz,
    derive_sampler,
    read_meta,
    run_gap_limit_ns,
    system_wide,
)
from uprof_util import (
    COPY_DIRS,
    DUMP_FILENAMES,
    LinesError,
    one_line,
    UprofError,
    absolute,
    contains,
    copy_session,
    parse_time_filter,
    skill_identity,
    tail_lines,
)

IS_WINDOWS = os.name == "nt"
TRANSLATE_TIMEOUT: Final = 4 * 60 * 60
TRANSLATE_OUTPUTS: Final = ("cpu.db", "callstack.db", *DUMP_FILENAMES.values())
# Oldest releases the tools work with: uProf 5.3 writes cpu.db as DuckDB by default (older
# releases write SQLite), and LLVM 23 added llvm-symbolizer --pdb and llvm-objdump
# --no-symbolize-operands. A tool below its minimum stops the command that needs it.
UPROF_MINIMUM: Final = (5, 3)
LLVM_MINIMUM: Final = (23,)


# ---------------------------------------------------------------- tool discovery


def _first_existing(candidates: Iterable[str | os.PathLike[str] | None]) -> Path | None:
    for path in candidates:
        if path and Path(path).is_file():
            return absolute(path)
    return None


def _which(name: str) -> Path | None:
    """name on PATH, never from the current directory.

    collect runs elevated, and shutil.which (like CreateProcess with a bare name) also looks in
    the current directory and in '.', empty or relative PATH entries, so a planted executable
    in a project or capture folder would run as administrator. Only absolute PATH entries other
    than the current directory are searched.
    """
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry) if entry else None
        if directory is None or not directory.is_absolute():
            continue
        try:
            if cwd is not None and directory.resolve() == cwd:
                continue
        except OSError:
            continue
        for candidate in _executable_names(name):
            path = directory / candidate
            if path.is_file() and (IS_WINDOWS or os.access(path, os.X_OK)):
                return path
    return None


def _executable_names(name: str) -> list[str]:
    """name with each PATHEXT extension on Windows (unless it has one), else name itself."""
    if not IS_WINDOWS or Path(name).suffix:
        return [name]
    extensions = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [name + extension.lower() for extension in extensions.split(";") if extension]


def _system_directory() -> Path:
    """The Windows system directory as the OS reports it, not from SystemRoot or windir, which
    the caller's environment can point anywhere."""
    buffer = ctypes.create_unicode_buffer(32768)
    try:
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    except (AttributeError, OSError) as exc:
        raise UprofError(f"cannot read the Windows system directory: {exc}") from exc
    if not 0 < length < len(buffer):
        raise UprofError("cannot read the Windows system directory (GetSystemDirectoryW failed)")
    return Path(buffer.value)


def _version_key(path: Path) -> tuple[int, ...]:
    """Numeric version from an install path, so llvm-19 sorts above llvm-9."""
    match = re.search(r"(?:llvm-|AMDuProf_|uProf_?)(\d+(?:\.\d+)*)", str(path))
    if not match:
        match = re.search(r"-(\d+(?:\.\d+)*)$", path.name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _newest(paths: Iterable[Path]) -> list[Path]:
    return sorted(paths, key=_version_key, reverse=True)


def find_uprof() -> Path | None:
    env = os.environ.get("AMDUPROF_CLI")
    candidates: list[str | os.PathLike[str] | None] = [env, _which("AMDuProfCLI")]
    if IS_WINDOWS:
        for base in (
            os.environ.get("ProgramW6432"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
        ):
            if base:
                candidates.append(Path(base) / "AMD" / "AMDuProf" / "bin" / "AMDuProfCLI.exe")
    else:
        candidates.extend(_newest(Path("/opt").glob("AMDuProf_*/bin/AMDuProfCLI")))
        candidates.extend(_newest(Path("/opt").glob("AMD/uProf*/bin/AMDuProfCLI")))
        candidates.append(Path("/usr/local/bin/AMDuProfCLI"))
    return _first_existing(candidates)


def find_symbolizer() -> Path | None:
    env = os.environ.get("LLVM_SYMBOLIZER")
    candidates: list[str | os.PathLike[str] | None] = [env, _which("llvm-symbolizer")]
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles")):
            if base:
                candidates.append(Path(base) / "LLVM" / "bin" / "llvm-symbolizer.exe")
    else:
        candidates.extend(_newest(Path("/usr/lib").glob("llvm-*/bin/llvm-symbolizer")))
        candidates.extend(_newest(Path("/usr/bin").glob("llvm-symbolizer-*")))
        candidates.append(Path("/usr/bin/llvm-symbolizer"))
    return _first_existing(candidates)


def find_sibling(symbolizer: Path | None, name: str) -> Path | None:
    """llvm-pdbutil, llvm-readobj and llvm-objdump usually sit next to llvm-symbolizer."""
    candidates: list[str | os.PathLike[str] | None] = [_which(name)]
    if symbolizer:
        candidates.insert(0, symbolizer.parent / (name + (".exe" if IS_WINDOWS else "")))
    return _first_existing(candidates)


# apt.llvm.org's installer for one LLVM release; distribution packages are often older.
LINUX_LLVM_INSTALL: Final = "wget https://apt.llvm.org/llvm.sh && sudo bash llvm.sh 23"


def require_uprof() -> Path:
    found = find_uprof()
    if not found:
        hint = (
            "install AMD uProf, or set AMDUPROF_CLI to AMDuProfCLI.exe"
            if IS_WINDOWS
            else "install AMD uProf, or set AMDUPROF_CLI to the AMDuProfCLI binary"
        )
        raise UprofError(f"AMDuProfCLI not found; {hint}")
    # Only --version is evidence: an install path naming a release is not the binary's version.
    cause = uprof_version_cause(found, _first_version_line(found, "--version"))
    if cause:
        raise UprofError(cause)
    return found


def failure(
    cause: str, labels: Iterable[str], *, session: str | os.PathLike[str] | None = None
) -> UprofError:
    """An unsupported case: the cause, then one runnable ALTERNATIVE line per applicable option.

    The tool never switches capture or analysis mode on its own, so the caller chooses. Lines are
    sorted by label without a default; each chain is complete, fills in only the resolved
    AMDuProfCLI, this skill's uprof.py and the failing session, and keeps the rest as
    <PLACEHOLDER>, so an option is the same text wherever it is offered.
    """
    found = find_uprof()
    cli, cli_data = (
        uprof_shell.path_arg(found, "<AMDUPROFCLI>") if found else ("<AMDUPROFCLI>", None)
    )
    script, script_data = uprof_shell.path_arg(
        Path(__file__).resolve().parent / "uprof.py", "<UPROF_PY>"
    )
    tool = "python " + script
    known, known_data = (
        uprof_shell.path_arg(absolute(session), "<SESSION_DIR>")
        if session
        else ("<SESSION_DIR>", None)
    )
    then = (
        f"; {tool} translate --session <NEW_OUT_DIR> --out-dir <NEW_TR_DIR>; "
        f"{tool} profile --session <NEW_TR_DIR> --by process"
    )
    chains = {
        "timer capture": (
            f"{tool} collect --out-dir <NEW_OUT_DIR> --seconds <S> --event timer" + then
        ),
        "timer capture (launch)": (
            f"{tool} collect --out-dir <NEW_OUT_DIR> --seconds <S> "
            "--event timer --launch <APP>" + then
        ),
        "timer window": (
            f"{tool} translate --session {known} --out-dir <NEW_TR_DIR> "
            f"--time-filter <T1>:<T2>; {tool} profile --session <NEW_TR_DIR> "
            "--by process"
        ),
        "translate without dump": (
            f"{tool} translate --session {known} --out-dir <NEW_TR_DIR>; "
            f"{tool} profile --session <NEW_TR_DIR> --by process"
        ),
        "whole capture": f"{tool} profile --session {known} --by process",
        "Zen host": (
            f"{tool} collect --out-dir <NEW_OUT_DIR> --seconds <S> --process <PID> "
            "--event <ibs-op|cycles>" + then
        ),
    }
    lines = [cause] + [
        f"ALTERNATIVE: {label}: {chains[label]}" for label in sorted(set(labels), key=str.lower)
    ]
    # A path that is not plain is a placeholder in the commands and named here as data only.
    lines += [data for data in (cli_data, script_data, known_data) if data]
    return LinesError(lines)


def require_symbolizer() -> Path:
    """The llvm-symbolizer; it and the found siblings symbolization uses must meet LLVM_MINIMUM."""
    found = find_symbolizer()
    if not found:
        hint = "winget install --id LLVM.LLVM" if IS_WINDOWS else LINUX_LLVM_INSTALL
        raise UprofError(f"llvm-symbolizer not found; {hint} (or set LLVM_SYMBOLIZER)")
    require_llvm(SYMBOLIZE_TOOLS, found)
    return found


# Symbolization derives function ranges with llvm-readobj on every OS; llvm-pdbutil reads the
# PDB fold cache on Windows only.
SYMBOLIZE_TOOLS: Final = ("llvm-symbolizer", "llvm-readobj") + (
    ("llvm-pdbutil",) if IS_WINDOWS else ()
)
# Instruction context symbolizes, reads imports with llvm-readobj and decodes with llvm-objdump.
CONTEXT_TOOLS: Final = ("llvm-symbolizer", "llvm-readobj", "llvm-objdump")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """The dotted version after the word "version", else the first dotted number in text."""
    if not text:
        return None
    match = re.search(r"(?i)\bversion[:\s-]*v?(\d+(?:\.\d+)*)", text) or re.search(
        r"(\d+(?:\.\d+)+)", text
    )
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _minimum_text(minimum: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in minimum)


def _version_cause(
    path: Path, version: str | None, minimum: tuple[int, ...], tool: str, why: str, remedy: str
) -> str | None:
    """None when version meets minimum, else one line with the cause and the alternatives."""
    found = parse_version(version)
    if found is not None and found >= minimum:
        return None
    floor = _minimum_text(minimum)
    if found is None:
        problem = f"cannot read the version of {path} (--version printed none)"
    else:
        problem = f"{path} is {tool} {_minimum_text(found)}, below the minimum {floor}"
    return f"{problem}; {tool} {floor} or later is required: {why}; remedy: {remedy}, then re-run"


def uprof_version_cause(path: Path, version: str | None) -> str | None:
    floor = _minimum_text(UPROF_MINIMUM)
    return _version_cause(
        path,
        version,
        UPROF_MINIMUM,
        "AMD uProf",
        "older releases do not write cpu.db as DuckDB by default, which the analysis reads",
        f"install AMD uProf {floor} or later from https://www.amd.com/en/developer/uprof.html, "
        f"or set AMDUPROF_CLI to an AMDuProfCLI of uProf {floor} or later",
    )


def llvm_version_cause(path: Path, version: str | None) -> str | None:
    floor = _minimum_text(LLVM_MINIMUM)
    install = "winget upgrade --id LLVM.LLVM" if IS_WINDOWS else LINUX_LLVM_INSTALL
    return _version_cause(
        path,
        version,
        LLVM_MINIMUM,
        "LLVM",
        "llvm-symbolizer --pdb and llvm-objdump --no-symbolize-operands need it",
        f"{install}, or set LLVM_SYMBOLIZER to an llvm-symbolizer of LLVM {floor} or later "
        "whose directory holds the other LLVM tools",
    )


class SymbolizerUnsupported(UprofError):
    """An LLVM tool is below LLVM_MINIMUM; the message is the cause and the remedy."""


@functools.lru_cache(maxsize=None)
def llvm_cause(path: str) -> str | None:
    """None when the LLVM tool at path meets LLVM_MINIMUM, else its one-line cause.

    One --version run per tool path and process: every module of a profile asks again.
    """
    return llvm_version_cause(Path(path), _first_version_line(Path(path), "--version"))


def _llvm_path(name: str, symbolizer: Path | None) -> Path | None:
    return symbolizer if name == "llvm-symbolizer" else find_sibling(symbolizer, name)


def require_llvm(names: Iterable[str], symbolizer: Path | None = None) -> None:
    """Stop when a found LLVM tool among names is below LLVM_MINIMUM; absent tools pass here,
    because each caller reports absence with its own cause."""
    symbolizer = symbolizer if symbolizer else find_symbolizer()
    for name in names:
        path = _llvm_path(name, symbolizer)
        cause = llvm_cause(str(path)) if path else None
        if cause:
            raise SymbolizerUnsupported(cause)


def tool_operand(path: str | os.PathLike[str]) -> str:
    """A file path as a positional tool argument.

    LLVM tools parse an argument starting with '-' as an option and one starting with '@' as a
    response file. An absolute path starts with neither, so a capture-supplied path is always
    read as the file it names.
    """
    return os.path.abspath(os.fspath(path))


class ToolTimeout(UprofError):
    """An external tool did not finish within its timeout."""


def run(
    command: Sequence[str],
    timeout: int | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a native tool with UTF-8 text output and no console-window flash."""
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if input_text is not None:
        kwargs["input"] = input_text
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(command, **kwargs)
    except FileNotFoundError as exc:
        raise UprofError(f"cannot run {command[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeout(f"{Path(command[0]).name} timed out after {timeout}s") from exc


def print_output(completed: subprocess.CompletedProcess[str]) -> None:
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.stderr:
        print(
            completed.stderr, end="" if completed.stderr.endswith("\n") else "\n", file=sys.stderr
        )


def failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    return tail_lines((completed.stdout or "") + "\n" + (completed.stderr or ""))


# Windows NTSTATUS of an access violation, as a signed and an unsigned process exit code.
ACCESS_VIOLATION_EXITS: Final = (-1073741819, 0xC0000005)
# The CLI log line AMDuProfCLI writes when a session holds no sample category it can translate.
NO_CATEGORY_LOG: Final = "No categories to translate"
CLI_LOG_TAIL: Final = 64 * 1024


class CliLog:
    """What AMDuProfCLI appends to its <temp>/<user>-AMDuProfCLI.log during one run.

    The CLI names some failures only in that log, so the sizes are taken before the run and
    only the bytes added afterwards count; an earlier run's lines are never blamed.
    """

    def __init__(self) -> None:
        self.sizes: dict[Path, int] = {}
        for path in self._logs():
            try:
                self.sizes[path] = path.stat().st_size
            except OSError:
                pass

    @staticmethod
    def _logs() -> list[Path]:
        roots = dict.fromkeys(
            Path(root)
            for root in (os.environ.get("TMP"), os.environ.get("TEMP"), tempfile.gettempdir())
            if root
        )
        return sorted({path for root in roots for path in root.glob("*-AMDuProfCLI.log")})

    def appended(self) -> tuple[Path | None, str]:
        """The log that grew during the run and the text it gained (bounded to its tail)."""
        for path in self._logs():
            try:
                size = path.stat().st_size
                start = self.sizes.get(path, 0)
                if size <= start:
                    continue
                with open(path, "rb") as handle:
                    handle.seek(max(start, size - CLI_LOG_TAIL))
                    return path, handle.read().decode("utf-8", errors="replace")
            except OSError:
                continue
        return None, ""


def recapture_commands(session: Path) -> tuple[str, str, str | None]:
    """Cycle and IBS collect commands writing next to session, for a capture that holds nothing
    to translate, and the data lines for any path that had to become a placeholder."""
    script, script_data = uprof_shell.path_arg(
        Path(__file__).resolve().with_name("uprof.py"), "<UPROF_PY>"
    )
    out_dir, out_data = uprof_shell.path_arg(f"{session}-recapture", "<NEW_OUT_DIR>")
    cycles = f"python {script} collect --out-dir {out_dir} --seconds 60"
    data = "\n".join(line for line in (script_data, out_data) if line) or None
    return cycles, cycles + " --event ibs-op", data


def translate_failure(
    what: str, completed: subprocess.CompletedProcess[str], log: CliLog, session: Path
) -> LinesError:
    """The error for a failed AMDuProfCLI translate, naming the cause the CLI log gives: one
    line, plus a data line for each path a remedy holds as a placeholder."""
    return LinesError(_translate_failure_lines(what, completed, log, session))


def _translate_failure_lines(
    what: str, completed: subprocess.CompletedProcess[str], log: CliLog, session: Path
) -> list[str]:
    detail = failure_detail(completed)
    code = completed.returncode
    crash = (
        " (AMDuProfCLI crashed: access violation (0xC0000005))"
        if code in ACCESS_VIOLATION_EXITS
        else ""
    )
    log_path, log_text = log.appended()
    if NO_CATEGORY_LOG in log_text:
        cycles, ibs, data = recapture_commands(session)
        return [
            f"{what} failed with {code}{crash}: the session has no translatable sample "
            "category (per the AMDuProfCLI log), so it holds no samples to translate; "
            f"remedy: capture again with {cycles} (cycles) or {ibs} (IBS), then translate "
            "that session",
            *(data.split("\n") if data else []),
        ]
    if crash and log_path:
        return [f"{what} failed with {code}{crash}: {detail}; AMDuProfCLI log: {log_path}"]
    return [f"{what} failed with {code}{crash}: {detail}"]


def symbol_cache_dir() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Symbols"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "amd-uprof" / "symbols"


def default_symbol_servers() -> list[str]:
    return ["https://msdl.microsoft.com/download/symbols"] if IS_WINDOWS else []


@dataclass(frozen=True)
class NtSymbolPath:
    """The parts of a Windows-style _NT_SYMBOL_PATH, each element stripped of padding.

    caches are local symbol-store roots: the downstream stores of srv*/symsrv* entries (every
    element before the last) and cache* directories. upstreams are the last element of each
    srv*/symsrv* entry, a URL or a share that is never read, since reading it would contact the
    store. dirs are plain directory entries. servers are the srv* entries that name a downstream store slot and an upstream
    store, rejoined from their stripped elements; uProf translate receives them as symbol
    servers and download_cache, the first server's local downstream store, as its symbol cache.
    """

    caches: tuple[str, ...] = ()
    upstreams: tuple[str, ...] = ()
    dirs: tuple[str, ...] = ()
    servers: tuple[str, ...] = ()
    download_cache: str | None = None


def _is_url(text: str) -> bool:
    return re.match(r"[a-z][a-z0-9+.-]*://", text, re.IGNORECASE) is not None


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def parse_nt_symbol_path(value: str | None) -> NtSymbolPath:
    """Read _NT_SYMBOL_PATH once, the same way for translate, discovery and the cache key."""
    caches: list[str] = []
    upstreams: list[str] = []
    dirs: list[str] = []
    servers: list[str] = []
    download_cache = None
    for entry in (value or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = [part.strip() for part in entry.split("*")]
        kind = parts[0].lower()
        if kind in ("srv", "symsrv"):
            # symsrv*<dll>*<stores>: the second element names the DLL, not a store.
            slots = parts[2 if kind == "symsrv" else 1 :]
            caches += [store for store in slots[:-1] if store and not _is_url(store)]
            upstreams += [store for store in slots[-1:] if store]
            if kind == "srv" and len(slots) >= 2:
                servers.append("*".join(parts))
                if download_cache is None and slots[0] and not _is_url(slots[0]):
                    download_cache = slots[0]
        elif kind == "cache":
            caches += [part for part in parts[1:] if part]
        elif len(parts) == 1 and not _is_url(entry):
            dirs.append(entry)
    return NtSymbolPath(
        _unique(caches), _unique(upstreams), _unique(dirs), tuple(servers), download_cache
    )


def parse_symbol_path(value: str | None) -> tuple[str | None, list[str]]:
    """Return (download cache, symbol servers) from a Windows-style _NT_SYMBOL_PATH."""
    parsed = parse_nt_symbol_path(value)
    return parsed.download_cache, list(parsed.servers)


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


# Numeric VBS and hypervisor state from CIM: both values print as integers in every display
# language, unlike the systeminfo text. A missing DeviceGuard class reads as 0.
_VIRTUALIZATION_QUERY: Final = (
    "$d = Get-CimInstance -Namespace root/Microsoft/Windows/DeviceGuard "
    "-ClassName Win32_DeviceGuard -ErrorAction SilentlyContinue; "
    "$c = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop; "
    "'{0} {1}' -f [int]$d.VirtualizationBasedSecurityStatus, [int][bool]$c.HypervisorPresent"
)
_WINDOWS_IBS_REMEDY: Final = (
    "remedy: turn off Hyper-V and VBS (as administrator: bcdedit /set hypervisorlaunchtype off, "
    "then disable Memory integrity and reboot)"
)


def ibs_unavailable_reason() -> str:
    """Explain why IBS counters are missing, naming VBS or a hypervisor when that is the cause."""
    if IS_WINDOWS:
        try:
            # By absolute path: a bare name is also looked up in the current directory.
            powershell = _system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            completed = run(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _VIRTUALIZATION_QUERY,
                ],
                timeout=60,
            )
            match = (
                re.fullmatch(r"\s*(\d+)\s+(\d+)\s*", completed.stdout or "")
                if completed.returncode == 0
                else None
            )
        except UprofError:
            match = None
        if match is None:
            return (
                "no IBS counters are available, and the VBS/hypervisor state could not be "
                "read (Win32_DeviceGuard, Win32_ComputerSystem); " + _WINDOWS_IBS_REMEDY
            )
        vbs_status, hypervisor = int(match.group(1)), int(match.group(2))
        if vbs_status == 2:
            return (
                "VBS is running (Win32_DeviceGuard VirtualizationBasedSecurityStatus 2), so "
                "IBS counters are not exposed; " + _WINDOWS_IBS_REMEDY
            )
        if hypervisor:
            return (
                "a hypervisor is present (Win32_ComputerSystem HypervisorPresent), so IBS "
                "counters are not exposed; " + _WINDOWS_IBS_REMEDY
            )
        return (
            "no IBS counters are available although no hypervisor or VBS is active; check "
            "that the CPU is AMD Zen with IBS and that uProf's driver is loaded"
        )
    try:
        with open(Path("/proc/cpuinfo"), "r", encoding="utf-8", errors="replace") as handle:
            if " hypervisor" in handle.read():
                return "a hypervisor is active (/proc/cpuinfo reports the hypervisor flag)"
    except OSError:
        pass
    if any(Path("/sys/module").glob("kvm*")):
        return (
            "no IBS counters are available; the KVM kernel module is loaded, which may "
            "hide them, or check the kernel and perf permissions"
        )
    return "no IBS counters are available; check the kernel, virtualization and perf permissions"


def _first_version_line(path: Path, *args: str) -> str | None:
    """The first line of `<tool> --version` that names a version, else its first line."""
    try:
        completed = run([str(path), *args], timeout=30)
    except (UprofError, OSError):
        return None
    if completed.returncode != 0:
        return None
    lines = [
        line.strip()
        for line in ((completed.stdout or "") + "\n" + (completed.stderr or "")).splitlines()
        if line.strip()
    ]
    if not lines:
        return None
    return next((line for line in lines if "version" in line.lower()), lines[0])


def _tool_row(
    name: str,
    path: Path | None,
    version: str | None,
    missing: str = "not found",
    minimum: tuple[int, ...] | None = None,
    too_old: str | None = None,
    display: str | None = None,
) -> str:
    """One `tools` row; missing is the cause (and remedy) printed when path is absent, too_old
    the cause when version is below minimum or unreadable, and display replaces the printed
    version text without changing the verdict."""
    if not path:
        return f"{name}=; version=MISSING ({missing})"
    row = f"{name}={path}; version={display or version or 'unavailable'}"
    if minimum is not None:
        status = "TOO OLD" if parse_version(version) else "UNREADABLE"
        row += f"; minimum={_minimum_text(minimum)}; " + (
            f"{status}: {too_old}" if too_old else "ok"
        )
    return row


def _uprof_version(path: Path | None, version: str | None) -> str | None:
    """The --version line for display, else the install-path version marked as such."""
    if path is None:
        return None
    if version:
        return version
    path_version = ".".join(str(part) for part in _version_key(path))
    if path_version:
        return f"{path_version} (from the install path; --version gave nothing)"
    return "unavailable (--version failed and the install path carries no version)"


def tool_report() -> None:
    """Print every tool path and version against its minimum, then fail once if a required tool
    is missing or any found tool, optional ones included, is below its minimum or unreadable:
    a command that runs it would stop, while an absent optional tool only omits its block."""
    uprof = find_uprof()
    symbolizer = find_symbolizer()
    pdbutil = find_sibling(symbolizer, "llvm-pdbutil")
    readobj = find_sibling(symbolizer, "llvm-readobj")
    objdump = find_sibling(symbolizer, "llvm-objdump")
    try:
        import duckdb

        version = duckdb.__version__
        try:
            major = int(version.split(".")[0])
        except (AttributeError, ValueError):
            major = 0
        if major < 2:
            duckdb_version = None
            duckdb_short = None
            duckdb_hint = (
                f"DuckDB {version} is too old (need 2.x); upgrade with: "
                f"{sys.executable} -m pip install --upgrade duckdb"
            )
        else:
            duckdb_version = f"duckdb {version} (python)"
            duckdb_short = version
            duckdb_hint = None
    except ImportError:
        duckdb_version = None
        duckdb_short = None
        duckdb_hint = f"DuckDB Python module (install with: {sys.executable} -m pip install duckdb)"
    too_old: list[str] = []

    def row(
        name: str,
        path: Path | None,
        version: str | None,
        minimum: tuple[int, ...],
        cause_of,
        tool: str,
        missing: str = "not found",
        display: str | None = None,
    ) -> str:
        cause = cause_of(path, version) if path else None
        if cause:
            too_old.append(tool)
        return _tool_row(name, path, version, missing, minimum, cause, display)

    def llvm_row(name: str, path: Path | None, tool: str, missing: str = "not found") -> str:
        version = _first_version_line(path, "--version") if path else None
        return row(name, path, version, LLVM_MINIMUM, llvm_version_cause, tool, missing)

    uprof_line = _first_version_line(uprof, "--version") if uprof else None
    print(
        row(
            "UPROF",
            uprof,
            uprof_line,
            UPROF_MINIMUM,
            uprof_version_cause,
            "AMDuProfCLI",
            display=_uprof_version(uprof, uprof_line),
        )
    )
    print(f"DUCKDB={duckdb_version or ''}; version={duckdb_short or 'MISSING (not installed)'}")
    print(llvm_row("SYMBOLIZER", symbolizer, "llvm-symbolizer"))
    print(llvm_row("PDBUTIL", pdbutil, "llvm-pdbutil"))
    print(llvm_row("READOBJ", readobj, "llvm-readobj"))
    print(
        llvm_row(
            "OBJDUMP",
            objdump,
            "llvm-objdump",
            "llvm-objdump is not beside llvm-symbolizer or on PATH; instruction context "
            "is omitted; remedy: install LLVM or add its bin directory to PATH; "
            "module/RVA rows still work",
        )
    )
    failures = []
    if too_old:
        failures.append(
            "TOO OLD or UNREADABLE " + ", ".join(too_old) + " (each row names the remedy)"
        )
    missing = []
    if not uprof:
        missing.append("uProf CLI (install AMD uProf or set AMDUPROF_CLI)")
    if not duckdb_version:
        missing.append(duckdb_hint)
    if not symbolizer:
        missing.append("llvm-symbolizer (install LLVM or set LLVM_SYMBOLIZER)")
    if missing:
        failures.insert(0, "MISSING " + "; ".join(missing))
    if failures:
        raise UprofError("; ".join(failures))


# ---------------------------------------------------------------- translate


def translate_command(
    uprof: str | Path,
    session: str | Path,
    *,
    symbol_cache: str | Path | None = None,
    symbol_servers: Iterable[str] = (),
    symbol_paths: Iterable[str | Path] = (),
    time_filter: str | None = None,
    ascii_kind: str | None = None,
) -> list[str]:
    """The AMDuProfCLI translate argument list shared by translate and the dump cache."""
    command = [os.fspath(uprof), "translate", "-i", os.fspath(session)]
    if symbol_cache is not None:
        command += ["--symbol-cache-dir", os.fspath(symbol_cache)]
    for server in symbol_servers:
        command += ["--symbol-server", server]
    for path in symbol_paths:
        command += ["--symbol-path", os.fspath(path)]
    if time_filter:
        command += ["--time-filter", time_filter]
    if ascii_kind:
        command += ["--ascii", ascii_kind]
    return command


def _dump_rows(reader: Iterable[list[str]], path: Path) -> Iterator[list[str]]:
    """The dump's rows; a cell above csv's field limit is no uProf dump cell and fails with its
    cause."""
    try:
        yield from reader
    except csv.Error as exc:
        raise UprofError(
            f"{path} is not a uProf ASCII dump ({exc}); the capture is damaged "
            "or not uProf output, so capture again: collect --out-dir "
            "<NEW_OUT_DIR> --seconds <S>"
        ) from exc


def dump_cadence(
    path: Path, context_key: str | tuple[str, ...] = "CPU_Number", gap_limit_ns: float | None = None
) -> tuple[int, int, list[ContextCadence]]:
    """Sample rows, distinct CPU_Number values and per-context cadence in an ASCII dump.

    context_key is CPU_Number for a system-wide capture and (PID, TID) for an attached one, so
    same-TID threads of different processes stay separate; if any named column is missing the
    context falls back to CPU_Number. Per-context DeltaTicks are held to take an exact median, so
    memory grows with the dump. gap_limit_ns drops gaps above it, which are time the context was
    not sampling continuously; None keeps every gap.
    """
    samples = 0
    cpus: set[str] = set()
    ticks: dict[tuple[str, ...], list[int]] = {}
    keys = (context_key,) if isinstance(context_key, str) else context_key
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = _dump_rows(csv.reader(handle), path)
        header = next(reader, None)
        if header is None:
            return 0, 0, []
        try:
            cpu_index = header.index("CPU_Number")
            delta_index = header.index("DeltaTick")
        except ValueError:
            return 0, 0, []
        key_indices = (
            [header.index(key) for key in keys]
            if all(key in header for key in keys)
            else [cpu_index]
        )
        for row in reader:
            if not row:
                continue
            samples += 1
            if len(row) > cpu_index:
                cpus.add(row[cpu_index])
            try:
                tick = int(row[delta_index])
            except (IndexError, ValueError):
                continue
            key = tuple(row[index] if len(row) > index else "?" for index in key_indices)
            ticks.setdefault(key, []).append(tick)
    contexts: list[ContextCadence] = []
    for values in ticks.values():
        values.sort()
        gaps = [later - earlier for earlier, later in zip(values, values[1:])]
        if gap_limit_ns is not None:
            gaps = [gap for gap in gaps if gap <= gap_limit_ns]
        contexts.append(
            ContextCadence(
                samples=len(values), median_gap_ns=statistics.median(gaps) if gaps else None
            )
        )
    return samples, len(cpus), contexts


def _dump_window_s(session: Path, time_filter: str | None) -> float | None:
    """The dump's window: the translate window when given, else the recorded capture elapsed."""
    if time_filter:
        bounds = parse_time_filter(time_filter)
        if bounds is not None:
            return float(bounds[1] - bounds[0])
    meta = read_meta(session) or {}
    elapsed_ms = ((meta.get("collection") or {}).get("schedule") or {}).get("elapsed")
    try:
        return int(elapsed_ms) / 1000.0
    except (TypeError, ValueError):
        return None


def _shown(path: Path) -> str:
    """path, followed by where it resolves when that differs from how it was given."""
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        return os.fspath(path)
    if os.path.normcase(resolved) == os.path.normcase(os.fspath(path)):
        return os.fspath(path)
    return f"{path} (resolves to {resolved})"


def _refuse_overlap(out: Path, session: Path) -> None:
    """Refuse an out-dir that is the session or nests with it, however either is spelled.

    Run it again directly before anything in out is removed or copied, so a path swapped for a
    link after the first check is still caught.
    """
    if contains(out, session) or contains(session, out):
        raise UprofError(
            f"--out-dir {_shown(out)} overlaps --session {_shown(session)} (the "
            "same directory under another name, or one inside the other): "
            "translate rewrites cpu.db, callstack.db and session.uprof, so it works "
            "on a copy. Choose an --out-dir outside the session"
        )


def _clear_translate_outputs(out: Path) -> None:
    """Remove what an earlier translate copied and generated in out.

    The copied raw session directories are inputs to the next translation, so stale ones from a
    different session would be translated alongside the new copy.
    """
    for name in (*COPY_DIRS, "session.uprof", *TRANSLATE_OUTPUTS):
        path = out / name
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError as exc:
            raise UprofError(
                f"cannot remove {path} for --force: {exc}; choose another "
                "--out-dir or fix its permissions"
            ) from exc


def symbolization_counts(cpu_db: Path | str) -> tuple[int, int | None, int] | None:
    """Modules, modules recording debug info and Function rows in a translated cpu.db.

    AMDuProfCLI translate exits 0 and prints a generic "Translation finished" even when no
    module got symbols, so these counts are the only evidence that translation resolved any.
    Returns None when the database cannot be inspected; the caller reports that, not success.
    """
    import uprof_db

    try:
        connection = uprof_db.connect(cpu_db)
    except Exception:
        return None
    try:
        columns = {
            str(name).lower()
            for (name,) in connection.execute(
                "SELECT lower(column_name) FROM duckdb_columns() WHERE lower(table_name) = 'module'"
            ).fetchall()
        }
        modules = connection.execute("SELECT count(*) FROM Module").fetchone()[0]
        with_debug = (
            connection.execute("SELECT count(*) FROM Module WHERE foundDebugInfo").fetchone()[0]
            if "founddebuginfo" in columns
            else None
        )
        functions = connection.execute("SELECT count(*) FROM Function").fetchone()[0]
        return int(modules), (None if with_debug is None else int(with_debug)), int(functions)
    except Exception:
        return None
    finally:
        connection.close()


def translate(
    session_dir: Path | str,
    out_dir: Path | str,
    symbol_paths: Iterable[str | Path] | None = None,
    symbol_cache: Path | str | None = None,
    symbol_servers: list[str] | None = None,
    time_filter: str | None = None,
    ascii_kind: str | None = None,
    force: bool = False,
) -> None:
    session = absolute(session_dir)
    if not session.is_dir():
        raise UprofError(f"session directory not found: {session}")
    if not (session / "session.uprof").is_file():
        raise UprofError(f"session.uprof not found in {session}")
    out = absolute(out_dir)
    if out.exists() and not out.is_dir():
        raise UprofError(
            f"output path exists and is not a directory: {out}; choose another --out-dir"
        )
    _refuse_overlap(out, session)
    # Copied inputs count too: a translate that stopped after the copy leaves them, and copying
    # another capture over them would merge both captures' raw files into one translation.
    conflicts = [
        out / name
        for name in (*TRANSLATE_OUTPUTS, "session.uprof", *COPY_DIRS)
        if os.path.lexists(out / name)
    ]
    if conflicts and not force:
        shown = ", ".join(os.fspath(path) for path in conflicts)
        raise UprofError(
            f"--out-dir already holds a previous translation or its copied inputs ({shown}); "
            "translate never appends to it or merges into it. Choose another --out-dir, or pass "
            "--force to replace the files translate copied and generated there"
        )
    if time_filter and parse_time_filter(time_filter) is None:
        raise UprofError("--time-filter needs T1:T2 in seconds from capture start, e.g. 0:5")
    if ascii_kind and ascii_kind not in DUMP_FILENAMES:
        raise UprofError(
            f"unknown --ascii kind {ascii_kind!r}; use ibsop-event-dump or ibsfetch-event-dump"
        )
    if ascii_kind and derive_sampler(session).timer:
        raise failure(
            "a timer capture has no IBS sample dump", ["translate without dump"], session=session
        )

    env_cache, env_servers = parse_symbol_path(os.environ.get("_NT_SYMBOL_PATH"))
    cache = symbol_cache or env_cache or symbol_cache_dir()
    servers = list(
        dict.fromkeys([*(env_servers or default_symbol_servers()), *(symbol_servers or [])])
    )
    print(f"VERSION={skill_identity()}")
    print(f"SYMBOL_CACHE={cache}")
    print(f"SYMBOL_SERVERS={' | '.join(servers)}")
    for server in servers:
        upstream = server.split("*")[-1].strip()
        if upstream.lower().startswith("http://"):
            # Downloaded PDBs are parsed; over plain http anyone on the path can replace them.
            print(
                f"WARNING: symbol server {upstream} uses plain http, so a network attacker "
                "can substitute the PDBs translate downloads; use its https:// URL",
                file=sys.stderr,
            )

    uprof = require_uprof()
    _refuse_overlap(out, session)
    if force:
        _clear_translate_outputs(out)
    copy_session(session, out)

    command = translate_command(
        uprof,
        out,
        symbol_cache=cache,
        symbol_servers=servers,
        symbol_paths=[absolute(path) for path in symbol_paths or ()],
        time_filter=time_filter,
        ascii_kind=ascii_kind,
    )

    log = CliLog()
    completed = run(command, timeout=TRANSLATE_TIMEOUT)
    print_output(completed)
    if completed.returncode != 0:
        raise translate_failure("translate", completed, log, session)

    cpu_db = out / "cpu.db"
    if not cpu_db.is_file():
        raise UprofError(f"translate did not produce {cpu_db}")
    print(f"SESSION={out}")
    print(f"CPU_DB={cpu_db}")
    counts = symbolization_counts(cpu_db)
    if counts is None:
        print(
            "SYMBOLIZATION: could not inspect cpu.db; check Module.foundDebugInfo and Function "
            "rows before relying on names"
        )
    else:
        modules, with_debug, functions = counts
        debug_text = (
            "Module.foundDebugInfo absent" if with_debug is None else f"{with_debug}/{modules}"
        )
        print(f"SYMBOLIZATION: {debug_text} modules with debug info, {functions} Function rows")
        if functions == 0 or with_debug == 0:
            print(
                "WARNING: translate exited 0 and printed 'Translation finished', but this "
                "cpu.db has no symbol evidence; the exit status is not symbolization success. "
                "Check the SYMBOL_CACHE and SYMBOL_SERVERS paths above, or pass --symbol-cache, "
                "--symbol-server or --symbol-path and re-run translate into a new --out-dir"
            )
    if ascii_kind:
        dump = out / DUMP_FILENAMES[ascii_kind]
        if not dump.is_file():
            raise UprofError(
                f"translate --ascii {ascii_kind} did not produce "
                f"{DUMP_FILENAMES[ascii_kind]} in {out}"
            )
        print(f"ASCII_DUMP={dump}")
        sampler = derive_sampler(out)
        wide = system_wide(out)
        label = "CPU" if wide else "thread"
        frequency_hz = cycle_frequency_hz(sampler)
        samples, cpus, contexts = dump_cadence(
            dump, "CPU_Number" if wide else ("PID", "TID"), run_gap_limit_ns(sampler, frequency_hz)
        )
        window_s = _dump_window_s(out, time_filter)
        summary = achieved_summary(samples, window_s, cpus, sampler, contexts, label, frequency_hz)
        print(f"ACHIEVED: {summary}")
    else:
        print("ACHIEVED: reported at profile time (translate wrote no dump without --ascii)")
