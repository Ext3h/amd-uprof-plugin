"""Read session.uprof and decide what the capture's sample shares mean.

The sampler, interval and cycle-vs-op control come from collection.events.ibsEvents and
pmcEvents.pmcEventList. collectCommand is only a fallback: a --config capture records no event=
there. Nothing here reads a documented constant instead of the capture.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from uprof_util import UprofError

PMC_EVENT = "event#0x3000076"
IBS_OP_EVENT = "event#0x100000000f100"
IBS_FETCH_EVENT = "event#0x100000000f000"


@dataclass
class Sampler:
    """What session.uprof records, and therefore what its sample shares mean.

    ibs_dump_kind is a DUMP_FILENAMES key when an IBS ASCII dump exists; metric is the METRIC
    line printed before query results. pmc marks a PMC cycle capture, whose samples come from
    cpu.db rather than a dump; event_column names its sample column in UnifiedSampleSeries.
    """

    ibs_dump_kind: str | None = None
    metric: str = "METRIC: session.uprof not found -- metric basis not established"
    pmc: bool = False
    event_column: str | None = None
    interval: int | None = None
    interval_units: str | None = None  # 'cycles' (time-convertible), 'ops' or 'fetch' (not)


def read_meta(session_dir: Path | str) -> dict[str, Any] | None:
    path = Path(session_dir) / "session.uprof"
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {}


def _registered_columns(meta: dict[str, Any]) -> dict[str, str]:
    columns: dict[str, str] = {}
    for category in (meta.get("auxiliary") or {}).get("eventColumnIds") or []:
        for event in category.get("events") or []:
            name = event.get("name")
            encoded = event.get("encodedId")
            if name and encoded is not None:
                columns.setdefault(str(name), f"event#0x{int(encoded):x}")
    return columns


def _encoded_name(event: dict[str, Any]) -> str | None:
    """The event#0x... spelling of the entry's encodedEventId; None when it has none."""
    encoded = event.get("encodedEventId")
    return f"event#0x{int(encoded):x}" if encoded is not None else None


def _event_from_command(meta: dict[str, Any]) -> str | None:
    match = re.search(r"event=([^,\s]+)", str(meta.get("collectCommand") or ""))
    return match.group(1) if match else None


def derive_sampler(session_dir: Path | str) -> Sampler:
    meta = read_meta(session_dir)
    if meta is None:
        return Sampler()
    if not meta:
        return Sampler(metric="METRIC: session.uprof is not readable JSON -- metric basis not established")

    columns = _registered_columns(meta)
    from_command = _event_from_command(meta)
    ibs = (meta.get("collection") or {}).get("events", {}).get("ibsEvents")
    is_ibs = bool((meta.get("auxiliary") or {}).get("ibsOpSampling")) or bool(
        ibs and (ibs.get("op") or ibs.get("fetch")))

    if is_ibs:
        if ibs and ibs.get("op"):
            event = columns.get("IBS_ALL_OPS", IBS_OP_EVENT)
            interval = ibs.get("opInterval")
            if bool(ibs.get("opCycleCount")):
                metric = (f"METRIC: IBS OP (IBS_ALL_OPS {event}, cycle-controlled interval {interval}) "
                          "-- sample shares: time (cycle-controlled samples; busy CPU time, "
                          "frequency-dependent; system-wide it is summed over cores)")
            else:
                metric = (f"METRIC: IBS OP (IBS_ALL_OPS {event}, op-count interval {interval}) "
                          "-- sample shares: instruction share (op counts) -- not time; "
                          "cycle attribution: per-op latency, clipped at 131070")
            return Sampler(ibs_dump_kind="ibsop-event-dump", metric=metric, interval=interval,
                           interval_units="cycles" if bool(ibs.get("opCycleCount")) else "ops")
        if ibs and ibs.get("fetch"):
            event = columns.get("IBS_FETCH", IBS_FETCH_EVENT)
            metric = (f"METRIC: IBS fetch (IBS_FETCH {event}, interval {ibs.get('fetchInterval')}) "
                      "-- sample shares: fetch-sample share, not time")
            return Sampler(ibs_dump_kind="ibsfetch-event-dump", metric=metric,
                           interval=ibs.get("fetchInterval"), interval_units="fetch")
        return Sampler(metric=(
            "METRIC: IBS capture (details not recorded in session.uprof) -- sample shares: "
            "not established; do not read them as time"))

    pmc = [event for event in
           ((meta.get("collection") or {}).get("events", {}).get("pmcEvents") or {}).get("pmcEventList") or []
           if event]
    if pmc:
        mapped = columns.get("CYCLES_NOT_IN_HALT")
        event = next((entry for entry in pmc if entry.get("hwId") == 118), None)
        if event is None and mapped is not None:
            event = next((entry for entry in pmc if _encoded_name(entry) == mapped), None)
        if event is None:
            event = pmc[0]
        expected = _encoded_name(event)
        column = mapped if mapped is not None else (expected or PMC_EVENT)
        if mapped is not None and mapped != expected:
            raise UprofError(
                f"session.uprof is inconsistent: auxiliary.eventColumnIds maps "
                f"CYCLES_NOT_IN_HALT to {mapped}, but the hwId {event.get('hwId')} entry in "
                f"pmcEventList has encodedEventId {expected or 'none'}; re-run the capture or "
                "regenerate session.uprof")
        if event.get("hwId") == 118:
            name = "CYCLES_NOT_IN_HALT"
        elif from_command:
            name = from_command
        else:
            name = f"hwId {event.get('hwId')}"
        if event.get("hwId") == 118 and event.get("isSampling"):
            metric = (f"METRIC: {name} ({column}, hwId {event.get('hwId')}, "
                      f"interval {event.get('interval')}) -- sample shares: time (cycle samples; "
                      "busy CPU time, frequency-dependent; system-wide it is summed over cores)")
        else:
            metric = (f"METRIC: {name} (hwId {event.get('hwId')}, interval {event.get('interval')}, "
                      f"isSampling={event.get('isSampling')}) -- sample shares: not established; "
                      "do not read the shares as time")
        return Sampler(metric=metric, pmc=True, event_column=column,
                       interval=event.get("interval"), interval_units="cycles")

    if from_command:
        metric = (f"METRIC: {from_command} (from collectCommand; session.uprof records no PMC or IBS "
                  "event) -- sample shares: not established")
    else:
        metric = "METRIC: no PMC or IBS event recorded in session.uprof -- sample shares: not established"
    return Sampler(metric=metric)


