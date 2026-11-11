"""Fixture paths, tool runs and synthetic sessions shared by the debug-format tests.

The binaries and their exact builds are documented in fixtures/debug/README.md.
"""

import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path

import uprof_native

HERE = Path(__file__).resolve().parent
DEBUG = HERE / "fixtures" / "debug"
PE = DEBUG / "pe"
ELF = DEBUG / "elf"
TOOLS = HERE.parent / "plugin" / "skills" / "amd-uprof" / "tools"

SYMBOLIZER = uprof_native.find_symbolizer()
READOBJ = uprof_native.find_sibling(SYMBOLIZER, "llvm-readobj")
OBJDUMP = uprof_native.find_sibling(SYMBOLIZER, "llvm-objdump")
PDBUTIL = uprof_native.find_sibling(SYMBOLIZER, "llvm-pdbutil")

SOURCE_HELPER_LINE = 2
SOURCE_ENTRY_LINE = 6
SOURCE_BODY_LINE = 7

# Function entries and one instruction inside fixture_entry's body for each image. The layout is
# pinned by the build commands in fixtures/debug/README.md.
PE_HELPER = 0x1000
PE_ENTRY = 0x1010
PE_ENTRY_BODY = 0x1018
ELF_HELPER = 0x1370
ELF_ENTRY = 0x1380
ELF_ENTRY_BODY = 0x138E
ELF_COMPRESSED_HELPER = 0x1310
ELF_COMPRESSED_ENTRY = 0x1320
ELF_COMPRESSED_BODY = 0x132E
ELF_SHARED_HELPER = 0x13D0
ELF_SHARED_ENTRY = 0x13E0
ELF_SHARED_BODY = 0x13EE


def tool_skip() -> str | None:
    """A skip reason naming the first missing LLVM tool, or None."""
    missing = [name for name, path in (("llvm-symbolizer", SYMBOLIZER),
                                       ("llvm-readobj", READOBJ),
                                       ("llvm-objdump", OBJDUMP),
                                       ("llvm-pdbutil", PDBUTIL)) if not path]
    return ("missing " + ", ".join(missing) + "; install LLVM") if missing else None


def run(tool, *args, input_text: str | None = None) -> subprocess.CompletedProcess:
    command = [os.fspath(tool), *[os.fspath(arg) for arg in args]]
    return subprocess.run(command, input=input_text, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def symbolizer_records(image, addresses, pdb=None, debug_dirs=(), fallback=(),
                       ) -> list[dict]:
    """Raw llvm-symbolizer JSON records, one per address, in input order."""
    command = [f"--obj={image}"]
    if pdb:
        command.append(f"--pdb={pdb}")
    command += [f"--debug-file-directory={path}" for path in debug_dirs]
    command += [f"--fallback-debug-path={path}" for path in fallback]
    command += ["--relative-address", "--functions=linkage", "--inlines",
                "--output-style=JSON"]
    text = "\n".join(f"0x{address:X}" for address in addresses) + "\n"
    completed = run(SYMBOLIZER, *command, input_text=text)
    if completed.returncode != 0:
        raise AssertionError(f"llvm-symbolizer exited {completed.returncode}: "
                             f"{completed.stderr.strip()}")
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]


def innermost(record: dict) -> dict:
    """Frame 0 of a symbolizer record, or an empty dict for an empty Symbol list."""
    frames = record.get("Symbol") or []
    return frames[0] if frames else {}


def production(exe, addresses, **kwargs):
    """Run symbolize_module into a temporary directory.

    Returns (module, jsonl records, stream text). The records are read before the temporary
    directory is removed.
    """
    import io

    import uprof_symbols

    stream = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="uprof-debug-") as root:
        rva_file = Path(root) / "rvas.txt"
        rva_file.write_text("\n".join(f"0x{address:X}" for address in addresses) + "\n",
                            encoding="utf-8")
        module = uprof_symbols.symbolize_module(str(exe), str(rva_file), root,
                                                stream=stream, **kwargs)
        jsonl = Path(root) / "symbols.jsonl"
        records = [json.loads(line) for line in
                   jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    return module, records, stream.getvalue()


_SESSION_EVENTS = {"op": {"interval": 4_700_000}, "opInterval": 4_700_000,
                   "opCycleCount": True}


def write_session(root, module_path, addresses, function_name="") -> Path:
    """A synthetic translated session with one module and one sample per address."""
    import duckdb

    session = Path(root) / "session"
    session.mkdir()
    meta = {
        "translateCommand": "AMDuProfCLI.exe translate -i session --ascii "
                            "ibsop-event-dump",
        "collection": {"schedule": {"elapsed": 10_000},
                       "events": {"ibsEvents": _SESSION_EVENTS},
                       "target": {"systemWide": True}},
        "auxiliary": {"ibsOpSampling": True},
    }
    (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
    connection = duckdb.connect(str(session / "cpu.db"))
    try:
        connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
        connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR, "
                           "isSystemModule BOOLEAN)")
        connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                           "name VARCHAR)")
        connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                           "functionId UBIGINT, moduleRVA UBIGINT)")
        connection.execute("INSERT INTO Process VALUES (100, 'workload.exe')")
        connection.execute("INSERT INTO Module VALUES (1, ?, false)", [str(module_path)])
        connection.execute("INSERT INTO Function VALUES (10, 1, ?)", [function_name])
        connection.executemany("INSERT INTO UnifiedSampleSeries VALUES (1, 10, ?)",
                               [(address,) for address in addresses])
    finally:
        connection.close()
    with open(session / "IbsOpDump.csv", "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["DeltaTick", "CPU_Number", "TID", "PID", "Offset", "ModuleName"])
        for index, address in enumerate(addresses, start=1):
            writer.writerow([index * 100_000_000, 0, 1, 100, address, str(module_path)])
    return session


def profile(session, by="line", extra=()) -> dict:
    """Run the production profile path on a synthetic session and return its JSON document."""
    import sys

    completed = subprocess.run(
        [sys.executable, str(TOOLS / "uprof.py"), "profile", "--session", str(session),
         "--by", by, "--json", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise AssertionError(f"uprof.py profile exited {completed.returncode}: "
                             f"{completed.stderr.strip()}")
    return json.loads(completed.stdout)


def relative_path_for_fallback(path) -> Path:
    """LLVM's fallback-debug-path candidate: the binary's path without its root."""
    text = str(path)
    drive, rest = os.path.splitdrive(text)
    return Path(rest.lstrip("\\/") if drive else rest.lstrip("\\/"))
