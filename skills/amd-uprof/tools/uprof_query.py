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
import uprof_dump
import uprof_session
import uprof_symbols
import uprof_util as util

TOOLS_DIR: Final = Path(os.path.abspath(__file__)).parent
GRANULARITY_LABELS: Final = MappingProxyType({
    "process": "per Process.id",
    "function": "per Function.id",
    "line": "per file:line",
    "instruction": "per module offset",
})
SYMBOLIZED: Final = ("line", "instruction")
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
               time_start: str, time_end: str, event_column: str | None) -> dict[str, str]:
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
    }


def _symbolize_scope(connection: object, artifacts: Path,
                     stream: TextIO) -> dict[str, str]:
    """Symbolize every module in scope into artifacts; return the uprof_symbols* variables."""
    _, rows = uprof_db.fetch(connection, TOOLS_DIR / "profile-rvas.sql")
    modules: dict[str, list] = {}
    for module_name, module_path, rva in rows:
        if module_name not in modules:
            modules[module_name] = [module_path, []]
        modules[module_name][1].append(rva)
    symbols, publics = uprof_symbols.symbolize_modules(
        [(name, path, rvas) for name, (path, rvas) in modules.items()], artifacts, stream=stream)
    return {
        "uprof_symbols": str(symbols) if symbols else "",
        "uprof_aliases": str(publics) if publics else "",
    }


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
_TAG_SHARE_MEANING: Final = (
    "share of tag-to-retire cycles summed over sampled ops (AMD tag at dispatch to retirement, "
    "queue/stalls included; clipped at 131070); a total, not per-op latency")
# Only for frequency shares: those count sampled operations, not fixed work per sample.
_SAMPLE_CAVEAT: Final = ("a raw share cannot separate a cheap operation run often from an "
                         "expensive one run rarely")
_NEXT_BY: Final = {
    "process": ("function", "line", "instruction"),
    "function": ("line", "instruction"),
    "line": ("instruction",),
    "instruction": (),
}


def _sample_share_meaning(sampler: uprof_session.Sampler,
                          dump: uprof_dump.DumpInfo | None) -> str:
    """What sample_share is a fraction of in this capture mode."""
    if dump is None:
        if sampler.pmc_cycle_shares:
            return "busy core-cycle share (fixed cycles per sample; halted excluded)"
        return "not established (this PMC event is not the supported cycle event)"
    if sampler.ibs_dump_kind == "ibsfetch-event-dump":
        return "fetch-sample frequency share (fixed fetch events per sample)"
    if sampler.interval_units == "ops":
        return "dispatched-op frequency share (fixed dispatched ops per sample)"
    if sampler.ibs_dump_kind == "ibsop-event-dump":
        return "core-cycle share (fixed cycles per sample), not wall time"
    return "not established (the session records no usable sampler)"


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
    if args.top != "20":
        parts += ["--top", str(args.top)]
    if json_mode:
        parts.append("--json")
    return " ".join(parts)


def _next_calls(args: argparse.Namespace) -> list[str]:
    """Drill-down calls from the current --by; the last one is the JSON document."""
    calls = [_profile_command(args, by) for by in _NEXT_BY[args.by]]
    calls.append(_profile_command(args, "instruction", json_mode=True))
    return calls


def _legend(sampler: uprof_session.Sampler, dump: uprof_dump.DumpInfo | None,
            has_tag: bool, tag_available: bool,
            args: argparse.Namespace) -> dict[str, Any]:
    """Machine-readable meaning of every share column, the ordering estimate and next calls.

    sample_share's meaning is mode-specific; time_share is a time share estimated from gaps, not
    unqualified impact; tag_to_retire_share is a summed total, not per-op latency. A PMC capture
    has no DeltaTick gaps, so its rows fall back to the raw sample count.
    """
    columns: dict[str, str] = {
        "samples": "raw sample count",
        "sample_share": _sample_share_meaning(sampler, dump),
    }
    if args.by != "process":
        columns["attribution"] = "unique / shared (folded names) / unresolved (no symbol)"
    denominators: dict[str, str] = {"sample_share": "total_samples"}
    absent: dict[str, str] = {}
    if dump is None:
        absent["time_share"] = "this export aggregates PMC samples per window"
        absent["tag_to_retire_share"] = "this export has no tag-to-retire counter"
    else:
        columns["time_share"] = _TIME_SHARE_MEANING
        denominators["time_share"] = "total_weight"
        if tag_available:
            columns["tag_to_retire_share"] = _TAG_SHARE_MEANING
            denominators["tag_to_retire_share"] = "total_tag_cycles"
        elif sampler.ibs_dump_kind == "ibsop-event-dump" and not has_tag:
            absent["tag_to_retire_share"] = "this dump has no IbsTagToRetCtr column"
        elif sampler.ibs_dump_kind == "ibsop-event-dump":
            absent["tag_to_retire_share"] = "IbsTagToRetCtr has no non-null values in this dump"
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
        estimate = ("time_share is a time share estimated from the gaps between per-sample "
                    "timestamps; idle/wait is charged to the ending sample, the first sample per "
                    "CPU is ignored (no gap before it), and NULL sorts last")
    return {
        "share_unit": "fraction",
        "share_scale": 1,
        "denominators": denominators,
        "columns": columns,
        "absent": absent,
        "impact": {"order_by": order_by, "gap_weighted": gap_weighted, "estimate": estimate},
        "remainder_last": True,
        "next": _next_calls(args),
    }


