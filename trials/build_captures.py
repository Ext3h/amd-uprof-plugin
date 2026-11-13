"""Build the synthetic uProf sessions the trial scenarios in scenarios.md run on.

The sessions exercise the tools' code paths, not real capture statistics: sample counts are small
and the IBS field values are hand-picked. Every module image a session records is a copy of a
committed test fixture, and the recorded PE CheckSum and SizeOfImage are read from that copy, so
the captures stay the build the images on disk are. The other modules (`amdvlk64.dll`,
`ntdll.dll`, `node.exe`) are the repository's own `tests/fixtures/fixture.exe` under those
names, never a copied vendor or OS binary.

Usage (needs the DuckDB Python package):
    python trials/build_captures.py <out_dir> [SESSION ...]

<out_dir> must not exist. It receives one directory per session (all of SESSIONS by default) and
`bins/` with the images they record.
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
OWN_IMAGE = FIXTURES / "debug" / "pe" / "fixture_full.exe"
OWN_PDB = FIXTURES / "debug" / "pe" / "fixture_full.pdb"
SYSTEM_IMAGE = FIXTURES / "fixture.exe"

PID, OTHER_PID, SECOND_PID = 3112, 4242, 3113
PROCESS, OTHER_PROCESS = "storage_test.exe", "node.exe"
IBS_CYCLES = {
    "ibsEvents": {
        "op": {"interval": 4_700_000},
        "opInterval": 4_700_000,
        "opCycleCount": True,
        "opOs": True,
    }
}
IBS_OPS = {"ibsEvents": {"op": True, "opInterval": 1_000_000, "opCycleCount": False, "opOs": True}}
PMC = {
    "pmcEvents": {
        "pmcEventList": [
            {
                "hwId": 118,
                "interval": 2_592_000,
                "isSampling": True,
                "user": True,
                "os": True,
                "encodedEventId": 0x03000076,
            }
        ]
    }
}
PMC_COLUMN = "event#0x3000076"


def pe_identity(path: Path) -> tuple[int, int]:
    """(CheckSum, SizeOfImage) from a PE optional header."""
    data = path.read_bytes()
    optional = struct.unpack_from("<I", data, 0x3C)[0] + 24
    size_of_image = struct.unpack_from("<I", data, optional + 56)[0]
    checksum = struct.unpack_from("<I", data, optional + 64)[0]
    return checksum, size_of_image


@dataclass(frozen=True)
class Module:
    id: int
    path: str
    system: bool
    size: int
    checksum: int
    debug_info: bool


@dataclass(frozen=True)
class Sample:
    """count IBS samples at one RVA, with the IBS fields every one of them carries."""

    pid: int
    tid: int
    module: int
    rva: int
    count: int
    data_src: int = 0
    dc_miss: int = 0
    tag_to_ret: int = 10


# Function.name values uProf would record: names for the own exe and ntdll (PDBs on the capture
# host), none for the driver and the vendor DLL.
FUNCTION_NAMES = {
    (1, 0x1018): "fixture_entry",
    (1, 0x1000): "fixture_helper",
    (3, 0x1000): "RtlQueryPerformanceCounter",
    (3, 0x1050): "RtlAllocateHeap",
    (3, 0x1030): "NtWaitForSingleObject",
}

BASE = [
    Sample(PID, 11, 2, 0x101B, 40, 3, 1, 900),
    Sample(PID, 11, 2, 0x1030, 12, 0, 0, 40),
    Sample(PID, 12, 2, 0x1050, 8, 0, 0, 30),
    Sample(PID, 11, 3, 0x1000, 14, 0, 0, 20),
    Sample(PID, 12, 3, 0x1050, 6, 2, 1, 300),
    Sample(PID, 11, 1, 0x1018, 9, 8, 1, 2400),
    Sample(PID, 12, 1, 0x1000, 3, 0, 0, 25),
    Sample(PID, 12, 4, 0x2040, 5, 0, 0, 15),
]


class Builder:
    def __init__(self, root: Path) -> None:
        self.root = root
        bins = root / "bins"
        self.own = self._copy(OWN_IMAGE, bins / "app", OWN_PDB)
        self.far = self._copy(OWN_IMAGE, bins / "far")
        self._copy(OWN_PDB, bins / "pdbs")
        driver = self._copy(SYSTEM_IMAGE, bins / "drv", name="amdvlk64.dll")
        ntdll = self._copy(SYSTEM_IMAGE, bins / "sys", name="ntdll.dll")
        node = self._copy(SYSTEM_IMAGE, bins / "node", name="node.exe")
        # Recorded on the capture host only; no host has it, so its identity is never checked.
        vendor = str(bins / "absent" / "vendorlib.dll")
        self.modules = [
            self._module(1, self.own, False, True),
            self._module(2, driver, True, False),
            self._module(3, ntdll, True, False),
            Module(4, vendor, False, 0x10000, 0, False),
        ]
        self.node = self._module(5, node, False, False)

    @staticmethod
    def _copy(source: Path, directory: Path, *extra: Path, name: str | None = None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (name or source.name)
        shutil.copyfile(source, target)
        for path in extra:
            shutil.copyfile(path, directory / path.name)
        return target

    @staticmethod
    def _module(module_id: int, image: Path, system: bool, debug_info: bool) -> Module:
        checksum, size = pe_identity(image)
        return Module(module_id, str(image), system, size, checksum, debug_info)

    def write(
        self,
        name: str,
        modules: list[Module],
        samples: list[Sample],
        *,
        processes=((PID, PROCESS),),
        system_wide: bool = False,
        events=None,
        pmc: bool = False,
    ) -> Path:
        session = self.root / name
        session.mkdir()
        dump = not pmc
        meta = {
            "collection": {
                "schedule": {"elapsed": 10_000, "start": 1_000_000_000},
                "events": events or IBS_CYCLES,
                "target": {
                    "systemWide": system_wide,
                    "attachedProcesses": [{"processId": pid} for pid, _ in processes],
                },
            },
            "auxiliary": {"ibsOpSampling": dump},
        }
        if dump:
            meta["translateCommand"] = (
                "AMDuProfCLI.exe translate -i session --ascii ibsop-event-dump"
            )
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        self._cpu_db(session, modules, samples, processes, pmc)
        if dump:
            self._ibs_dump(session, modules, samples)
        return session

    @staticmethod
    def _cpu_db(
        session: Path, modules: list[Module], samples: list[Sample], processes, pmc: bool
    ) -> None:
        import duckdb

        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute(
                "CREATE TABLE Module (id UINTEGER, path VARCHAR, isSystemModule BOOLEAN, "
                "size UBIGINT, checksum UBIGINT, is32Bit BOOLEAN, foundDebugInfo BOOLEAN)"
            )
            connection.execute(
                "CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)"
            )
            extra = f', "{PMC_COLUMN}" DOUBLE' if pmc else ""
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries (processId UINTEGER, threadId UINTEGER, "
                f"moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT, coreId UINTEGER"
                f"{extra})"
            )
            # uProf records the full image path; --image matches all of it.
            connection.executemany(
                "INSERT INTO Process VALUES (?, ?)",
                [(pid, f"c:\\apps\\{name}") for pid, name in processes],
            )
            connection.executemany(
                "INSERT INTO Module VALUES (?, ?, ?, ?, ?, false, ?)",
                [(m.id, m.path, m.system, m.size, m.checksum, m.debug_info) for m in modules],
            )
            function_ids: dict[tuple[int, object], int] = {}
            for sample in samples:
                name = FUNCTION_NAMES.get((sample.module, sample.rva), "")
                key = (sample.module, name or sample.rva)
                if key not in function_ids:
                    function_ids[key] = len(function_ids) + 100
                    connection.execute(
                        "INSERT INTO Function VALUES (?, ?, ?)",
                        [function_ids[key], sample.module, name],
                    )
                row = [
                    sample.pid,
                    sample.tid,
                    sample.module,
                    function_ids[key],
                    sample.rva,
                    sample.tid % 4,
                ]
                if pmc:
                    connection.execute(
                        "INSERT INTO UnifiedSampleSeries VALUES (?, ?, ?, ?, ?, ?, ?)",
                        row + [sample.count],
                    )
                else:
                    connection.execute(
                        "INSERT INTO UnifiedSampleSeries VALUES (?, ?, ?, ?, ?, ?)", row
                    )
        finally:
            connection.close()

    @staticmethod
    def _ibs_dump(session: Path, modules: list[Module], samples: list[Sample]) -> None:
        paths = {m.id: m.path for m in modules}
        rows = [sample for sample in samples for _ in range(sample.count)]
        # A fixed interleaving, so threads and RVAs are not in blocks.
        rows.sort(key=lambda s: (s.pid * 31 + s.tid * 7 + s.rva) % 7)
        ticks: dict[tuple[int, int], int] = {}
        lines = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName,DataSrc,IbsDcMiss,IbsTagToRetCtr"]
        for s in rows:
            ticks[(s.pid, s.tid)] = ticks.get((s.pid, s.tid), 0) + 9_000_000
            lines.append(
                f"{ticks[(s.pid, s.tid)]},{s.tid % 4},{s.tid},{s.pid},{s.rva},"
                f"{paths[s.module]},{s.data_src},{s.dc_miss},{s.tag_to_ret}"
            )
        (session / "IbsOpDump.csv").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )


def _attached(b: Builder) -> Path:
    return b.write("attached", b.modules, BASE)


def _attached_after(b: Builder) -> Path:
    # The fix cut the own module's samples to a third; everything else is unchanged.
    after = [
        Sample(
            s.pid, s.tid, s.module, s.rva, max(1, s.count // 3), s.data_src, s.dc_miss, s.tag_to_ret
        )
        if s.module == 1
        else s
        for s in BASE
    ]
    return b.write("attached_after", b.modules, after)


def _pdb_far(b: Builder) -> Path:
    own = Builder._module(1, b.far, False, True)
    return b.write(
        "pdb_far",
        [own, b.modules[2]],
        [
            Sample(PID, 11, 1, 0x1018, 20, 0, 0, 30),
            Sample(PID, 11, 1, 0x1000, 5, 0, 0, 30),
            Sample(PID, 11, 3, 0x1000, 5, 0, 0, 20),
        ],
    )


def _systemwide(b: Builder) -> Path:
    return b.write(
        "systemwide",
        b.modules + [b.node],
        BASE + [Sample(OTHER_PID, 77, 5, 0x1030, 60), Sample(OTHER_PID, 78, 3, 0x1030, 20)],
        processes=((PID, PROCESS), (OTHER_PID, OTHER_PROCESS)),
        system_wide=True,
    )


# Background load of a typical desktop: (process image, PIDs). Every image is the repository's
# fixture.exe under that name; each process runs its own image plus ntdll and KernelBase.
LOAD = [
    ("chrome.exe", (5000, 5004, 5008)),
    ("msedge.exe", (5100,)),
    ("MsMpEng.exe", (5200,)),
    ("SearchIndexer.exe", (5300,)),
    ("explorer.exe", (5400,)),
    ("dwm.exe", (5500,)),
    ("Teams.exe", (5600,)),
    ("Code.exe", (5700,)),
    ("svchost.exe", (5800, 5804, 5808)),
    ("OneDrive.exe", (5900,)),
    ("Spotify.exe", (6000,)),
    ("slack.exe", (6100,)),
    ("steam.exe", (6200,)),
    ("Discord.exe", (6300,)),
    ("WmiPrvSE.exe", (6400,)),
    ("audiodg.exe", (6500,)),
    ("ctfmon.exe", (6600,)),
    ("RuntimeBroker.exe", (6700,)),
    ("SystemSettings.exe", (6800,)),
    ("git.exe", (6900,)),
    ("python.exe", (7000,)),
    ("conhost.exe", (7100,)),
]
LOAD_RVAS = (0x101B, 0x1031, 0x1050)


def _load(b: Builder) -> tuple[list[Module], list[Sample], list[tuple[int, str]]]:
    """Foreign processes that outrank the target: modules, samples and (PID, name) rows."""
    kernelbase = Builder._module(
        30, Builder._copy(SYSTEM_IMAGE, b.root / "bins" / "sys", name="KernelBase.dll"), True, False
    )
    modules, samples, processes = [kernelbase], [], []
    for index, (name, pids) in enumerate(LOAD):
        image = Builder._copy(SYSTEM_IMAGE, b.root / "bins" / "load", name=name)
        module = Builder._module(40 + index, image, False, False)
        modules.append(module)
        # Hot images first, falling to about the target's own hottest function.
        hot = 300 - 12 * index
        for k, pid in enumerate(pids):
            processes.append((pid, name))
            tid = pid + 1
            samples += [
                Sample(pid, tid, module.id, LOAD_RVAS[k % 3], max(30, hot // (k + 1))),
                Sample(pid, tid, 3, 0x1000, 5),
                Sample(pid, tid, 30, 0x1031, 3),
            ]
    return modules, samples, processes


def _systemwide_loaded(b: Builder) -> Path:
    """systemwide under desktop load: the target is 2-10 % and not the top process."""
    modules, samples, processes = _load(b)
    target = [
        Sample(s.pid, s.tid, s.module, s.rva, 2 * s.count, s.data_src, s.dc_miss, s.tag_to_ret)
        for s in BASE
    ]
    # A second instance of the same image, briefly busy.
    target.append(Sample(SECOND_PID, 21, 1, 0x1000, 10))
    return b.write(
        "systemwide_loaded",
        b.modules + [b.node] + modules,
        target + samples,
        processes=((PID, PROCESS), (SECOND_PID, PROCESS), *processes),
        system_wide=True,
    )


# Outside code inside the user's own process: runtime, graphics and vendor DLLs it loads.
INPROC = [
    "ucrtbase.dll",
    "vcruntime140.dll",
    "msvcp140.dll",
    "KernelBase.dll",
    "combase.dll",
    "rpcrt4.dll",
    "user32.dll",
    "win32u.dll",
    "gdi32full.dll",
    "dxgi.dll",
    "d3d12.dll",
    "d3d12core.dll",
    "dxcore.dll",
    "amdxc64.dll",
    "bcrypt.dll",
    "ws2_32.dll",
    "physics.dll",
    "audio_mix.dll",
    "netcore.dll",
    "assetio.dll",
    "zstd.dll",
    "tbb12.dll",
]


def _attached_inproc_loaded(b: Builder) -> Path:
    """attached, with 22 in-process DLLs each hotter than the own hottest function."""
    modules, samples = [], []
    for index, name in enumerate(INPROC):
        image = Builder._copy(SYSTEM_IMAGE, b.root / "bins" / "inproc", name=name)
        module = Builder._module(60 + index, image, index < 16, False)
        modules.append(module)
        samples.append(Sample(PID, 11 + index % 2, module.id, LOAD_RVAS[index % 3], 40 - index))
    return b.write("attached_inproc_loaded", b.modules + modules, BASE + samples)


def _opcount(b: Builder) -> Path:
    return b.write("opcount", b.modules, BASE, events=IBS_OPS)


def _pmc(b: Builder) -> Path:
    return b.write("pmc", b.modules, BASE, events=PMC, pmc=True)


def _timer(b: Builder) -> Path:
    """The committed real timer capture, with its image and PDB copied to bins/timer."""
    sys.path.insert(0, str(REPO / "tests"))
    import timer_fixture

    session = timer_fixture.build(b.root / "timer")
    for name in ("heap_churn.exe", "heap_churn.pdb"):
        Builder._copy(timer_fixture.FIXTURE / name, b.root / "bins" / "timer")
    return session


SESSIONS = {
    "attached": _attached,
    "attached_after": _attached_after,
    "pdb_far": _pdb_far,
    "systemwide": _systemwide,
    "opcount": _opcount,
    "pmc": _pmc,
    "timer": _timer,
    "systemwide_loaded": _systemwide_loaded,
    "attached_inproc_loaded": _attached_inproc_loaded,
}


def build(root: Path, names: list[str] | None = None) -> dict[str, Path]:
    """Write the named sessions (all by default) under root, which must not exist."""
    unknown = sorted(set(names or ()) - set(SESSIONS))
    if unknown:
        raise ValueError(
            f"unknown session(s) {', '.join(unknown)}; choose from {', '.join(SESSIONS)}"
        )
    root.mkdir(parents=True)
    builder = Builder(root)
    return {name: SESSIONS[name](builder) for name in (names or SESSIONS)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir", type=Path, help="new directory for the sessions")
    parser.add_argument(
        "sessions",
        nargs="*",
        metavar="SESSION",
        help=f"sessions to build (default: all of {', '.join(SESSIONS)})",
    )
    args = parser.parse_args(argv)
    if args.out_dir.exists():
        parser.error(f"{args.out_dir} exists; choose a new directory")
    for name, path in build(args.out_dir, args.sessions).items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