def verify_collect(session_dir: Path | str, event: str, interval: int, count_control: str,
                   system_wide: bool, pids: list[int]) -> dict[str, Any]:
    """Check the applied event against the request; return the metadata for printing."""
    meta = read_meta(session_dir)
    if not meta:
        raise UprofError(f"session.uprof is missing or unreadable in {session_dir}")
    target = (meta.get("collection") or {}).get("target") or {}
    collection_event = ((meta.get("collection") or {}).get("events") or {})
    if event == "cycles":
        pmc = ((collection_event.get("pmcEvents") or {}).get("pmcEventList") or [])
        if not pmc:
            raise UprofError("no PMC event recorded; this is not a cycle capture")
        applied = pmc[0]
        if applied.get("hwId") != 118:
            raise UprofError(f"PMC event is hwId {applied.get('hwId')}, not PMCx076 (118)")
        if applied.get("interval") != interval:
            raise UprofError(f"interval is {applied.get('interval')}, expected {interval}")
        if not applied.get("isSampling"):
            raise UprofError("PMC event is not sampling")
        if (meta.get("auxiliary") or {}).get("ibsOpSampling"):
            raise UprofError("capture is IBS, not PMC cycle counting")
    else:
        ibs = collection_event.get("ibsEvents") or {}
        if not ibs.get("op"):
            raise UprofError("no IBS OP event recorded; this is not an IBS OP capture")
        if not (meta.get("auxiliary") or {}).get("ibsOpSampling"):
            raise UprofError("capture metadata says IBS OP sampling is off")
        if ibs.get("opInterval") != interval:
            raise UprofError(f"IBS interval is {ibs.get('opInterval')}, expected {interval}")
        if bool(ibs.get("opCycleCount")) != (count_control == "cycles"):
            raise UprofError(f"IBS count control is opCycleCount={ibs.get('opCycleCount')}, "
                             f"expected {count_control == 'cycles'} ({count_control})")
    if bool(target.get("systemWide")) != system_wide:
        raise UprofError(f"capture target mode is systemWide={target.get('systemWide')}, "
                         f"expected {system_wide} (use -a or --process)")
    if not system_wide:
        attached = {int(entry.get("processId")) for entry in target.get("attachedProcesses") or []
                    if entry.get("processId") is not None}
        missing = [pid for pid in pids if pid not in attached]
        if missing:
            raise UprofError("capture did not attach to PID(s): " + ", ".join(map(str, missing)))
    return meta


def collect_summary(meta: dict[str, Any], event: str) -> str:
    collection_event = ((meta.get("collection") or {}).get("events") or {})
    target = (meta.get("collection") or {}).get("target") or {}
    auxiliary = meta.get("auxiliary") or {}
    if event == "cycles":
        applied = (((collection_event.get("pmcEvents") or {}).get("pmcEventList")) or [{}])[0]
        return (f"hwId={applied.get('hwId')} interval={applied.get('interval')} "
                f"isSampling={applied.get('isSampling')} systemWide={target.get('systemWide')} "
                f"ibsOpSampling={auxiliary.get('ibsOpSampling')}")
    ibs = collection_event.get("ibsEvents") or {}
    return (f"ibsOpSampling={auxiliary.get('ibsOpSampling')} ibsOp={ibs.get('op')} "
            f"opInterval={ibs.get('opInterval')} opCycleCount={ibs.get('opCycleCount')} "
            f"systemWide={target.get('systemWide')}")