def _read_text(legend: dict[str, Any]) -> str:
    """The positive reading rule, plus the ranking and remainder rule."""
    sample = legend["columns"]["sample_share"]
    if legend["impact"]["gap_weighted"]:
        read = "time_share ranks where time went; sample_share is not a per-op cost"
        if "tag_to_retire_share" in legend["columns"]:
            read += "; tag_to_retire_share is total tag-to-retire cycles (higher = more wait)"
        if sample.startswith(("dispatched-op", "fetch-sample")):
            read += f"; {_SAMPLE_CAVEAT}"
    elif sample.startswith("not established"):
        read = ("sample_share is not established for this event; rows are ordered by raw sample "
                "count only, not a time or impact claim")
    else:
        read = ("sample_share is the busy core-cycle share, so the sample order ranks where "
                "cycles were spent")
    read += "; the <other ...> row is last, not ranked"
    return read


def _next_text(calls: list[str]) -> str:
    """Full runnable calls joined by && so the Markdown line can be pasted as one command."""
    return " && ".join(calls)


def _legend_fields(legend: dict[str, Any]) -> list[tuple[str, str]]:
    """The short legend: columns and unit, what to read, and runnable next calls."""
    parts = []
    if "attribution" in legend["columns"]:
        parts.append(f"attribution = {legend['columns']['attribution']}")
    parts += ["samples = raw count", f"sample_share = {legend['columns']['sample_share']}"]
    if "time_share" in legend["columns"]:
        parts.append("time_share = time share")
    else:
        parts.append(f"time_share absent ({legend['absent']['time_share']})")
    if "tag_to_retire_share" in legend["columns"]:
        parts.append("tag_to_retire_share = tag-to-retire cycle share")
    else:
        parts.append(f"tag_to_retire_share absent "
                     f"({legend['absent']['tag_to_retire_share']})")
    return [("COLUMNS", "share columns are fractions 0.0-1.0; " + "; ".join(parts)),
            ("READ", _read_text(legend)),
            ("NEXT", _next_text(legend["next"]))]


def _header_fields(args: argparse.Namespace, sampler: uprof_session.Sampler,
                   dump: uprof_dump.DumpInfo | None, has_tag: bool,
                   tag_available: bool) -> list[tuple[str, str]]:
    """Detailed capture fields carried above the table and in the JSON document."""
    fields = [("METRIC", sampler.metric.removeprefix("METRIC: "))]
    if dump is None:
        if sampler.pmc_cycle_shares:
            share = ("sample_share is the busy core-cycle share (fixed cycles per sample; "
                     "halted excluded)")
        else:
            share = "sample_share is not established for this PMC event"
        fields.append(("DUMP", "none; this PMC capture has no IBS ASCII dump, so samples are the "
                               f"UnifiedSampleSeries event-column counts; {share}"))
        fields.append(("TIME_SHARE", "not applicable; this export aggregates PMC samples per "
                                     "window, so time_share is omitted"))
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
        fields.append(("TIME_SHARE", "share of time estimated from the per-sample DeltaTick "
                                     f"gaps within {window}; an estimate: idle/wait is charged "
                                     "to the ending sample, the first sample per CPU inside the "
                                     "window is ignored (no gap before it), and NULL sorts last; "
                                     "with few samples the share is coarse, so check "
                                     "sample_share"))
        if sampler.ibs_dump_kind == "ibsop-event-dump":
            if not has_tag:
                fields.append(("TAG_TO_RETIRE_SHARE", "not available for this dump: no "
                                                      "IbsTagToRetCtr column; sample_share and "
                                                      "time_share are unaffected"))
            elif tag_available:
                fields.append(("TAG_TO_RETIRE_SHARE", "share of tag-to-retire cycles summed "
                                                      "over sampled ops (AMD tag at dispatch to "
                                                      "retirement, queue/stalls included; "
                                                      "clipped at 131070); a total, not "
                                                      "per-op latency: a higher share means more "
                                                      "total tag-to-retire cycles, not a higher "
                                                      "per-op cost"))
            else:
                fields.append(("TAG_TO_RETIRE_SHARE", "not available for this dump: IbsTagToRetCtr "
                                                      "has no non-null values; sample_share and "
                                                      "time_share are unaffected"))
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
        window = ("whole capture" if args.time_filter is None
                  else f"time filter {args.time_filter} (first sample per CPU inside the window "
                       "excluded)")
        weights = ("exact DeltaTick-gap weights per CPU_Number (the first sample per CPU is "
                   "ignored, no gap before it; gap time is charged to the later sample); "
                   f"{window}; sample_share is the dump's raw sample share and time_share "
                   "weights the same rows by gap")
    fields.append(("WEIGHTS", f"{weights}; --by {args.by}: {GRANULARITY_LABELS[args.by]}"))
    return fields



