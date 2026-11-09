"""Profile orchestration: validate the arguments, resolve the dump and symbols, emit the result.

All set, join, aggregation, top-N and share work lives in the SQL files. Python owns argument
validation, session.uprof parsing, symbolization, temp files and output formatting.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, TextIO

import uprof_db
import uprof_context
import uprof_dump
import uprof_functions
import uprof_session
import uprof_symbols
import uprof_util as util

TOOLS_DIR: Final = Path(os.path.abspath(__file__)).parent
GRANULARITY_LABELS: Final = MappingProxyType({
    "process": "per Process.id",
    "module": "per module path",
    "region": "per 4 KiB address page",
    "function": "per derived function range (Function.id when no range)",
    "line": "per file:line",
    "instruction": "per module offset",
})
# Scopes that symbolize every module in scope. Function also runs symbolization so an accepted
# PDB supplies public symbols to the range derivation.
SYMBOLIZED: Final = ("function", "region", "line", "instruction")
# Scopes whose rows are grouped by function ranges derived from the PE image.
RANGE_SCOPES: Final = ("function", "region")
_ALL_NS: Final = "9223372036854775807"


def _check_args(args: argparse.Namespace) -> None:
    if args.command != "profile":
        return
    if getattr(args, "image", None) == "":
        raise util.UprofError("--image must not be empty; use '%' to match every process image")
    if args.pid is not None and args.pid < 0:
        raise util.UprofError("--pid needs a non-negative PID")


def _top_value(value: str) -> int:
    text = str(value).strip().lower()
    if text == "all":
        return 0
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number <= 0:
        raise util.UprofError("--top needs a positive row count or 'all'")
    return number


def _time_range(args: argparse.Namespace) -> tuple[str, str]:
    """Turn T1:T2 seconds into [start, end) nanoseconds for the DeltaTick filter."""
    if not args.time_filter:
        return "0", _ALL_NS
    bounds = util.parse_time_filter(args.time_filter)
    if bounds is None:
        raise util.UprofError("--time-filter needs T1:T2 in seconds from capture start "
                              "(for example 0:10)")
    start, end = bounds
    if end <= start:
        raise util.UprofError(f"--time-filter needs T1 < T2; got {args.time_filter}")
    scale = Decimal(1_000_000_000)
    return str(int(start * scale)), str(int(end * scale))


def _out_path(args: argparse.Namespace, cpu_db: Path, session: Path) -> Path | None:
    if not args.out_file:
        return None
    out_path = util.absolute(args.out_file)
    if not out_path.parent.is_dir():
        raise util.UprofError(f"output directory not found: {out_path.parent}")
    if out_path.is_dir():
        raise util.UprofError(f"output path is a directory: {out_path}")
    for protected in (cpu_db, session / "session.uprof"):
        if os.path.normcase(os.fspath(util.absolute(protected))) == os.path.normcase(
                os.fspath(out_path)):
            raise util.UprofError(f"--out-file would overwrite {util.absolute(protected)}")
    return out_path


def _variables(args: argparse.Namespace, dump: uprof_dump.DumpInfo | None, top: int,
               time_start: str, time_end: str, event_column: str | None,
               sample_interval_s: float | None = None,
               primary_measure: str = "samples") -> dict[str, str]:
    meta_path = util.absolute(args.session) / "session.uprof"
    return {
        "uprof_image": args.image if args.image is not None else "%",
        "uprof_pid": "" if args.pid is None else str(args.pid),
        "uprof_top": str(top),
        "uprof_time_start": time_start,
        "uprof_time_end": time_end,
        "uprof_ibs_dump": str(dump.path) if dump else "",
        "uprof_event_column": event_column or "",
        "uprof_session_meta": str(meta_path) if meta_path.is_file() else "",
        "uprof_tag_available": "",
        "uprof_symbols": "",
        "uprof_aliases": "",
        "uprof_pdbs": "",
        "uprof_functions": "",
        "uprof_range_attempted": "",
        "uprof_sample_interval_s": "" if sample_interval_s is None else repr(sample_interval_s),
        "uprof_primary_measure": primary_measure,
    }


def _module_debug_info(connection: object) -> dict[str, bool]:
    """Module.foundDebugInfo keyed by the full normalized path and its basename, when present."""
    columns = {str(row[0]).lower() for row in connection.execute(
        "SELECT column_name FROM duckdb_columns() WHERE lower(table_name) = 'module'").fetchall()}
    if "founddebuginfo" not in columns:
        return {}
    rows = connection.execute("""
        SELECT key, CAST(bool_or(found) AS BOOLEAN)
        FROM (
          SELECT module_key(path) AS key, CAST(foundDebugInfo AS BOOLEAN) AS found
          FROM Module WHERE COALESCE(path, '') <> ''
          UNION ALL
          SELECT regexp_extract(module_key(path), '([^/]+)$', 1) AS key,
                 CAST(foundDebugInfo AS BOOLEAN) AS found
          FROM Module WHERE COALESCE(path, '') <> ''
        )
        WHERE key <> '' GROUP BY key""").fetchall()
    return {str(key): bool(found) for key, found in rows}


def _evidence_cause(evidence: uprof_symbols.ModuleEvidence) -> str:
    """One-line per-module cause for SYMBOL_COVERAGE; the stderr evidence line has the detail."""
    if evidence.outcome == "no_debug_info":
        return "no debug information is recorded for this module in the capture"
    if evidence.outcome == "no_pdb":
        if evidence.symbolizer and evidence.symbolizer.errors:
            return f"no PDB found; llvm-symbolizer: {evidence.symbolizer.errors[0][1]}"
        return ("no PDB found beside the module, at the recorded CodeView path or in a local "
                "srv* cache; _NT_SYMBOL_PATH is not read by llvm-symbolizer")
    if evidence.outcome == "readobj_failed":
        return f"llvm-readobj could not read the debug directory: {evidence.note or 'unknown'}"
    if evidence.outcome == "module_missing":
        return f"module file not available: {evidence.note or 'no recorded path'}"
    return f"symbolization failed: {evidence.note or evidence.outcome}"


def _symbolize_scope(connection: object, artifacts: Path, stream: TextIO,
                     args: argparse.Namespace) -> tuple[dict[str, str], dict[str, str]]:
    """Symbolize every module in scope; return the artifact variables and per-module causes."""
    _, rows = uprof_db.fetch(connection, TOOLS_DIR / "profile-rvas.sql")
    modules: dict[str, list] = {}
    for module_name, module_path, rva in rows:
        if module_name not in modules:
            modules[module_name] = [module_path, []]
        modules[module_name][1].append(rva)
    symbols, publics, pdbs, evidence = uprof_symbols.symbolize_modules(
        [(name, path, rvas) for name, (path, rvas) in modules.items()], artifacts,
        stream=stream, debug_info=_module_debug_info(connection),
        pdb_file=getattr(args, "pdb_file", None),
        pdb_dirs=getattr(args, "pdb_dir", None) or (),
        debug_dirs=getattr(args, "debug_file_dir", None) or (),
        fallback_paths=getattr(args, "fallback_debug_path", None) or ())
    causes = {item.module_name: _evidence_cause(item) for item in evidence
              if item.outcome != "resolved"}
    return {
        "uprof_symbols": str(symbols) if symbols else "",
        "uprof_aliases": str(publics) if publics else "",
        "uprof_pdbs": str(pdbs) if pdbs else "",
    }, causes


def _derive_scope_ranges(connection: object, artifacts: Path | None,
                         stream: TextIO) -> tuple[Path | None, dict[str, Any]]:
    """Derive function ranges for modules whose samples uProf did not name.

    Only a module with at least one empty Function.name and an on-disk file is read; a fully
    named module keeps its Function.id rows and pays no llvm-readobj run. The returned evidence
    names each module that produced no range, so the function view's fallback is explained.
    """
    info: dict[str, Any] = {"modules": 0, "ranges": 0, "absent_by_module": {}, "attempted": []}
    _, rows = uprof_db.fetch(connection, TOOLS_DIR / "profile-function-modules.sql")
    ranges: list[uprof_functions.FunctionRange] = []
    for module_name, module_path, _unresolved, _samples in rows:
        name = str(module_name)
        info["attempted"].append(name)
        path = str(module_path or "")
        if not path:
            info["absent_by_module"][name] = "the capture records no module path"
            continue
        if not Path(path).is_file():
            info["absent_by_module"][name] = f"{path} is not on this host"
            continue
        publics = connection.execute(
            "SELECT rva, names FROM v_public_starts WHERE module_name = CAST(? AS VARCHAR)",
            [name]).fetchall()
        result = uprof_functions.derive_module_ranges(
            name, path, uprof_functions.public_starts_from_names(publics))
        if not result.ranges:
            reason = result.reason or "no function range"
            info["absent_by_module"][name] = reason
            print(f"WARNING: no function range derived for {name} ({path}): {reason}",
                  file=stream)
            continue
        ranges.extend(result.ranges)
        info["modules"] += 1
        print(f"FUNCTION_RANGES: {name}: {len(result.ranges)} ranges from {path}", file=stream)
    info["ranges"] = len(ranges)
    if not ranges:
        return None, info
    target = artifacts if artifacts is not None else Path(
        tempfile.mkdtemp(prefix="uprof-functions-"))
    return uprof_functions.write_ranges_csv(target / "functions.csv", ranges), info


def _dump_field(dump: uprof_dump.DumpInfo) -> str:
    size_mb = round(dump.path.stat().st_size / (1024 * 1024), 1)
    filter_note = f" (time filter {dump.time_filter})" if dump.time_filter else ""
    if dump.source == "generated":
        return (f"{dump.path}, {size_mb} MB; cache {dump.cache_dir} generated "
                f"({dump.reason}{filter_note}); the source session is untouched")
    if dump.source == "reused":
        return (f"{dump.path}, {size_mb} MB; cache {dump.cache_dir} reused "
                f"({dump.reason}{filter_note})")
    return f"{dump.path}, {size_mb} MB; {dump.reason}"


_TIME_SHARE_MEANING: Final = (
    "time share estimated from the gaps between per-sample timestamps (idle/wait is charged to "
    "the ending sample; the first sample per CPU is ignored, no gap before it; NULL sorts last)")
# An attached thread's samples span whichever CPUs it ran on; partitioning by TID keeps its
# migrating gaps from being counted once per CPU. The gap includes time the thread was off-CPU.
_TIME_SHARE_MEANING_ATTACHED: Final = (
    "time share estimated from the gaps between per-sample timestamps within each thread "
    "(PID, TID), so a thread that migrates between CPUs is counted once; the gap includes "
    "off-CPU time and is charged to the ending sample, the first sample per thread is ignored "
    "(no gap before it), and NULL sorts last")
_CYCLE_BASIS: Final = ("; for a cycle-controlled IBS capture sample_share (fixed cycles per "
                       "sample) is the CPU-cycle share and the better ranking")
_TAG_SHARE_MEANING: Final = (
    "share of tag-to-retire cycles summed over sampled ops (AMD tag at dispatch to retirement, "
    "queue/stalls included; 16-bit counter clipped at 65535); a total, not per-op latency")
# Only for frequency shares: those count sampled operations, not fixed work per sample.
_SAMPLE_CAVEAT: Final = ("a raw share cannot separate a cheap operation run often from an "
                         "expensive one run rarely")
_SYSTEM_MODULE_MEANING: Final = (
    "Module.isSystemModule: true = OS-shipped module, false = not OS-shipped")
# A cycle interval is time only at a frequency; the session records the interval in cycles, not
# the frequency of the capture machine.
_CPU_SECONDS_MEANING: Final = (
    "sampled CPU seconds = samples x the requested cycle interval / this machine's reported core "
    "frequency (the session records no frequency, so a capture from other hardware is "
    "approximate); CPU time, not wall-clock elapsed time")
_CPU_SECONDS_SYSTEM_WIDE: Final = (
    "; a system-wide capture sums CPU time across cores, and halted cycles are excluded")
_CPU_SECONDS_ATTACHED: Final = (
    "; an attached thread's off-CPU time is not sampled, so this is its running CPU time")
_NEXT_BY: Final = {
    "process": ("module",),
    "module": ("function",),
    "function": (),
    "region": (),
    "line": (),
    "instruction": (),
}
_REGION_MEANING: Final = (
    "4 KiB address page; samples are grouped by address, not by control flow; a page may span "
    "unrelated code; the hot RVA is the precise attribution and the page is the coarse bucket")
_NO_NAMED_FUNCTION_STOP: Final = (
    "a module with no source names is covered by derived function ranges: use profile --by "
    "function, which derives boundaries from the binary (.pdata, PE exports, PDB publics) with "
    "LLVM; do not write a PE parser, map .pdata by hand or disassemble")
_UNRESOLVED_FUNCTION_STOP: Final = (
    "unresolved samples are covered by derived function ranges: use profile --by function, which "
    "derives boundaries from the binary (.pdata, PE exports, PDB publics) with LLVM; do not write "
    "a PE parser, map .pdata by hand or disassemble")
_RANGE_UNRESOLVED_STOP: Final = (
    "no named or derived range covers these samples; function_reason and "
    "legend.function_ranges.absent_by_module name why, and --by region is the page fallback "
    "where no boundary source exists")
_SYMBOL_PATH_REMEDY: Final = (
    "; remedy: pass profile --pdb-file <file> or --pdb-dir <dir> for a PDB, or "
    "--debug-file-dir/--fallback-debug-path <dir> for DWARF, or place the module's debug file "
    "beside it; llvm-symbolizer ignores _NT_SYMBOL_PATH, and only an adjacent PDB, the recorded "
    "CodeView path or the skill's local srv* cache scan is searched")
# Report order is the resolution order in uprof_symbols._candidate_pdbs.
_SYMBOL_PATH_FLAGS: Final = (
    ("pdb_file", "--pdb-file"),
    ("pdb_dir", "--pdb-dir"),
    ("debug_file_dir", "--debug-file-dir"),
    ("fallback_debug_path", "--fallback-debug-path"),
)


def _sample_share_meaning(sampler: uprof_session.Sampler,
                          dump: uprof_dump.DumpInfo | None) -> str:
    """What sample_share is a fraction of in this capture mode."""
    if dump is None:
        if sampler.pmc_cycle_shares:
            return "busy core-cycle share (fixed cycles per sample; halted excluded)"
        return ("not established (this PMC event is not the supported cycle event); capture "
                "--event cycles (CYCLES_NOT_IN_HALT, hwId 118) to make sample_share a "
                "core-cycle share")
    if sampler.ibs_dump_kind == "ibsfetch-event-dump":
        return "fetch-sample frequency share (fixed fetch events per sample)"
    if sampler.interval_units == "ops":
        return "dispatched-op frequency share (fixed dispatched ops per sample)"
    if sampler.ibs_dump_kind == "ibsop-event-dump":
        return "core-cycle share (fixed cycles per sample), not wall time"
    return "not established (the session records no usable sampler)"


def _cycle_controlled(sampler: uprof_session.Sampler) -> bool:
    """True when IBS OP samples carry a fixed cycle count, so sample_share is a CPU-cycle share."""
    return sampler.ibs_dump_kind == "ibsop-event-dump" and sampler.interval_units == "cycles"


def _cpu_seconds_interval(sampler: uprof_session.Sampler) -> float | None:
    """Seconds per sample, or None when a sample is not a fixed cycle count."""
    if not (sampler.pmc_cycle_shares or _cycle_controlled(sampler)):
        return None
    interval_s, _requested, _ideal = uprof_session.requested_interval(sampler)
    return interval_s


def _quote(text: str) -> str:
    """Quote one shell argument; on Windows also protect %NAME% from cmd.exe expansion."""
    if os.name == "nt":
        text = text.replace("%", "%%")
    if text and all(char.isalnum() or char in "._-/:\\%+," for char in text):
        return text
    return '"' + text.replace('"', '\\"') + '"'


def _profile_command(args: argparse.Namespace, by: str, json_mode: bool = False) -> str:
    """A runnable drill-down command that keeps the session and the process/image scope."""
    parts = ["python", _quote(str(TOOLS_DIR / "uprof.py")), "profile",
             "--session", _quote(str(util.absolute(args.session))), "--by", by]
    if args.pid is not None:
        parts += ["--pid", str(args.pid)]
    elif args.image is not None and args.image != "%":
        parts += ["--image", _quote(args.image)]
    if args.time_filter:
        parts += ["--time-filter", _quote(args.time_filter)]
    if getattr(args, "pdb_file", None):
        parts += ["--pdb-file", _quote(args.pdb_file)]
    for option, values in (("--pdb-dir", getattr(args, "pdb_dir", None)),
                           ("--debug-file-dir", getattr(args, "debug_file_dir", None)),
                           ("--fallback-debug-path",
                            getattr(args, "fallback_debug_path", None))):
        for value in values or ():
            parts += [option, _quote(value)]
    if args.top != "20":
        parts += ["--top", str(args.top)]
    if json_mode:
        parts.append("--json")
    return " ".join(parts)


def _next_calls(args: argparse.Namespace,
                row_attribution: dict[str, int] | None = None) -> list[str]:
    """Drill-down calls, stopping at the function unless its rows are unresolved.

    The CPU-time path ends at the derived function. Region is the page fallback, offered only
    when function rows still carry unresolved samples; instruction context is a separate request,
    not a default drill-down.
    """
    calls = [_profile_command(args, by) for by in _NEXT_BY[args.by]]
    if args.by == "function" and row_attribution and row_attribution.get("unresolved", 0) > 0:
        calls.append(_profile_command(args, "region"))
    if args.by == "line":
        calls.append(_profile_command(args, "instruction", json_mode=True))
    return calls


def _cpu_seconds_absent(sampler: uprof_session.Sampler) -> str:
    """Why cpu_seconds is not emitted for this capture."""
    if sampler.interval_units == "ops":
        return "the requested interval is a work count, not time"
    if sampler.interval_units == "fetch":
        return "the requested interval is a fetch-event count, not time"
    if sampler.interval is None or sampler.interval_units is None:
        return "the session records no cycle interval"
    return "the core frequency is unavailable on this machine"


def _primary_measure(sampler: uprof_session.Sampler,
                     dump: uprof_dump.DumpInfo | None) -> tuple[str, str]:
    """The column that orders rows and --top for this capture, and why.

    A fixed unit per sample makes sample_share the ranking: a cycle-controlled or op-count IBS
    capture and a PMC cycle event. A gap-weighted (time-based) capture ranks by time_share. An
    unsupported PMC event leaves sample_share not established, so raw samples order the rows.
    """
    if dump is None:
        if sampler.pmc_cycle_shares:
            return "sample_share", ("this PMC cycle capture counts fixed cycles per sample, so "
                                    "sample_share is the busy core-cycle share")
        return "samples", ("sample_share is not established for this PMC event, so rows are "
                           "ordered by raw sample count")
    if (sampler.ibs_dump_kind == "ibsop-event-dump"
            and sampler.interval_units in ("cycles", "ops")):
        if sampler.interval_units == "cycles":
            return "sample_share", ("this cycle-controlled IBS capture counts fixed cycles per "
                                    "sample, so sample_share is the core-cycle share")
        return "sample_share", ("this op-count IBS capture counts a fixed number of dispatched "
                                "ops per sample, so sample_share is the dispatched-op frequency "
                                "share")
    return "time_share", ("this gap-weighted (time-based) capture has per-sample timestamps, so "
                          "time_share ranks where time went")


def _legend(sampler: uprof_session.Sampler, dump: uprof_dump.DumpInfo | None,
            has_tag: bool, tag_available: bool,
            args: argparse.Namespace, system_wide: bool,
            sample_interval_s: float | None = None,
            has_system_module: bool | None = None,
            range_info: dict[str, Any] | None = None,
            row_attribution: dict[str, int] | None = None) -> dict[str, Any]:
    """Machine-readable meaning of every share column, the ordering estimate and next calls.

    sample_share's meaning is mode-specific; time_share is a time share estimated from gaps, not
    unqualified impact; tag_to_retire_share is a summed total, not per-op latency. A PMC capture
    has no DeltaTick gaps, so its rows fall back to the raw sample count. A system-wide capture
    partitions gaps by CPU; an attached one by (PID, TID), so a migrating thread is counted once.
    """
    columns: dict[str, str] = {
        "samples": "raw sample count",
        "sample_share": _sample_share_meaning(sampler, dump),
    }
    if args.by == "function":
        columns["attribution"] = ("unique / derived (synthetic binary range) / unresolved (no "
                                  "symbol)")
        columns["function_start"] = ("first RVA of the derived function range (inclusive); NULL "
                                     "when no range was derived")
        columns["function_end"] = "address after the function range (exclusive)"
        columns["function_source"] = ("boundary source of the range: pdb_public, export, pdata "
                                      "or none (no boundary source; the row falls back to uProf "
                                      "Function.id or is unresolved)")
        columns["function_derived"] = ("true when the displayed name is synthetic "
                                       "(sub_<rva>/pdata_<start>), not a source symbol")
        columns["hot_rva"] = ("highest-sample RVA in the function; ties pick the lowest RVA; the "
                              "precise attribution inside the range")
        columns["hot_samples"] = "samples at hot_rva"
        columns["hot_sample_share"] = ("hot_rva's samples as a share of the scoped capture "
                                       "total")
        columns["function_reason"] = ("why this row has no derived range "
                                      "(function_start/function_end are NULL)")
    elif args.by in ("line", "instruction"):
        columns["attribution"] = "unique / shared (folded names) / unresolved (no symbol)"
    elif args.by == "region":
        columns["attribution"] = ("hot_rva: unique / shared (folded names) / derived (synthetic "
                                  "binary range) / unresolved (no symbol)")
        columns["region"] = _REGION_MEANING
        columns["sampled_addresses"] = "distinct RVAs with samples in the page"
        columns["hot_rva"] = "highest-sample RVA in the page; ties pick the lowest RVA"
        columns["hot_samples"] = "samples at hot_rva"
        columns["hot_sample_share"] = ("hot_rva's samples as a share of the scoped capture "
                                       "total")
        columns["function_source"] = ("hot_rva's boundary source: pdb_public, export, pdata or "
                                      "none (no range covers hot_rva)")
        columns["function_derived"] = ("true when the hot function name is synthetic "
                                       "(sub_<rva>/pdata_<start>), not a source symbol")
        columns["symbol_reason"] = ("why function/file/line are NULL for hot_rva")
    denominators: dict[str, str] = {"sample_share": "total_samples"}
    absent: dict[str, str] = {}
    if dump is None:
        absent["time_share"] = ("this export aggregates PMC samples per window; capture with "
                                "--event ibs-op (IBS OP with per-sample timestamps) to get "
                                "time_share")
        absent["tag_to_retire_share"] = "this export has no tag-to-retire counter"
    else:
        if system_wide:
            columns["time_share"] = _TIME_SHARE_MEANING
        else:
            columns["time_share"] = (_TIME_SHARE_MEANING_ATTACHED
                                     + (_CYCLE_BASIS if _cycle_controlled(sampler) else ""))
        denominators["time_share"] = "total_weight"
        if tag_available:
            columns["tag_to_retire_share"] = _TAG_SHARE_MEANING
            denominators["tag_to_retire_share"] = "total_tag_cycles"
        elif sampler.ibs_dump_kind == "ibsop-event-dump" and not has_tag:
            absent["tag_to_retire_share"] = "this dump has no IbsTagToRetCtr column"
        elif sampler.ibs_dump_kind == "ibsop-event-dump":
            absent["tag_to_retire_share"] = (
                "IbsTagToRetCtr has no nonzero values in this dump, so every share is NULL; "
                "the tag-to-retire counter comes from an IBS OP capture (--event ibs-op), not "
                "from PMC or IBS fetch")
        else:
            absent["tag_to_retire_share"] = "this export has no tag-to-retire counter"
    # A PMC sample is a fixed cycle count, so the sample order ranks cycles when the event is the
    # supported cycle event; an IBS sample is a selected operation and only time_share ranks.
    if dump is None:
        order_by = ["samples"]
        gap_weighted = False
        if sampler.pmc_cycle_shares:
            estimate = ("sample_share is the busy core-cycle share (fixed cycles per sample, "
                        "halted excluded), so the sample count ranks where cycles were spent")
        else:
            estimate = ("row order is raw sample count only; sample_share is not established, "
                        "so this is not a time or impact claim")
    else:
        order_by = ["time_share", "samples"]
        gap_weighted = True
        if system_wide:
            estimate = ("time_share is a time share estimated from the gaps between per-sample "
                        "timestamps; idle/wait is charged to the ending sample, the first sample "
                        "per CPU is ignored (no gap before it), and NULL sorts last")
        else:
            estimate = ("time_share is a time share estimated from the gaps between per-sample "
                        "timestamps within each thread (PID, TID), so a migrating thread is "
                        "counted once; off-CPU time is charged to the ending sample, the first "
                        "sample per thread is ignored (no gap before it), and NULL sorts last"
                        + (_CYCLE_BASIS if _cycle_controlled(sampler) else ""))
    if args.by in ("module", "region", "function"):
        if args.by == "module":
            columns["hot_rva"] = ("highest-sample RVA in the module; ties pick the lowest RVA; "
                                  "the precise attribution inside the module")
            columns["hot_samples"] = "samples at hot_rva"
            columns["hot_sample_share"] = ("hot_rva's samples as a share of the scoped capture "
                                           "total")
        if has_system_module:
            columns["isSystemModule"] = _SYSTEM_MODULE_MEANING
        else:
            absent["isSystemModule"] = ("this export has no Module.isSystemModule column; "
                                        "third-party cannot be classified")
        if sample_interval_s is not None:
            columns["cpu_seconds"] = (_CPU_SECONDS_MEANING
                                      + (_CPU_SECONDS_SYSTEM_WIDE if system_wide
                                         else _CPU_SECONDS_ATTACHED))
        else:
            absent["cpu_seconds"] = _cpu_seconds_absent(sampler)
    result: dict[str, Any] = {
        "share_unit": "fraction",
        "share_scale": 1,
        "denominators": denominators,
        "columns": columns,
        "absent": absent,
        "impact": {"order_by": order_by, "gap_weighted": gap_weighted, "estimate": estimate},
        "remainder_last": True,
        "next": _next_calls(args, row_attribution),
    }
    if args.by in ("process", "module", "function", "region"):
        measure, reason = _primary_measure(sampler, dump)
        result["impact"]["order_by"] = [measure] + ([] if measure == "samples" else ["samples"])
        result["impact"]["gap_weighted"] = measure == "time_share"
        result["impact"]["primary_measure"] = measure
        result["impact"]["primary_measure_reason"] = reason
        noun = {"process": "processes", "module": "modules", "function": "functions",
                "region": "pages"}[args.by]
        result["impact"]["estimate"] = (
            f"{reason}; --top selects the highest rows by {measure}, so the default 20 already "
            f"carries the hot {noun}")
        if args.by in ("function", "region"):
            precise = ("hot_rva is the highest-sample RVA inside the function"
                       if args.by == "function"
                       else ("hot_rva is the highest-sample RVA inside the page and the precise "
                             "attribution"))
            result["impact"]["estimate"] += f"; {precise}"
    if args.by == "instruction":
        measure, reason = _primary_measure(sampler, dump)
        result["impact"]["order_by"] = [measure] + ([] if measure == "samples" else ["samples"])
        result["impact"]["gap_weighted"] = measure == "time_share"
        result["impact"]["primary_measure"] = measure
        result["impact"]["primary_measure_reason"] = reason
        result["impact"]["estimate"] = (
            f"{reason}; --top selects the highest instruction rows by {measure}")
        if measure == "time_share":
            result["impact"]["sample_hot"] = (
                "this instruction table is time_share-ordered, not sample-hot: do not read its "
                "first row as the sample-hot RVA or claim that no single instruction exceeds a "
                "sample threshold; the function or region hot_rva and hot_samples are the "
                "sample-hot attribution")
        else:
            result["impact"]["sample_hot"] = (
                "this instruction table is sample-ordered, so its first row is the sample-hot "
                "instruction and agrees with the function or region hot_rva")
    if args.by == "function":
        info = range_info or {}
        result["function_ranges"] = {
            "source": ("PE runtime-function starts (.pdata), PE exports and PDB public symbols, "
                       "read with LLVM; a range ends at the next boundary in the same executable "
                       "section or at its end"),
            "derived_modules": int(info.get("modules", 0)),
            "ranges": int(info.get("ranges", 0)),
            "absent_by_module": info.get("absent_by_module") or {},
            "fallback": ("a fully named module is not read, so its rows keep the uProf Function.id "
                         "grouping and function_reason says derivation was skipped; a module with "
                         "no derived range groups by Function.id and function_reason says why no "
                         "range exists"),
            "meaning": ("function is the semantic unit; the tool derives the boundaries, so a "
                        "consumer must not write a PE parser or map .pdata by hand; region is "
                        "the fallback when no boundary source exists or a diffuse page view is "
                        "wanted"),
        }
    if args.by == "region":
        info = range_info or {}
        result["region"] = {
            "page_size": 4096,
            "key": "module + RVA aligned down to 4096",
            "page_start": "first address in the page (inclusive)",
            "page_end": "address after the page (exclusive)",
            "sampled_addresses": "distinct RVAs with samples in the page",
            "hot_rva": "highest-sample RVA in the page; ties pick the lowest RVA",
            "hot_samples": "samples at hot_rva",
            "hot_sample_share": "hot_samples / total_samples",
            "function_source": ("hot_rva's boundary source: pdb_public, export, pdata or none "
                                "(no range covers hot_rva)"),
            "function_derived": ("true when the hot function name is synthetic, not a source "
                                 "symbol"),
            "symbol_reason": "why function/file/line are NULL for the hot RVA",
            "derived_function": ("the hot RVA carries its derived function when a range covers "
                                 "it; region is the fallback when no boundary source exists"),
            "function_ranges": {"derived_modules": int(info.get("modules", 0)),
                                "ranges": int(info.get("ranges", 0))},
        }
    return result


def _read_text(legend: dict[str, Any], system_wide: bool) -> str:
    """The positive reading rule, plus the ranking and remainder rule."""
    sample = legend["columns"]["sample_share"]
    if "function_ranges" in legend:
        measure = legend["impact"]["primary_measure"]
        reason = legend["impact"]["primary_measure_reason"]
        if measure == "time_share":
            read = f"time_share orders the functions: {reason}; sample_share is not a per-op cost"
        elif measure == "sample_share":
            read = f"sample_share orders the functions: {reason}"
        else:
            read = f"rows are ordered by raw sample count: {reason}"
        read += ("; hot_rva is the highest-sample RVA inside the function; function_start and "
                 "function_end delimit the derived range; a name with function_derived=true is "
                 "synthetic (sub_/pdata_), derived from the binary, not a source symbol; "
                 "function_reason explains a row with no derived range")
        if "tag_to_retire_share" in legend["columns"]:
            read += "; tag_to_retire_share is total tag-to-retire cycles (higher = more wait)"
        if sample.startswith(("dispatched-op", "fetch-sample")):
            read += f"; {_SAMPLE_CAVEAT}"
        read += "; the <other functions> row is last, not ranked"
        return read
    if "region" in legend:
        measure = legend["impact"]["primary_measure"]
        reason = legend["impact"]["primary_measure_reason"]
        if measure == "time_share":
            read = f"time_share orders the pages: {reason}; sample_share is not a per-op cost"
        elif measure == "sample_share":
            read = f"sample_share orders the pages: {reason}"
        else:
            read = f"rows are ordered by raw sample count: {reason}"
        read += ("; hot_rva is the precise attribution inside the page and sampled_addresses "
                 "shows whether the page is a tight cluster or a diffuse bucket; hot_rva's "
                 "function_source names the boundary source and function_derived=true marks a "
                 "synthetic binary name; a page may span unrelated code, so do not read it as a "
                 "contiguous region, hot path or basic block")
        if "tag_to_retire_share" in legend["columns"]:
            read += "; tag_to_retire_share is total tag-to-retire cycles (higher = more wait)"
        if sample.startswith(("dispatched-op", "fetch-sample")):
            read += f"; {_SAMPLE_CAVEAT}"
        read += "; the <other regions> row is last, not ranked"
        return read
    measure = legend["impact"].get("primary_measure")
    if measure == "time_share":
        if system_wide:
            read = "time_share ranks where time went; sample_share is not a per-op cost"
        else:
            read = ("time_share ranks where each thread's elapsed time went (PID, TID partition, "
                    "so a migrating thread is counted once); sample_share is not a per-op cost")
        if "tag_to_retire_share" in legend["columns"]:
            read += "; tag_to_retire_share is total tag-to-retire cycles (higher = more wait)"
        if sample.startswith(("dispatched-op", "fetch-sample")):
            read += f"; {_SAMPLE_CAVEAT}"
    elif measure == "sample_share":
        if sample.startswith("busy core-cycle"):
            read = ("sample_share is the busy core-cycle share, so the sample order ranks where "
                    "cycles were spent")
        else:
            read = f"sample_share orders the rows: {sample}"
            if sample.startswith(("dispatched-op", "fetch-sample")):
                read += f"; {_SAMPLE_CAVEAT}"
        if "tag_to_retire_share" in legend["columns"]:
            read += "; tag_to_retire_share is total tag-to-retire cycles (higher = more wait)"
    elif sample.startswith("not established"):
        read = ("sample_share is not established for this event; rows are ordered by raw sample "
                "count only, not a time or impact claim")
    else:
        read = ("sample_share is the busy core-cycle share, so the sample order ranks where "
                "cycles were spent")
    if "sample_hot" in legend["impact"]:
        read += f"; {legend['impact']['sample_hot']}"
    read += "; the <other ...> row is last, not ranked"
    return read


def _next_text(calls: list[str]) -> str:
    """Full runnable calls joined by && so the Markdown line can be pasted as one command."""
    return " && ".join(calls)


def _legend_fields(legend: dict[str, Any], system_wide: bool) -> list[tuple[str, str]]:
    """The short legend: columns and unit, what to read, and runnable next calls."""
    parts = []
    if "attribution" in legend["columns"]:
        parts.append(f"attribution = {legend['columns']['attribution']}")
    parts += ["samples = raw count", f"sample_share = {legend['columns']['sample_share']}"]
    if "time_share" in legend["columns"]:
        if system_wide:
            parts.append("time_share = time share")
        else:
            parts.append("time_share = per-thread time share (off-CPU gaps are charged to the "
                         "ending sample)")
    else:
        parts.append(f"time_share absent ({legend['absent']['time_share']})")
    if "tag_to_retire_share" in legend["columns"]:
        parts.append("tag_to_retire_share = tag-to-retire cycle share")
    else:
        parts.append(f"tag_to_retire_share absent "
                     f"({legend['absent']['tag_to_retire_share']})")
    if "function_ranges" in legend:
        for key in ("function_start", "function_end", "function_source", "function_derived",
                    "hot_rva", "hot_samples", "hot_sample_share", "function_reason"):
            if key in legend["columns"]:
                parts.append(f"{key} = {legend['columns'][key]}")
    if "region" in legend["columns"]:
        for key in ("region", "sampled_addresses", "hot_rva", "hot_samples", "hot_sample_share",
                    "function_source", "function_derived", "symbol_reason"):
            parts.append(f"{key} = {legend['columns'][key]}")
    if ("hot_rva" in legend["columns"] and "attribution" not in legend["columns"]
            and "region" not in legend["columns"]):
        for key in ("hot_rva", "hot_samples", "hot_sample_share"):
            parts.append(f"{key} = {legend['columns'][key]}")
    if "isSystemModule" in legend["columns"]:
        parts.append(f"isSystemModule = {legend['columns']['isSystemModule']}")
    elif "isSystemModule" in legend["absent"]:
        parts.append(f"isSystemModule absent ({legend['absent']['isSystemModule']})")
    if "cpu_seconds" in legend["columns"]:
        parts.append(f"cpu_seconds = {legend['columns']['cpu_seconds']}")
    elif "cpu_seconds" in legend["absent"]:
        parts.append(f"cpu_seconds absent ({legend['absent']['cpu_seconds']})")
    fields = [("COLUMNS", "share columns are fractions 0.0-1.0; " + "; ".join(parts)),
              ("READ", _read_text(legend, system_wide))]
    if legend["next"]:
        fields.append(("NEXT", _next_text(legend["next"])))
    return fields


def _header_fields(args: argparse.Namespace, sampler: uprof_session.Sampler,
                   dump: uprof_dump.DumpInfo | None, has_tag: bool,
                   tag_available: bool, system_wide: bool) -> list[tuple[str, str]]:
    """Detailed capture fields carried above the table and in the JSON document."""
    fields = [("VERSION", util.skill_identity()),
              ("METRIC", sampler.metric.removeprefix("METRIC: "))]
    if dump is None:
        if sampler.pmc_cycle_shares:
            share = ("sample_share is the busy core-cycle share (fixed cycles per sample; "
                     "halted excluded)")
        else:
            share = ("sample_share is not established for this PMC event; capture --event cycles "
                     "(CYCLES_NOT_IN_HALT, hwId 118) to make it a core-cycle share")
        fields.append(("DUMP", "none; this PMC capture has no IBS ASCII dump, so samples are the "
                               f"UnifiedSampleSeries event-column counts; {share}"))
        fields.append(("TIME_SHARE", "not applicable; this export aggregates PMC samples per "
                                     "window, so time_share is omitted; capture with --event "
                                     "ibs-op (IBS OP with per-sample timestamps) to get it"))
        fields.append(("TAG_TO_RETIRE_SHARE", "not applicable; this export has no tag-to-retire "
                                              "counter, so tag_to_retire_share is omitted"))
    else:
        fields.append(("DUMP", _dump_field(dump)))
        dump_dir = dump.path.parent
        session_full = util.absolute(args.session)
        if os.path.normcase(os.fspath(dump_dir)) != os.path.normcase(os.fspath(session_full)):
            fields.append(("DB", f"only the dump in {dump_dir} is valid; cpu.db must come from "
                                 f"the original session ({session_full / 'cpu.db'})"))
        window = ("the whole capture" if args.time_filter is None
                  else f"the time filter {args.time_filter}")
        if system_wide:
            fields.append(("TIME_SHARE", "share of time estimated from the per-sample DeltaTick "
                                         f"gaps within {window}; an estimate: idle/wait is "
                                         "charged to the ending sample, the first sample per CPU "
                                         "inside the window is ignored (no gap before it), and "
                                         "NULL sorts last; with few samples the share is coarse, "
                                         "so check sample_share"))
        else:
            cycle_basis = ("; for this cycle-controlled capture sample_share (fixed cycles per "
                           "sample) is the CPU-cycle share and the better ranking"
                           if _cycle_controlled(sampler) else "")
            fields.append(("TIME_SHARE", "share of elapsed thread time estimated from the "
                                         "per-sample DeltaTick gaps within "
                                         f"{window}; the partition is (PID, TID), so a thread that "
                                         "migrates between CPUs is counted once; an estimate: "
                                         "idle/wait, including off-CPU time, is charged to the "
                                         "ending sample, the first sample per thread inside the "
                                         "window is ignored (no gap before it), and NULL sorts "
                                         "last; with few samples the share is coarse, so check "
                                         f"sample_share{cycle_basis}"))
        if sampler.ibs_dump_kind == "ibsop-event-dump":
            if not has_tag:
                fields.append(("TAG_TO_RETIRE_SHARE", "not available for this dump: no "
                                                      "IbsTagToRetCtr column; sample_share and "
                                                      "time_share are unaffected"))
            elif tag_available:
                fields.append(("TAG_TO_RETIRE_SHARE", "share of tag-to-retire cycles summed "
                                                      "over sampled ops (AMD tag at dispatch to "
                                                      "retirement, queue/stalls included; 16-bit "
                                                      "counter clipped at 65535); a total, not "
                                                      "per-op latency: a higher share means more "
                                                      "total tag-to-retire cycles, not a higher "
                                                      "per-op cost"))
            else:
                fields.append(("TAG_TO_RETIRE_SHARE", "not available for this dump: IbsTagToRetCtr "
                                                      "has no nonzero values, so every "
                                                      "tag_to_retire_share is NULL; the "
                                                      "tag-to-retire counter comes from an IBS OP "
                                                      "capture (--event ibs-op), not from PMC or "
                                                      "IBS fetch; sample_share and time_share are "
                                                      "unaffected"))
        else:
            fields.append(("TAG_TO_RETIRE_SHARE", "not applicable; this export has no "
                                                  "tag-to-retire counter, so "
                                                  "tag_to_retire_share is omitted"))
    if dump is None:
        if sampler.pmc_cycle_shares:
            weights = ("PMC sample counts from UnifiedSampleSeries; sample_share is the busy "
                       "core-cycle share")
        else:
            weights = ("PMC sample counts from UnifiedSampleSeries; sample_share is not "
                       "established for this event")
    else:
        if system_wide:
            window = ("whole capture" if args.time_filter is None
                      else f"time filter {args.time_filter} (first sample per CPU inside the "
                           "window excluded)")
            weights = ("exact DeltaTick-gap weights per CPU_Number (the first sample per CPU is "
                       "ignored, no gap before it; gap time is charged to the later sample); "
                       f"{window}; sample_share is the dump's raw sample share and time_share "
                       "weights the same rows by gap")
        else:
            window = ("whole capture" if args.time_filter is None
                      else f"time filter {args.time_filter} (first sample per thread inside the "
                           "window excluded)")
            weights = ("exact DeltaTick-gap weights per (PID, TID) (the first sample per thread "
                       "is ignored, no gap before it; gap time, including off-CPU time, is charged "
                       "to the later sample); "
                       f"{window}; sample_share is the dump's raw sample share and time_share "
                       "weights the same rows by gap")
    fields.append(("WEIGHTS", f"{weights}; --by {args.by}: {GRANULARITY_LABELS[args.by]}"))
    return fields



def _dump_contexts(connection: object, gap_limit_ns: float | None
                   ) -> list[uprof_session.ContextCadence]:
    """Per-context sample counts and median gaps.

    The context is the weighting context of v_dump_context: a CPU for a system-wide capture and a
    (pid, tid) thread for an attached one, so same-TID threads of different processes stay apart.
    gap_limit_ns drops gaps above it, which are time the context was not running; None keeps every
    gap.
    """
    limit = "" if gap_limit_ns is None else " FILTER (WHERE gap <= ?)"
    parameters: list[float] = [] if gap_limit_ns is None else [gap_limit_ns]
    rows = connection.execute(
        "WITH gaps AS (SELECT context_pid, context, "
        "delta_tick - lag(delta_tick) OVER (PARTITION BY context_pid, context "
        "ORDER BY delta_tick) AS gap FROM v_dump_context) "
        f"SELECT context_pid, context, count(*), median(gap){limit} "
        "FROM gaps GROUP BY context_pid, context",
        parameters).fetchall()
    return [uprof_session.ContextCadence(int(row[2] or 0),
                                         None if row[3] is None else float(row[3]))
            for row in rows]


def _capture_scan(connection: object,
                  check_kernel: bool) -> tuple[int, int, bool | None]:
    """Whole-capture sample count, context count and kernel coverage from one scan.

    The kernel flag is only projected when asked for; the module join is then part of the same
    scan the totals need. None means the flag was not requested.
    """
    kernel = ("COALESCE(bool_or(kernel_image), false)" if check_kernel else "NULL")
    rows = connection.sql(
        "SELECT CAST(COALESCE(sum(samples), 0) AS BIGINT), count(DISTINCT context), "
        f"{kernel} FROM v_capture_modules").fetchall()
    if not rows:
        return 0, 0, None if not check_kernel else False
    samples = int(rows[0][0] or 0)
    contexts = int(rows[0][1] or 0)
    return samples, contexts, (None if not check_kernel else bool(rows[0][2]))


def _achieved_field(connection: object, has_dump: bool, args: argparse.Namespace,
                    sampler: uprof_session.Sampler, dump: uprof_dump.DumpInfo | None,
                    check_kernel: bool = False) -> tuple[tuple[str, str], int, bool | None]:
    """The achieved sampling rate over the whole capture, independent of the scope filter.

    The same scan returns the whole-capture sample count and, when check_kernel is set, whether
    any sample landed in a kernel image, so the kernel-coverage check adds no query of its own.
    """
    meta = uprof_session.read_meta(args.session) or {}
    schedule = (meta.get("collection") or {}).get("schedule") or {}
    elapsed_ms = schedule.get("elapsed")
    contexts: list[uprof_session.ContextCadence] = []
    label = "CPU"
    window_s: float | None = None
    samples, cpus, kernel_covered = _capture_scan(connection, check_kernel)
    if has_dump:
        wide = uprof_session.system_wide(args.session)
        label = "CPU" if wide else "thread"
        contexts = _dump_contexts(connection, uprof_session.run_gap_limit_ns(sampler))
        if dump is not None and dump.time_filter:
            bounds = util.parse_time_filter(dump.time_filter)
            if bounds is not None:
                window_s = float(bounds[1] - bounds[0])
    else:
        # PMC carries no per-sample timestamps, so a core's samples/s is its whole cadence.
        per_core = connection.sql(
            "SELECT context, CAST(COALESCE(sum(samples), 0) AS BIGINT) "
            "FROM v_capture_modules GROUP BY context").fetchall()
        contexts = [uprof_session.ContextCadence(int(row[1] or 0)) for row in per_core]
        label = "core"
    if window_s is None and elapsed_ms is not None:
        try:
            window_s = int(elapsed_ms) / 1000.0
        except (TypeError, ValueError):
            window_s = None
    field = ("ACHIEVED", uprof_session.achieved_summary(samples, window_s, cpus, sampler,
                                                        contexts, label))
    return field, samples, kernel_covered


def _scope_filter_active(args: argparse.Namespace, time_start: str, time_end: str) -> bool:
    """True when the report's population is narrower than the whole dump.

    The time window is taken from the same values bound to SQL, so an empty --time-filter or a
    defaulted sentinel end cannot make Python and the view disagree about whether it is filtered.
    """
    return ((args.image is not None and args.image != "%")
            or args.pid is not None
            or time_start != "0" or time_end != _ALL_NS)


def _dump_sanity(connection: object, has_dump: bool, scope_filtered: bool,
                 system_wide: bool) -> tuple[str | None, int]:
    """The sanity message and the dump's non-null IbsTagToRetCtr value count.

    v_scope_sanity is only read for a system-wide capture: an attached one yields no scoped
    verdict, and querying it would scan the dump for nothing.
    """
    if not has_dump:
        return None, 0
    rows = connection.sql("SELECT message, tag_values FROM v_dump_sanity").fetchall()
    if not rows:
        return None, 0
    message = rows[0][0] or None
    tag_values = int(rows[0][1] or 0)
    if scope_filtered and system_wide:
        scoped = connection.sql("SELECT message FROM v_scope_sanity").fetchall()
        if scoped and scoped[0][0]:
            message = f"{message}; {scoped[0][0]}" if message else scoped[0][0]
    return message, tag_values


def _kernel_coverage_field(sampler: uprof_session.Sampler, kernel_covered: bool | None,
                           windowed: bool = False,
                           captured_samples: int | None = None) -> list[tuple[str, str]]:
    """Whole-capture kernel coverage, checked only when the applied sampler requested OS mode.

    kernel_covered comes from the ACHIEVED scan and None means it was not determined. windowed
    suppresses the field: the resolved dump may have been produced by the session's recorded
    translate --time-filter, so it need not hold the whole capture. captured_samples is the
    capture total that same scan counted.
    """
    if not sampler.os_requested or windowed or kernel_covered is None:
        return []
    warning = uprof_session.kernel_coverage_warning(
        sampler, kernel_covered, captured_samples)
    return [] if warning is None else [("KERNEL_COVERAGE", warning)]


def _coverage_values(connection: object) -> dict[str, Any] | None:
    """The uProf DB channel: aggregate and per-module named sample counts from coverage.sql."""
    columns, rows = uprof_db.fetch(connection, TOOLS_DIR / "coverage.sql")
    if not rows:
        return None
    values = [dict(zip(columns, row)) for row in rows]
    total = int(values[0]["scoped_samples"])
    if total == 0:
        return None
    return {"rows": values, "total": total, "named": int(values[0]["named_total"])}


def _symbol_path_flags(args: argparse.Namespace) -> list[str]:
    """The explicit symbol-path flags passed to profile, in resolution order and deduplicated."""
    return [flag for attribute, flag in _SYMBOL_PATH_FLAGS
            if getattr(args, attribute, None)]


def _symbol_coverage_legend(coverage: dict[str, Any],
                            symbolizer: dict[str, Any] | None,
                            symbolized: bool,
                            flags: list[str],
                            by: str | None = None,
                            row_attribution: dict[str, int] | None = None) -> dict[str, Any]:
    """The JSON form of SYMBOL_COVERAGE: each channel separately, its counts and the flags used.

    The profile rows come from the symbolizer channel; the uProf DB channel is the Function.name
    uProf recorded at translate time and is not affected by profile's symbol-path flags. For
    function and region the rows also carry derived ranges, which are attributed but not named by
    the symbolizer, so the row split is reported instead of a 0% symbolizer share.
    """
    entry: dict[str, Any] = {
        "uprof_db": {
            "source": "cpu.db Function.name (uProf symbolization at translate)",
            "named_samples": coverage["named"],
            "scoped_samples": coverage["total"],
        },
        "flags": list(flags),
        "profile_symbolizer": None,
        "rows_drillable": None,
    }
    if by in RANGE_SCOPES and row_attribution is not None and row_attribution["scoped"] > 0:
        scoped = row_attribution["scoped"]
        entry["profile_symbolizer"] = {
            "source": "the emitted rows: llvm-symbolizer names plus derived PE ranges",
            "named_samples": row_attribution["named"],
            "derived_samples": row_attribution["derived"],
            "resolved_samples": row_attribution["named"] + row_attribution["derived"],
            "unresolved_samples": row_attribution["unresolved"],
            "scoped_samples": scoped,
        }
        entry["rows_drillable"] = row_attribution["unresolved"] == 0
        return entry
    if symbolized and symbolizer is not None:
        scoped = int(symbolizer["scoped_samples"])
        unresolved = int(symbolizer["unresolved_samples"])
        resolved = max(scoped - unresolved, 0)
        entry["profile_symbolizer"] = {
            "source": "llvm-symbolizer parquet (agreement.sql)",
            "resolved_samples": resolved,
            "unresolved_samples": unresolved,
            "scoped_samples": scoped,
        }
        entry["rows_drillable"] = scoped > 0 and resolved == scoped
    return entry


def _coverage_field(connection: object,
                    causes: dict[str, str] | None = None,
                    symbolizer: dict[str, Any] | None = None,
                    symbolized: bool = False,
                    flags: list[str] | None = None,
                    coverage: dict[str, Any] | None = None,
                    by: str | None = None,
                    row_attribution: dict[str, int] | None = None) -> list[tuple[str, str]]:
    """Disclose how many scoped samples resolve to a named function, and per module.

    Two channels exist. coverage.sql reads cpu.db Function.name, which uProf wrote at translate
    time and which profile's symbol-path flags do not change. agreement.sql reads the parquet
    llvm-symbolizer produced for line/instruction rows, counting a sample resolved when the
    symbolizer named its address. When the rows are symbolized and the channels disagree, or when
    a symbol path was passed explicitly, both are named so a resolved table is never paired with
    a "cannot be drilled" stop line. causes carries the per-module symbolization outcome.

    For function and region the rows also carry derived ranges, so the emitted row split (named /
    derived / unresolved) is quoted. Quoting the symbolizer's unresolved count there would call
    every derived row unattributed even though the row names its range and hot RVA.
    """
    coverage = coverage if coverage is not None else _coverage_values(connection)
    if coverage is None:
        return []
    values = coverage["rows"]
    total = coverage["total"]
    named = coverage["named"]
    db_text = f"{named}/{total} samples ({100.0 * named / total:.1f}%) resolve to a named function"
    flags = flags or []
    flag_text = f" (flags: {', '.join(flags)})" if flags else ""
    if by in RANGE_SCOPES and row_attribution is not None and row_attribution["scoped"] > 0:
        scoped = row_attribution["scoped"]
        named_rows = row_attribution["named"]
        derived = row_attribution["derived"]
        unresolved = row_attribution["unresolved"]

        def percent(samples: int) -> str:
            return f"{100.0 * samples / scoped:.1f}%"

        text = (f"uProf DB {db_text}; rows named {named_rows}/{scoped} samples "
                f"({percent(named_rows)}), derived {derived}/{scoped} samples "
                f"({percent(derived)}) from a synthetic binary range, unresolved "
                f"{unresolved}/{scoped} samples ({percent(unresolved)}) (<no symbol>){flag_text}")
        if unresolved == 0:
            return [("SYMBOL_COVERAGE",
                     f"{text}; every scoped sample is attributed below module/RVA; a derived row "
                     "is actionable at its hot RVA")]
        reasons = [f"{module}: {cause}" for module, cause in sorted((causes or {}).items())]
        if reasons:
            text += f"; cause by module: {'; '.join(reasons)}"
        return [("SYMBOL_COVERAGE", f"{text}; {_RANGE_UNRESOLVED_STOP}{_SYMBOL_PATH_REMEDY}")]
    scoped = int(symbolizer["scoped_samples"]) if symbolized and symbolizer else 0
    unresolved_samples = int(symbolizer["unresolved_samples"]) if scoped else 0
    resolved = max(scoped - unresolved_samples, 0)
    both = scoped > 0 and (bool(flags) or resolved != named)
    if both:
        text = (f"uProf DB {db_text}; profile symbolizer {resolved}/{scoped} samples "
                f"({100.0 * resolved / scoped:.1f}%) resolve below module/RVA"
                + (f" (flags: {', '.join(flags)})" if flags else ""))
        if resolved == scoped:
            return [("SYMBOL_COVERAGE",
                     f"{text}; every scoped sample is drillable below module/RVA; the uProf DB "
                     "share reflects only what translate recorded, and profile's symbol paths "
                     "affect the rows, not that share")]
        reasons = [f"{module}: {cause}" for module, cause in sorted((causes or {}).items())]
        if reasons:
            text += f"; cause by module: {'; '.join(reasons)}"
        stop = _NO_NAMED_FUNCTION_STOP if resolved == 0 else _UNRESOLVED_FUNCTION_STOP
        return [("SYMBOL_COVERAGE", f"{text}; {stop}{_SYMBOL_PATH_REMEDY}")]
    unresolved = [value for value in values
                  if int(value["samples"]) > int(value["named_samples"])]
    if not unresolved:
        return [("SYMBOL_COVERAGE", f"{db_text}; no module has unresolved samples")]
    parts = []
    for value in unresolved:
        samples = int(value["samples"])
        named_samples = int(value["named_samples"])
        kind = "partial" if named_samples else "no source names"
        share = 100.0 * (samples - named_samples) / samples
        cause = (causes or {}).get(value["module_name"])
        reason = f"; cause: {cause}" if cause else ""
        parts.append(f"{value['module_name']} {samples - named_samples}/{samples} "
                     f"({share:.1f}% unresolved, {kind}{reason})")
    # A module with no names at all is the case that provokes a manual PE/.pdata mapping.
    stop = (_NO_NAMED_FUNCTION_STOP
            if any(int(value["named_samples"]) == 0 for value in unresolved)
            else _UNRESOLVED_FUNCTION_STOP)
    remedy = _SYMBOL_PATH_REMEDY if causes else ""
    return [("SYMBOL_COVERAGE",
             f"{db_text}; unresolved by module: {'; '.join(parts)}; {stop}{remedy}")]


def _tag_nonzero(connection: object) -> int:
    """Samples whose tag-to-retire counter is nonzero, over the whole dump.

    The column can be present with every cell zero; then every tag share is NULL rather than
    zero, so the profile says the counter carried no nonzero value.
    """
    rows = connection.sql(
        "SELECT count(*) FILTER (WHERE tag_cycles > 0) FROM v_dump").fetchall()
    return int(rows[0][0] or 0) if rows else 0


def _scope_warning(connection: object, image_pattern: str | None = None) -> list[tuple[str, str]]:
    """Say that the scope matched nothing, and for --image name what the filter selects."""
    fields: list[tuple[str, str]] = []
    empty = connection.sql("SELECT total_samples FROM v_scope_totals").fetchall()
    if empty and empty[0][0] == 0:
        message = "no samples in the selected scope; the process or time filter matched nothing"
        if image_pattern not in (None, "%"):
            message = ("no samples in the selected scope; --image selects process base images "
                       "(Process.name), not modules or module paths")
            names = [row[0] for row in connection.execute(
                "SELECT DISTINCT name FROM Process WHERE COALESCE(name, '') <> '' "
                "ORDER BY name").fetchall()]
            if names:
                shown = ", ".join(names[:10]) + (" ..." if len(names) > 10 else "")
                message += f"; available images in this capture: {shown}"
            else:
                message += "; this capture records no process image names"
        fields.append(("WARNING", message))
    return fields


def _agreement_values(connection: object) -> dict[str, Any] | None:
    columns, rows = uprof_db.fetch(connection, TOOLS_DIR / "agreement.sql")
    return dict(zip(columns, rows[0])) if rows else None


def _row_attribution(connection: object, by: str) -> dict[str, int]:
    """Sample weight by the row attribution for --by function/region.

    The row views are the authority: a derived range is an attribution, not a missing symbol, so
    the symbolizer-only unresolved counts must not be quoted for these scopes. Query the view with
    the cap removed so the split covers the whole scope; restore the cap before the projection.
    """
    view = "v_function_rows" if by == "function" else "v_region_rows"
    previous = connection.execute("SELECT getvariable('top_n')").fetchall()
    previous = previous[0][0] if previous else 0
    try:
        connection.execute("SET VARIABLE top_n = ?", ["0"])
        rows = connection.execute(
            f"SELECT attribution, CAST(sum(samples) AS BIGINT) FROM {view} "
            "GROUP BY attribution").fetchall()
    finally:
        connection.execute("SET VARIABLE top_n = ?", [str(previous)])
    counts = {"named": 0, "derived": 0, "unresolved": 0}
    for attribution, samples in rows:
        value = int(samples or 0)
        if attribution in ("unique", "shared"):
            counts["named"] += value
        elif attribution == "derived":
            counts["derived"] += value
        else:
            counts["unresolved"] += value
    counts["scoped"] = sum(counts.values())
    return counts


def _hot_rva_field(connection: object) -> list[tuple[str, str]]:
    """The highest-sample address across the function rows, independent of the row order.

    A function row can be low in the table while one address inside it is the hottest sample in
    the scope, so the summary is the maximum hot_samples over the rows, not the top row's hot_rva.
    Query the view uncapped so --top cannot hide it; restore the cap before the projection.
    """
    previous = connection.execute("SELECT getvariable('top_n')").fetchall()
    previous = previous[0][0] if previous else 0
    try:
        connection.execute("SET VARIABLE top_n = ?", ["0"])
        rows = connection.execute(
            "SELECT function, module, hot_rva, hot_samples, total_samples FROM v_function_rows "
            "WHERE hot_samples IS NOT NULL ORDER BY hot_samples DESC, hot_rva LIMIT 1"
        ).fetchall()
    finally:
        connection.execute("SET VARIABLE top_n = ?", [str(previous)])
    if not rows:
        return []
    function, module, hot_rva, hot_samples, total_samples = rows[0]
    total = int(total_samples or 0)
    share = f"{100.0 * int(hot_samples or 0) / total:.1f}%" if total else "an absent share"
    return [("HOT_RVA", (f"{module or '<unknown module>'} {function or '<unresolved>'} at "
                         f"{hot_rva} with {int(hot_samples or 0)} samples ({share} of the scoped "
                         "capture); the highest-sample address across the function rows, not "
                         "the top function row's hot_rva"))]


def _agreement_text(connection: object,
                    values: dict[str, Any] | None = None,
                    by: str | None = None,
                    row_attribution: dict[str, int] | None = None) -> str | None:
    values = values if values is not None else _agreement_values(connection)
    if by in RANGE_SCOPES and row_attribution is not None and row_attribution["scoped"] > 0:
        scoped = row_attribution["scoped"]
        text = (f"sample attribution from the emitted rows: named {row_attribution['named']} "
                f"samples (unique or shared; uProf Function.name or a named binary range), "
                f"derived {row_attribution['derived']} samples (synthetic binary range), "
                f"unresolved {row_attribution['unresolved']} samples (<no symbol>), of "
                f"{scoped} scoped"
                + ("; region rows count each page's samples under its hot-RVA attribution"
                   if by == "region" else ""))
        if values is None:
            return text
        return text + ("; symbolizer names are the containing function (llvm-symbolizer's "
                       "outermost frame), the name uProf Function.id carries, so inline "
                       "instances are not counted as mismatches; "
                       f"mismatch_rate={_percent_text(values, 'mismatch_rate')} (sample-weighted "
                       f"mismatched_samples/compared_samples = {values['mismatched_samples']}/"
                       f"{values['compared_samples']}); name_mismatches={values['name_mismatches']} "
                       f"addresses of {values['compared_addresses']} compared")
    if values is None:
        return None

    return ("line/instruction coordinates are the innermost (executed) frame; symbolizer names are "
            "the containing function (llvm-symbolizer's outermost frame), the name uProf "
            "Function.id carries, so inline instances are not counted as mismatches; "
            f"mismatch_rate={_percent_text(values, 'mismatch_rate')} (sample-weighted "
            f"mismatched_samples/compared_samples = {values['mismatched_samples']}/"
            f"{values['compared_samples']}); name_mismatches={values['name_mismatches']} "
            f"addresses of {values['compared_addresses']} compared; unresolved="
            f"{values['unresolved']} addresses, {values['unresolved_samples']} samples "
            f"(<no symbol>, {_percent_text(values, 'unresolved_pct')} of "
            f"{values['scoped_samples']} scoped)")


def _percent_text(values: dict[str, Any], key: str) -> str:
    return "n/a (no denominator)" if values[key] is None else f"{values[key]}%"


def _write_profile(connection: object, args: argparse.Namespace,
                   fields: list[tuple[str, str]], out_path: Path | None,
                   legend: dict[str, object] | None = None) -> None:
    """Render the profile; instruction JSON fetches its SELECT once and joins contexts to it."""
    if args.json and args.by == "instruction":
        columns, types, rows = uprof_db.fetch_typed(
            connection, TOOLS_DIR / "profile-instruction.sql")
        contexts = uprof_context.instruction_contexts(
            connection, columns, rows,
            debug_dirs=getattr(args, "debug_file_dir", None) or (),
            fallback_paths=getattr(args, "fallback_debug_path", None) or ())
        help_text = uprof_context.context_help(legend or {})
        summary = uprof_context.context_omission_summary(contexts)
        if summary:
            print(f"CONTEXT: {summary}", file=sys.stderr)
        uprof_db.output_rows(columns, types, rows, fields, args.json, out_path,
                             legend, contexts, help_text)
        return
    uprof_db.output(connection, TOOLS_DIR / f"profile-{args.by}.sql", fields, args.json,
                    out_path, legend)


def run_query_command(args: argparse.Namespace) -> None:
    _check_args(args)
    session = util.require_dir(args.session, "capture directory")
    cpu_db = session / "cpu.db"
    if not cpu_db.is_file():
        raise util.UprofError(f"cpu.db not found: {cpu_db} (run translate --session "
                              f"'{session}' --out-dir <COPY_DIR> first, then query the original "
                              "session)")
    out_path = _out_path(args, cpu_db, session)

    if args.command == "schema":
        with uprof_db.open_query(cpu_db, [], {}) as connection:
            uprof_db.output(connection, TOOLS_DIR / "schema.sql", [], False, out_path)
        return

    top = _top_value(args.top)
    time_start, time_end = _time_range(args)
    sampler = uprof_session.derive_sampler(session)
    system_wide = uprof_session.system_wide(session)
    if sampler.pmc:
        if args.time_filter:
            raise util.UprofError(
                "--time-filter cannot be applied to a PMC capture: this export aggregates PMC "
                "samples per window and carries no "
                "per-sample timestamps, so the window cannot be selected; capture with "
                "--event ibs-op to use --time-filter")
        dump = None
    else:
        dump = uprof_dump.resolve_dump(session, sampler)
    if (out_path and dump is not None
            and os.path.normcase(os.fspath(dump.path)) == os.path.normcase(
                os.fspath(out_path))):
        raise util.UprofError(f"--out-file would overwrite the IBS dump {dump.path}")
    # The resolved dump may have been produced by the recorded translate window, so the
    # whole-capture claim cannot be made from it.
    dump_windowed = dump is not None and uprof_dump.recorded_time_filter(session) is not None

    stream = sys.stderr
    primary_measure, _ = _primary_measure(sampler, dump)
    sample_interval_s = (_cpu_seconds_interval(sampler)
                         if args.by in ("module", "region", "function") else None)
    variables = _variables(args, dump, top, time_start, time_end, sampler.event_column,
                           sample_interval_s, primary_measure)
    artifacts: Path | None = None
    success = False
    try:
        with uprof_db.open_query(cpu_db, [TOOLS_DIR / "views.sql"], variables) as connection:
            causes: dict[str, str] = {}
            symbolizer: dict[str, Any] | None = None
            symbolized = False
            if args.by in SYMBOLIZED:
                try:
                    artifacts = Path(tempfile.mkdtemp(prefix="uprof-symbols-"))
                except OSError as exc:
                    raise util.UprofError(
                        f"cannot create the symbol artifact directory in "
                        f"{tempfile.gettempdir()}: {exc}; check free space and permissions on "
                        "the temp directory") from exc
                print(f"ARTIFACTS={artifacts}", file=stream)
                symbols, causes = _symbolize_scope(connection, artifacts, stream, args)
                variables.update(symbols)
                uprof_db.set_variables(connection, symbols)
                symbolized = bool(symbols.get("uprof_symbols"))
                symbolizer = _agreement_values(connection)

            range_info: dict[str, Any] | None = None
            row_attribution: dict[str, int] | None = None
            if args.by in RANGE_SCOPES:
                functions_path, range_info = _derive_scope_ranges(connection, artifacts, stream)
                if functions_path is not None:
                    variables["uprof_functions"] = str(functions_path)
                    uprof_db.set_variables(connection,
                                           {"uprof_functions": str(functions_path)})
                attempted = chr(2).join(range_info.get("attempted", []))
                variables["uprof_range_attempted"] = attempted
                uprof_db.set_variables(connection, {"uprof_range_attempted": attempted})
                row_attribution = _row_attribution(connection, args.by)

            sanity_message, tag_values = _dump_sanity(
                connection, dump is not None,
                _scope_filter_active(args, time_start, time_end), system_wide)
            tag_rows = connection.sql("SELECT getvariable('has_tag')").fetchall()
            has_tag = bool(tag_rows and tag_rows[0][0])
            module_rows = connection.sql("SELECT getvariable('has_system_module')").fetchall()
            has_system_module = bool(module_rows and module_rows[0][0])
            tag_available = (sampler.ibs_dump_kind == "ibsop-event-dump" and has_tag
                             and tag_values > 0
                             and _tag_nonzero(connection) > 0)
            uprof_db.set_variables(connection, {
                "uprof_tag_available": "true" if tag_available else "false"})
            legend = _legend(sampler, dump, has_tag, tag_available, args, system_wide,
                             sample_interval_s, has_system_module, range_info, row_attribution)
            fields = _header_fields(args, sampler, dump, has_tag, tag_available, system_wide)
            # The legend sits directly below METRIC, after VERSION, before the detailed fields.
            fields[2:2] = _legend_fields(legend, system_wide)
            if sanity_message:
                fields.append(("SANITY", sanity_message))
            check_kernel = sampler.os_requested and not dump_windowed
            achieved, captured_samples, kernel_covered = _achieved_field(
                connection, dump is not None, args, sampler, dump, check_kernel)
            if achieved:
                fields.append(achieved)
            fields.extend(_kernel_coverage_field(
                sampler, kernel_covered, dump_windowed, captured_samples))
            if args.by == "function":
                fields.extend(_hot_rva_field(connection))
            coverage = _coverage_values(connection)
            flags = _symbol_path_flags(args)
            fields.extend(_coverage_field(
                connection, causes, symbolizer, symbolized, flags, coverage,
                by=args.by, row_attribution=row_attribution))
            if coverage is not None:
                legend["symbol_coverage"] = _symbol_coverage_legend(
                    coverage, symbolizer, symbolized, flags,
                    by=args.by, row_attribution=row_attribution)
            fields.extend(_scope_warning(connection, args.image))
            if args.by in SYMBOLIZED:
                agreement = _agreement_text(
                    connection, symbolizer, by=args.by, row_attribution=row_attribution)
                if agreement:
                    fields.append(("AGREEMENT", agreement))
            _write_profile(connection, args, fields, out_path, legend)
        success = True
    finally:
        if artifacts is not None:
            if not success:
                print(f"ARTIFACTS_KEPT={artifacts} (the run failed; kept for inspection)",
                      file=stream)
            else:
                try:
                    shutil.rmtree(artifacts)
                except OSError as exc:
                    reason = str(exc)
                else:
                    reason = None
                if artifacts.exists():
                    print(f"ARTIFACTS_KEPT={artifacts} "
                          f"(cleanup failed: {reason or 'the directory still exists'})",
                          file=stream)
                else:
                    print(f"ARTIFACTS_CLEANED={artifacts}", file=stream)