def achieved_summary(samples: int, window_s: float | None, cpus: int,
                     sampler: Sampler) -> str:
    """The `ACHIEVED` text: delivered samples, capture window, CPUs and rate per CPU.

    A capture can accept an interval it cannot deliver: the collector's effective cadence is far
    below the request in both modes. Samples per second per CPU makes that visible instead of
    leaving a sparse capture to look authoritative. cpus is 0 only when the capture records no
    CPU id; the per-CPU rate is then not derivable and the text says so.
    """
    if window_s is None or window_s <= 0:
        return (f"{samples} samples; the capture records no usable duration, so the rate is "
                "not derivable")

    ideal_per_s: float | None = None
    ideal_note = ""
    warn = False
    if sampler.interval is None or sampler.interval_units is None:
        requested = "the session records no interval"
    elif sampler.interval_units == "cycles":
        try:
            from uprof_collect import _core_frequency
            hertz, display, _source = _core_frequency()
            interval_s = sampler.interval / hertz
            shown = (f"{interval_s * 1e3:.3g} ms" if interval_s >= 1e-3
                     else f"{interval_s * 1e6:.3g} us")
            requested = f"{sampler.interval} cycles at {display} = {shown}"
            ideal_per_s = 1.0 / interval_s
            ideal_note = f"; ideal {ideal_per_s:,.0f}/s per CPU"
            warn = True
        except Exception:
            requested = f"{sampler.interval} cycles (core frequency unavailable)"
    elif sampler.interval_units == "ops":
        requested = f"{sampler.interval} dispatched ops"
        ideal_note = "; the interval is a work count, so no time rate follows"
    else:
        requested = f"{sampler.interval} fetch events"

    if cpus:
        rate = samples / window_s / cpus
        grade = f"{samples} samples, {window_s:.3f} s, {cpus} CPUs, {rate:,.2f}/s per CPU"
        below = warn and rate < 0.1 * ideal_per_s
    else:
        rate = samples / window_s
        grade = f"{samples} samples, {window_s:.3f} s, {rate:,.2f}/s over the capture"
        ideal_note += "; the capture records no CPU id, so the per-CPU rate is not derivable"
        below = False
    text = f"{grade} (requested {requested}{ideal_note})"
    if below:
        text += "; WARN: achieved sample rate is below 10% of the requested interval"
    return text


def anchor_report(session_dir: Path | str, qpc: int, freq: int | None) -> list[str]:
    """Map a QueryPerformanceCounter reading onto capture-relative seconds.

    collection.schedule.start/end are QPC-derived nanoseconds and each sample's DeltaTick is
    sample_ns - start, so a workload's own QPC reading converts with
    (qpc * 1e9 / freq - start) / 1e9. A reading taken before the capture started (negative)
    is attach latency: the capture does not cover it.
    """
    meta = read_meta(session_dir)
    if not meta:
        raise UprofError(f"session.uprof is missing or unreadable in {session_dir}")
    schedule = (meta.get("collection") or {}).get("schedule") or {}
    start = schedule.get("start")
    if start is None:
        raise UprofError("session.uprof records no collection.schedule.start; cannot anchor")
    from datetime import timezone

    hertz = freq or 10_000_000
    source = "argument" if freq else "default (10 MHz; the session records no QPC frequency)"
    relative = (qpc * 1e9 / hertz - int(start)) / 1e9
    lines = [
        f"QPC={qpc} HZ={hertz} HZ_SOURCE={source}",
        f"START_NS={int(start)}",
        f"RELATIVE_S={relative:.9f}",
    ]
    stamp = schedule.get("startTs")
    if stamp:
        parsed = _parse_start_ts(str(stamp))
        if parsed is not None:
            utc = parsed.astimezone(timezone.utc)
            lines.append(f"START_LOCAL={parsed.isoformat()}")
            lines.append(f"START_UTC={utc:%Y-%m-%dT%H:%M:%S}Z (precision: one second)")
        else:
            lines.append(f"START_TS={stamp} (unparsed; no UTC conversion)")
    else:
        lines.append("START_TS=absent (no UTC conversion)")
    if relative < 0:
        lines.append("NOTE=RELATIVE_S is negative: the reading precedes the capture start "
                     "(attach latency); the capture does not cover it")
    else:
        lines.append("NOTE=RELATIVE_S is seconds from capture start, comparable to "
                     "--time-filter")
    return lines


def _parse_start_ts(text: str):
    """schedule.startTs is local time with a single-digit day, e.g. 'Sat Nov  7 12:56:23 2026'."""
    import datetime

    collapsed = re.sub(r"\s+", " ", text.strip())
    try:
        parsed = datetime.datetime.strptime(collapsed, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.astimezone()
