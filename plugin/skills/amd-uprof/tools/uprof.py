"""AMD uProf capture and query entry point: python <SKILL_DIR>/tools/uprof.py <command> ..."""

from __future__ import annotations

import argparse
import sys

import uprof_util as util

EPILOG = """\
Sampling modes and what a share means:
  cycle sampling (default): event=CYCLES_NOT_IN_HALT (hwId 118). --sample-interval is a time
      (ns, us, ms or s) converted to core cycles at the machine's reported core frequency; the
      value, frequency and source are printed. sample_share is the busy core-cycle share (fixed
      cycles per sample; halted excluded), summed over cores in a system-wide capture.
      Default 1ms.
  IBS OP, cycle-controlled (--ibs-count-control cycles): the same time conversion for the
      interval; sample_share is the core-cycle share (fixed cycles per sample), not wall time,
      not a per-op cost.
  IBS OP, op-count interval (--ibs-count-control op, the default): --sample-interval is a
      positive integer work count (dispatched ops), not a time; default 1000000 ops.
      sample_share is the dispatched-op frequency share (fixed dispatched ops per sample), not
      a per-op cost.

collect captures PMC cycle samples or IBS OP samples; it does not capture IBS fetch. An IBS
fetch dump produced elsewhere can be translated (--ascii ibsfetch-event-dump) and profiled,
but no capture of IBS fetch has been validated (unverified).

profile reads the IBS ASCII dump (IbsOpDump.csv or IbsFetchDump.csv). Samples are ordered by
DeltaTick within an execution context and weighed by the gap to the previous sample; the first
sample per context is ignored, having no gap before it. The context is CPU_Number for a
system-wide capture and (PID, TID) for an attached one, so a migrating attached thread is counted
once. --time-filter selects samples by DeltaTick and excludes the first sample per context inside
the window. A missing dump is generated on a cached copy of the session, never in place.
sample_share is the raw sample share; what a sample represents depends on the sampler above.
time_share is a time share estimated from those gaps (an attached thread's gaps include off-CPU
time; for a cycle-controlled capture sample_share is the CPU-cycle share and the better ranking);
tag_to_retire_share is the share of tag-to-retire cycles summed over sampled ops, a total, not
per-op latency. All shares are fractions (0.0-1.0). function, region, line and instruction
symbolize every module in scope and print the uProf-vs-symbolizer agreement summary. function
groups by a derived function range (.pdata, PE exports or PDB publics) and keeps uProf
Function.id where no range was derived. module, function and region add isSystemModule
(Module's OS-shipped flag) and cpu_seconds = samples x the requested cycle interval / the
reported core frequency; that conversion applies only to a cycle interval, and the result is
CPU time, not wall time.

Output is a Markdown pipe table with the METRIC, COLUMNS, READ, NEXT, DUMP, TIME_SHARE,
TAG_TO_RETIRE_SHARE, WEIGHTS and AGREEMENT header lines above it; --json emits those fields, a
structured legend and the rows as a JSON document. COLUMNS names what each share measures in the
capture mode; READ states which column ranks where time went and that the <other ...> remainder
is last, not ranked; NEXT lists runnable calls that keep the session and scope. Shares are
emitted at full precision. Table cells and header values show CR, LF, TAB, NUL, other
C0 controls and DEL as
visible \\n, \\r, \\t or \\xNN escapes; JSON keeps the original characters, so table and JSON
agree modulo this display escaping for control characters. --top N returns N rows and a
<other ...> remainder row when the result is truncated; --top all returns every row.
--out-file writes exactly the bytes stdout would carry: UTF-8 without a BOM, LF newlines.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uprof.py",
        description="AMD uProf capture, translation and DuckDB analysis.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser(
        "collect",
        help="capture PMC cycle or IBS OP samples (not IBS fetch)",
        description="Capture PMC cycle samples or IBS OP samples. IBS fetch is not captured; an "
        "IBS fetch dump produced elsewhere can be translated "
        "(--ascii ibsfetch-event-dump) and profiled, but no fetch capture has been "
        "validated (unverified).",
    )
    collect.add_argument("--out-dir", required=True)
    collect.add_argument("--seconds", type=int, default=60)
    collect.add_argument("--process", action="append", metavar="PID[,PID]")
    collect.add_argument(
        "--sample-interval",
        metavar="TIME|OPS",
        help="cycle modes: a time with a unit (ns, us, ms, s), e.g. 200us or 1ms, converted to "
        "core cycles at the machine's reported core frequency; default 1ms. IBS op-count "
        "mode: a positive integer work count (dispatched ops), not time; default 1000000 ops",
    )
    collect.add_argument(
        "--event",
        choices=("cycles", "ibs-op", "timer"),
        default="cycles",
        help="cycles (PMC, default), ibs-op (IBS OP) or timer (uProf timer, "
        "system-wide or with --launch; --sample-interval in whole ms)",
    )
    collect.add_argument(
        "--ibs-count-control",
        choices=("op", "cycles"),
        default="op",
        help="IBS OP interval control: op (work count, default) or cycles "
        "(time converted to cycles)",
    )
    collect.add_argument("--dry-run", action="store_true")
    collect.add_argument(
        "--launch",
        nargs=argparse.REMAINDER,
        metavar="APP [ARGS]",
        help="--event timer only: start APP with its arguments under the "
        "capture; must be the last option",
    )

    translate = sub.add_parser("translate", help="resolve symbols; optionally write an ASCII dump")
    translate.add_argument("--session", required=True)
    translate.add_argument("--out-dir", required=True)
    translate.add_argument(
        "--symbol-path",
        action="append",
        metavar="PATH",
        help="symbol or debug file, or a directory to search (repeatable); passed to the "
        "translator's own symbol-path option",
    )
    translate.add_argument("--symbol-cache")
    translate.add_argument("--symbol-server", action="append")
    translate.add_argument("--time-filter", metavar="T1:T2")
    translate.add_argument("--ascii", choices=("ibsop-event-dump", "ibsfetch-event-dump"))
    translate.add_argument(
        "--force",
        action="store_true",
        help="replace cpu.db, callstack.db, any ASCII dump and the copied session files when "
        "--out-dir already holds an earlier translation; without it that is an error",
    )

    profile = sub.add_parser("profile", help="samples and time shares at one granularity")
    profile.add_argument("--session", required=True)
    profile.add_argument(
        "--by",
        required=True,
        choices=("process", "module", "region", "function", "line", "instruction"),
    )
    scope = profile.add_mutually_exclusive_group()
    scope.add_argument("--pid", type=int, metavar="N")
    scope.add_argument(
        "--image",
        metavar="PATTERN",
        help="process base-image ILIKE pattern; all PIDs of matching images",
    )
    profile.add_argument("--time-filter", metavar="T1:T2")
    profile.add_argument(
        "--function",
        metavar="NAME",
        help="narrow --by instruction|line to one function row's 'function' name; requires "
        "--module and reports the parent function's samples, shares and cpu_seconds",
    )
    profile.add_argument(
        "--module",
        metavar="MODULE",
        help="the function row's 'module' value; required with --function so the filter scope is "
        "explicit",
    )
    profile.add_argument(
        "--function-start",
        metavar="RVA",
        help="optional function_start (0x hex or decimal) when --function matches more than one "
        "row in the module",
    )
    profile.add_argument(
        "--symbol-path",
        action="append",
        metavar="PATH",
        help="symbol or debug file, or a directory to search, for "
        "--by function|region|line|instruction (repeatable); the path is probed: a file is "
        "an explicit symbol/debug input, a directory a search root",
    )
    profile.add_argument(
        "--image-path",
        action="append",
        metavar="PATH",
        help="module image file, or a directory in which to find it by module basename "
        "(repeatable), for --by function|region|line|instruction; supplies an image whose "
        "recorded path is not on this host. Distinct from --image, which filters processes",
    )
    profile.add_argument("--top", default="20", metavar="N|all")
    profile.add_argument(
        "--json", action="store_true", help="emit the header fields and rows as JSON"
    )
    profile.add_argument("--out-file")

    anchor = sub.add_parser(
        "anchor",
        help="map a QPC reading onto capture-relative seconds",
        description="Map a QueryPerformanceCounter reading (for example a marker a workload "
        "printed) onto the capture's own clock. schedule.start/end are QPC-derived "
        "nanoseconds and each sample's DeltaTick is sample_ns - start, so relative "
        "seconds = (qpc * 1e9 / freq - start) / 1e9; negative values are attach "
        "latency, which the capture does not cover.",
    )
    anchor.add_argument("--session", required=True)
    anchor.add_argument("--qpc", type=int, required=True, metavar="TICKS")
    anchor.add_argument(
        "--freq",
        type=int,
        metavar="HZ",
        help="QueryPerformanceFrequency; defaults to 10 MHz (Windows QPC) and "
        "says so in the output",
    )

    schema = sub.add_parser("schema", help="live table/column/type listing")
    schema.add_argument("--session", required=True)
    schema.add_argument("--out-file")

    sub.add_parser("tools", help="print resolved tool paths and versions")
    return parser


def _parse_pids(values: list[str] | None) -> list[int]:
    pids: list[int] = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if not part:
                raise util.UprofError("--process needs at least one positive PID")
            try:
                pids.append(int(part))
            except ValueError as exc:
                raise util.UprofError(f"--process needs integer PID(s); got {part!r}") from exc
    return pids


def _warn_if_elevated(command: str) -> None:
    """Only collect needs elevation; elsewhere the capture's files are read with the admin token."""
    if command in ("translate", "profile"):
        import uprof_native

        if uprof_native.is_admin():
            print(
                f"WARNING: running elevated; only collect needs elevation, so run {command} "
                "from a normal shell",
                file=sys.stderr,
            )


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "collect":
        import uprof_collect

        uprof_collect.collect(
            uprof_collect.CollectOptions(
                out_dir=args.out_dir,
                seconds=args.seconds,
                pids=_parse_pids(args.process),
                sample_interval=args.sample_interval,
                event=args.event,
                ibs_count_control=args.ibs_count_control,
                dry_run=args.dry_run,
                launch=args.launch,
            )
        )
    elif args.command == "translate":
        import uprof_native

        uprof_native.translate(
            args.session,
            args.out_dir,
            args.symbol_path,
            args.symbol_cache,
            args.symbol_server,
            args.time_filter,
            args.ascii,
            args.force,
        )
    elif args.command == "tools":
        import uprof_native

        uprof_native.tool_report()
    elif args.command == "anchor":
        import uprof_session

        for line in uprof_session.anchor_report(args.session, args.qpc, args.freq):
            print(line)
    else:
        import uprof_query

        uprof_query.run_query_command(args)


def main(argv: list[str] | None = None) -> int:
    util.configure_streams()
    args = build_parser().parse_args(argv)
    _warn_if_elevated(args.command)
    try:
        _dispatch(args)
        return 0
    except util.UprofError as exc:
        # A LinesError's own lines (cause, ALTERNATIVE, data) were escaped one by one; any other
        # message is one line, whatever capture text it carries.
        text = str(exc) if isinstance(exc, util.LinesError) else util.one_line(exc)
        print(f"error: {text}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"error: {util.one_line(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