def _dump_contexts(connection: object, key: str, gap_limit_ns: float | None
                   ) -> list[uprof_session.ContextCadence]:
    """Per-context sample counts and median gaps from v_dump.

    key is cpu for a system-wide capture and tid for an attached one. gap_limit_ns drops gaps
    above it, which are time the context was not running; None keeps every gap.
    """
    limit = "" if gap_limit_ns is None else " FILTER (WHERE gap <= ?)"
    parameters: list[float] = [] if gap_limit_ns is None else [gap_limit_ns]
    rows = connection.execute(
        f"WITH gaps AS (SELECT {key} AS context, "
        f"delta_tick - lag(delta_tick) OVER (PARTITION BY {key} ORDER BY delta_tick) AS gap "
        "FROM v_dump) "
        f"SELECT context, count(*), median(gap){limit} FROM gaps GROUP BY context",
        parameters).fetchall()
    return [uprof_session.ContextCadence(int(row[1] or 0),
                                         None if row[2] is None else float(row[2]))
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
        contexts = _dump_contexts(connection, "cpu" if wide else "tid",
                                  uprof_session.run_gap_limit_ns(sampler))
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


def _scope_warning(connection: object) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    empty = connection.sql("SELECT total_samples FROM v_scope_totals").fetchall()
    if empty and empty[0][0] == 0:
        fields.append(("WARNING", "no samples in the selected scope; the process or time filter "
                                  "matched nothing"))
    return fields


def _agreement_text(connection: object) -> str | None:
    columns, rows = uprof_db.fetch(connection, TOOLS_DIR / "agreement.sql")
    if not rows:
        return None
    values = dict(zip(columns, rows[0]))

    def percent(key: str) -> str:
        return "n/a" if values[key] is None else f"{values[key]}%"

    return ("line/instruction coordinates are the innermost (executed) frame; symbolizer names are "
            "the containing function (llvm-symbolizer's outermost frame), the name uProf "
            "Function.id carries, so inline instances are not counted as mismatches; "
            f"mismatch_rate={percent('mismatch_rate')} (sample-weighted "
            f"mismatched_samples/compared_samples = {values['mismatched_samples']}/"
            f"{values['compared_samples']}); name_mismatches={values['name_mismatches']} "
            f"addresses of {values['compared_addresses']} compared; unresolved="
            f"{values['unresolved']} addresses, {values['unresolved_samples']} samples "
            f"(<no symbol>, {percent('unresolved_pct')} of {values['scoped_samples']} scoped)")


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
    variables = _variables(args, dump, top, time_start, time_end, sampler.event_column)
    artifacts: Path | None = None
    success = False
    try:
        with uprof_db.open_query(cpu_db, [TOOLS_DIR / "views.sql"], variables) as connection:
            if args.by in SYMBOLIZED:
                try:
                    artifacts = Path(tempfile.mkdtemp(prefix="uprof-symbols-"))
                except OSError as exc:
                    raise util.UprofError(
                        f"cannot create the symbol artifact directory in "
                        f"{tempfile.gettempdir()}: {exc}; check free space and permissions on "
                        "the temp directory") from exc
                print(f"ARTIFACTS={artifacts}", file=stream)
                symbols = _symbolize_scope(connection, artifacts, stream)
                variables.update(symbols)
                uprof_db.set_variables(connection, symbols)

            sanity_message, tag_values = _dump_sanity(
                connection, dump is not None,
                _scope_filter_active(args, time_start, time_end), system_wide)
            tag_rows = connection.sql("SELECT getvariable('has_tag')").fetchall()
            has_tag = bool(tag_rows and tag_rows[0][0])
            tag_available = (sampler.ibs_dump_kind == "ibsop-event-dump" and has_tag
                             and tag_values > 0)
            uprof_db.set_variables(connection, {
                "uprof_tag_available": "true" if tag_available else "false"})
            legend = _legend(sampler, dump, has_tag, tag_available, args)
            fields = _header_fields(args, sampler, dump, has_tag, tag_available)
            # The legend sits directly below METRIC, before the detailed capture fields.
            fields[1:1] = _legend_fields(legend)
            if sanity_message:
                fields.append(("SANITY", sanity_message))
            check_kernel = sampler.os_requested and not dump_windowed
            achieved, captured_samples, kernel_covered = _achieved_field(
                connection, dump is not None, args, sampler, dump, check_kernel)
            if achieved:
                fields.append(achieved)
            fields.extend(_kernel_coverage_field(
                sampler, kernel_covered, dump_windowed, captured_samples))
            fields.extend(_scope_warning(connection))
            if args.by in SYMBOLIZED:
                agreement = _agreement_text(connection)
                if agreement:
                    fields.append(("AGREEMENT", agreement))
            uprof_db.output(connection, TOOLS_DIR / f"profile-{args.by}.sql", fields,
                            args.json, out_path, legend)
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
