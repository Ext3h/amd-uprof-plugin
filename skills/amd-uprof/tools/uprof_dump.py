"""Resolve the IBS ASCII dump pct_time reads.

Preference: one beside the session, a current cached dump, then a dump generated on a copy of the
raw session under the temp directory. The source session is never translated in place. A dump-only
translate still writes a degraded cpu.db, so every database is removed from the cache before it is
handed back.

Generation runs in a unique staging directory renamed into place when complete, so concurrent
runs cannot delete each other's work. The Linux branch is kept but unverified on this host.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from uprof_native import TRANSLATE_TIMEOUT, require_uprof, run, translate_command
from uprof_session import Sampler
from uprof_util import DUMP_FILENAMES, UprofError, absolute, copy_session, read_text, tail_lines

_TIME_FILTER = re.compile(r"--time-filter[ =]+(\d+(?:\.\d+)?:\d+(?:\.\d+)?)")


@dataclass
class DumpInfo:
    """A resolved dump, its provenance, and the cache it may live in.

    source is beside | reused | generated; reason is printed with the DUMP line; time_filter is the
    translate window recorded for the dump.
    """

    path: Path
    source: str
    cache_dir: Path | None = None
    reason: str = ""
    time_filter: str | None = None


def cache_dir(session_dir: Path | str) -> Path:
    full = os.path.normcase(os.path.abspath(os.fspath(session_dir))).rstrip("\\/")
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "uprof-dump-cache" / digest


def remove_databases(directory: Path) -> None:
    if not directory.is_dir():
        return
    for path in directory.rglob("*"):
        if path.is_file() and re.search(r"\.(db|duckdb)(\.(wal|shm))?$", path.name, re.IGNORECASE):
            try:
                path.unlink()
            except OSError as exc:
                print(f"WARNING: could not remove {path}: {exc}; the cache copy still contains "
                      "a database", file=sys.stderr)


def newest_raw_prd(session_dir: Path | str) -> float | None:
    cpu_dir = Path(session_dir) / "cpu"
    if not cpu_dir.is_dir():
        return None
    times = [path.stat().st_mtime
             for path in cpu_dir.iterdir() if path.name.lower().endswith(".prd")]
    return max(times) if times else None


def recorded_time_filter(session_dir: Path | str) -> str | None:
    """The T1:T2 window of the session's recorded translate --time-filter, or None.

    A resolved dump may have been produced by that translate, so it may hold only the window's
    samples rather than the whole capture.
    """
    try:
        meta = json.loads(read_text(Path(session_dir) / "session.uprof"))
    except (UprofError, json.JSONDecodeError):
        return None
    match = _TIME_FILTER.search(str(meta.get("translateCommand") or ""))
    return match.group(1) if match else None


def _write_cache_meta(path: Path, session_dir: Path | str, kind: str, time_filter: str | None,
                      dump: Path) -> None:
    meta = {"sessionPath": str(session_dir), "kind": kind, "timeFilter": time_filter or "",
            "dump": str(dump)}
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(meta, handle, indent=2)
    except OSError as exc:
        raise UprofError(f"cannot write {path}: {exc}; check free space and permissions on the "
                         "dump cache") from exc


def _make_dump(session_dir: Path | str, kind: str, time_filter: str | None, cache: Path) -> Path:
    dump_name = DUMP_FILENAMES[kind]
    final_dump = cache / dump_name
    uprof = require_uprof()
    print(f"GENERATING DUMP: {cache} (AMDuProfCLI translate --ascii; this can take hours)",
          file=sys.stderr)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UprofError(f"cannot create the dump cache {cache.parent}: {exc}; free the path or "
                         "fix its permissions") from exc
    try:
        staging = Path(tempfile.mkdtemp(prefix="uprof-dump-", dir=cache.parent))
    except OSError as exc:
        raise UprofError(f"cannot create a staging directory in {cache.parent}: {exc}; check "
                         "free space and permissions on the temp directory") from exc
    try:
        copy_session(session_dir, staging)
        command = translate_command(uprof, staging, time_filter=time_filter, ascii_kind=kind)
        completed = run(command, timeout=TRANSLATE_TIMEOUT)
        detail = tail_lines((completed.stdout or "") + "\n" + (completed.stderr or ""))
        if completed.returncode != 0:
            raise UprofError(f"AMDuProfCLI translate --ascii {kind} failed with "
                             f"{completed.returncode} on the cache copy {staging}: {detail}")
        dump = staging / dump_name
        if not dump.is_file():
            raise UprofError(f"translate --ascii did not produce {dump_name} in {staging}; "
                             f"output: {detail}")
        try:
            size = dump.stat().st_size
        except OSError as exc:
            raise UprofError(f"cannot read {dump}: {exc}; the dump may have been removed during "
                             "translation") from exc
        if size <= 0:
            raise UprofError(f"translate --ascii produced an empty {dump_name} in {staging}; "
                             f"output: {detail}")
        remove_databases(staging)
        _write_cache_meta(staging / "cache.json", session_dir, kind, time_filter, final_dump)
        try:
            if cache.is_dir():
                shutil.rmtree(cache)
            os.replace(staging, cache)
        except OSError as exc:
            # Another run may have installed a complete cache first; keep it only when it
            # matches this request, never as a fallback for a stale dump.
            try:
                meta = json.loads(read_text(cache / "cache.json"))
            except (UprofError, json.JSONDecodeError):
                meta = {}
            current = (final_dump.is_file() and str(meta.get("kind")) == kind
                       and str(meta.get("timeFilter") or "") == str(time_filter or ""))
            if not current:
                raise UprofError(f"cannot install the dump cache at {cache}: {exc}; remove the "
                                 "path or fix its permissions and retry") from exc
            shutil.rmtree(staging, ignore_errors=True)
        return final_dump
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def resolve_dump(session_dir: Path | str, sampler: Sampler) -> DumpInfo:
    meta_path = Path(session_dir) / "session.uprof"
    if not meta_path.is_file():
        raise UprofError(
            f"session.uprof is missing from {session_dir}; pct_time needs it to identify the "
            "sampler and check the dump against the capture span")
    if not sampler.ibs_dump_kind:
        raise UprofError(
            "pct_time needs the IBS ASCII sample dump, and session.uprof records no IBS OP or IBS "
            "fetch event: this capture has no per-sample timestamps. Profile a capture made with "
            "--event ibs-op, or use schema/tools for a PMC cycle capture")
    dump_name = DUMP_FILENAMES[sampler.ibs_dump_kind]
    path = Path(session_dir) / dump_name
    if path.is_file():
        return DumpInfo(absolute(path), "beside", reason="found beside the session")

    time_filter = recorded_time_filter(session_dir)
    cache = cache_dir(session_dir)
    cached_dump = cache / dump_name
    cache_meta = cache / "cache.json"
    newest_raw = newest_raw_prd(session_dir)

    reuse = False
    reason = "no cached dump for this session"
    if cache_meta.is_file():
        try:
            meta = json.loads(read_text(cache_meta))
        except (UprofError, json.JSONDecodeError):
            meta = None
        if not meta or not cached_dump.is_file():
            reason = "cached dump or its metadata is missing"
        elif str(meta.get("kind")) != sampler.ibs_dump_kind:
            reason = f"cached dump is for {meta.get('kind')}, not {sampler.ibs_dump_kind}"
        elif str(meta.get("timeFilter") or "") != str(time_filter or ""):
            reason = "cached dump was made with a different time filter"
        elif newest_raw and cached_dump.stat().st_mtime < newest_raw:
            reason = "cached dump is older than the session's raw cpu/*.prd"
        else:
            reuse = True
    if reuse:
        remove_databases(cache)
        return DumpInfo(absolute(cached_dump), "reused", cache_dir=cache,
                        reason="dump is at least as new as the session's raw cpu/*.prd",
                        time_filter=time_filter)

    dump = _make_dump(session_dir, sampler.ibs_dump_kind, time_filter, cache)
    return DumpInfo(absolute(dump), "generated", cache_dir=cache, reason=reason,
                    time_filter=time_filter)
