"""Profile orchestration: validate the arguments, resolve the dump and symbols, emit the result.

All set, join, aggregation, top-N and percentage work lives in the SQL files. Python owns argument
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
from typing import Final, TextIO

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


def _header_fields(args: argparse.Namespace, sampler: uprof_session.Sampler,
                   dump: uprof_dump.DumpInfo | None, has_tag: bool,
                   tag_available: bool) -> list[tuple[str, str]]:
    """Header lines carried above the table and as named fields in the JSON document."""
    fields = [("METRIC", sampler.metric.removeprefix("METRIC: "))]
    if dump is None:
        fields.append(("DUMP", "none; this PMC capture has no IBS ASCII dump, so samples are the "
                               "UnifiedSampleSeries event-column counts (fixed cycles per sample)"))
        fields.append(("PCT_TIME", "not applicable; PMC samples carry no per-sample timestamps, so "
                                   "pct_time is omitted; pct_samples is the CPU-time share"))
        fields.append(("PCT_CYCLES", "not applicable; PMC samples carry no tag-to-retire counter, "
                                     "so pct_cycles is omitted"))
    else:
        fields.append(("DUMP", _dump_field(dump)))
        dump_dir = dump.path.parent
        session_full = util.absolute(args.session)
        if os.path.normcase(os.fspath(dump_dir)) != os.path.normcase(os.fspath(session_full)):
            fields.append(("DB", f"only the dump in {dump_dir} is valid; cpu.db must come from "
                                 f"the original session ({session_full / 'cpu.db'})"))
        window = ("the whole capture" if args.time_filter is None
                  else f"the time filter {args.time_filter}")
        fields.append(("PCT_TIME", "share of elapsed CPU time from per-sample DeltaTick gaps "
                                   f"within {window}; the first sample per CPU inside the window "
                                   "carries no weight; a gap is charged whole to the later sample"))
        if sampler.ibs_dump_kind == "ibsop-event-dump":
            if not has_tag:
                fields.append(("PCT_CYCLES", "not available for this dump: no IbsTagToRetCtr "
                                             "column; pct_samples and pct_time are unaffected"))
            elif tag_available:
                fields.append(("PCT_CYCLES", "share of sampled op latency (tag-to-retire, "
                                             "IbsTagToRetCtr), clipped at 131070; never time"))
            else:
                fields.append(("PCT_CYCLES", "not available for this dump: IbsTagToRetCtr has no "
                                             "non-null values; pct_samples and pct_time are "
                                             "unaffected"))
        else:
            fields.append(("PCT_CYCLES", "not applicable; IBS fetch samples carry no "
                                         "tag-to-retire counter, so pct_cycles is omitted"))
    if dump is None:
        weights = "PMC sample counts from UnifiedSampleSeries; pct_samples is the CPU-time share"
    else:
        window = ("whole capture" if args.time_filter is None
                  else f"time filter {args.time_filter} (first sample per CPU inside the window "
                       "excluded)")
        weights = ("exact DeltaTick-gap weights per CPU_Number (first sample per CPU excluded; gap "
                   f"time is charged to the later sample); {window}; pct_samples is the dump's raw "
                   "sample share")
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


def _achieved_field(connection: object, has_dump: bool, args: argparse.Namespace,
                    sampler: uprof_session.Sampler,
                    dump: uprof_dump.DumpInfo | None) -> tuple[tuple[str, str], int]:
    """The achieved sampling rate over the whole capture, independent of the scope filter.

    The returned sample count is the same whole-capture total, reused by the kernel-coverage
    check so that check need not scan the capture again.
    """
    meta = uprof_session.read_meta(args.session) or {}
    schedule = (meta.get("collection") or {}).get("schedule") or {}
    elapsed_ms = schedule.get("elapsed")
    contexts: list[uprof_session.ContextCadence] = []
    label = "CPU"
    window_s: float | None = None
    if has_dump:
        rows = connection.sql("SELECT count(*), count(DISTINCT cpu) FROM v_dump").fetchall()
        samples = int(rows[0][0] or 0)
        cpus = int(rows[0][1] or 0)
        wide = uprof_session.system_wide(args.session)
        label = "CPU" if wide else "thread"
        contexts = _dump_contexts(connection, "cpu" if wide else "tid",
                                  uprof_session.run_gap_limit_ns(sampler))
        if dump is not None and dump.time_filter:
            bounds = util.parse_time_filter(dump.time_filter)
            if bounds is not None:
                window_s = float(bounds[1] - bounds[0])
    else:
        # PMC samples are the event-column counts in UnifiedSampleSeries; coreId is the CPU.
        rows = connection.sql(
            "SELECT CAST(COALESCE(sum(COLUMNS(lambda c: c = "
            "getvariable('uprof_event_column'))), 0) AS BIGINT), count(DISTINCT coreId) "
            "FROM UnifiedSampleSeries").fetchall()
        samples = int(rows[0][0] or 0)
        cpus = int(rows[0][1] or 0)
        # PMC carries no per-sample timestamps, so a core's samples/s is its whole cadence.
        per_core = connection.sql(
            "SELECT coreId, CAST(COALESCE(sum(COLUMNS(lambda c: c = "
            "getvariable('uprof_event_column'))), 0) AS BIGINT) "
            "FROM UnifiedSampleSeries GROUP BY coreId").fetchall()
        contexts = [uprof_session.ContextCadence(int(row[1] or 0)) for row in per_core]
        label = "core"
    if window_s is None and elapsed_ms is not None:
        try:
            window_s = int(elapsed_ms) / 1000.0
        except (TypeError, ValueError):
            window_s = None
    field = ("ACHIEVED", uprof_session.achieved_summary(samples, window_s, cpus, sampler,
                                                        contexts, label))
    return field, samples


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


def _kernel_coverage_field(connection: object, sampler: uprof_session.Sampler,
                           windowed: bool = False,
                           captured_samples: int | None = None) -> list[tuple[str, str]]:
    """Whole-capture kernel coverage, checked only when the applied sampler requested OS mode.

    v_capture_modules carries every module with its sample count and whether it is a kernel
    image, before any scope filter. windowed suppresses the field: the resolved dump may have
    been produced by the session's
    recorded translate --time-filter, so it need not hold the whole capture. captured_samples is
    the capture total the ACHIEVED check already counted.
    """
    if not sampler.os_requested or windowed:
        return []
    rows = connection.sql(
        "SELECT module_name, CAST(COALESCE(sum(samples), 0) AS BIGINT) AS samples, "
        "max(kernel_image) AS kernel_image "
        "FROM v_capture_modules GROUP BY module_name").fetchall()
    kernel_samples = sum(int(samples or 0) for _name, samples, kernel in rows if kernel)
    warning = uprof_session.kernel_coverage_warning(sampler, kernel_samples, captured_samples)
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
                "--time-filter cannot be applied to a PMC capture: PMC samples carry no "
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
            fields = _header_fields(args, sampler, dump, has_tag, tag_available)
            if sanity_message:
                fields.append(("SANITY", sanity_message))
            achieved, captured_samples = _achieved_field(
                connection, dump is not None, args, sampler, dump)
            if achieved:
                fields.append(achieved)
            fields.extend(_kernel_coverage_field(
                connection, sampler, dump_windowed, captured_samples))
            fields.extend(_scope_warning(connection))
            if args.by in SYMBOLIZED:
                agreement = _agreement_text(connection)
                if agreement:
                    fields.append(("AGREEMENT", agreement))
            uprof_db.output(connection, TOOLS_DIR / f"profile-{args.by}.sql", fields,
                            args.json, out_path)
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
