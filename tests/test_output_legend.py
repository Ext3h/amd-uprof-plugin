"""The profile output contract: measure-first share columns, fractions and a short legend.

Seven synthetic captures cover the modes whose `sample_share` means different things: PMC
cycles, an unsupported PMC event, IBS OP cycle-controlled, IBS OP op-count, IBS fetch, IBS OP
without a tag column and IBS OP with an all-blank tag column. Two more fixtures check the row
order, the remainder row and the runnable NEXT calls. No capture or external tool is used.

Run from the repository root: python tests/run_tests.py test_output_legend
"""
# Spec: SPEC-29, SPEC-37, SPEC-42, SPEC-48

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

import uprof_db

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
EVENT_COLUMN = "event#0x3000076"

SAMPLE_PMC = "busy core-cycle share (fixed cycles per sample; halted excluded)"
SAMPLE_PMC_UNSUPPORTED = ("not established (this PMC event is not the supported cycle event); "
                          "capture --event cycles (CYCLES_NOT_IN_HALT, hwId 118) to make "
                          "sample_share a core-cycle share")
TIME_ABSENT_PMC = ("this export aggregates PMC samples per window; capture with --event ibs-op "
                   "(IBS OP with per-sample timestamps) to get time_share")
SAMPLE_OP_CYCLES = "core-cycle share (fixed cycles per sample), not wall time"
SAMPLE_OP_OPS = "dispatched-op frequency share (fixed dispatched ops per sample)"
SAMPLE_FETCH = "fetch-sample frequency share (fixed fetch events per sample)"
TIME_MEANING = ("time share estimated from the gaps between per-sample timestamps (idle/wait "
                "is charged to the ending sample; the first sample per CPU is ignored, no gap "
                "before it; NULL sorts last)")
TAG_MEANING = ("share of tag-to-retire cycles summed over sampled ops (AMD tag at dispatch to "
               "retirement, queue/stalls included; 16-bit counter clipped at 65535); a total, "
               "not per-op latency")
CAVEAT = ("a raw share cannot separate a cheap operation run often from an expensive one run "
          "rarely")
READ_TAIL = "the <other ...> row is last, not ranked"

TAG_ABSENT = {
    "pmc": "this export has no tag-to-retire counter",
    "pmc-unsupported": "this export has no tag-to-retire counter",
    "pmc-118-unsampled": "this export has no tag-to-retire counter",
    "ibsfetch": "this export has no tag-to-retire counter",
    "ibs-op-tagless": "this dump has no IbsTagToRetCtr column",
    "ibs-op-empty-tag": ("IbsTagToRetCtr has no nonzero values in this dump, so every share is "
                         "NULL; the tag-to-retire counter comes from an IBS OP capture "
                         "(--event ibs-op), not from PMC or IBS fetch"),
}
TAG_ABSENT["ibs-op-zero-tag"] = TAG_ABSENT["ibs-op-empty-tag"]

# What each mode's legend must carry. Keeping it here makes the check an expected-output test:
# a wrong or missing meaning fails the comparison, not just a substring search.
MODES = {
    "pmc": {
        "sample": SAMPLE_PMC,
        "time": False,
        "tag": None,
        "order_by": ["sample_share", "samples"],
        "gap_weighted": False,
        "row_columns": ["process", "pid", "samples", "sample_share"],
        "metric": ["busy core-cycle share", "halted excluded"],
    },
    "pmc-unsupported": {
        "sample": SAMPLE_PMC_UNSUPPORTED,
        "time": False,
        "tag": None,
        "order_by": ["samples"],
        "gap_weighted": False,
        "row_columns": ["process", "pid", "samples", "sample_share"],
        "metric": ["not established", "not the supported cycle event"],
    },
    "pmc-118-unsampled": {
        "sample": SAMPLE_PMC_UNSUPPORTED,
        "time": False,
        "tag": None,
        "order_by": ["samples"],
        "gap_weighted": False,
        "row_columns": ["process", "pid", "samples", "sample_share"],
        "metric": ["not established", "isSampling=False", "not the supported cycle event"],
    },
    "ibs-op-cycles": {
        "sample": SAMPLE_OP_CYCLES,
        "time": True,
        "tag": TAG_MEANING,
        "order_by": ["sample_share", "samples"],
        "gap_weighted": False,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share",
                        "tag_to_retire_share"],
        "metric": ["core-cycle share", "cycle-controlled", "not a per-op cost"],
    },
    "ibs-op-ops": {
        "sample": SAMPLE_OP_OPS,
        "time": True,
        "tag": TAG_MEANING,
        "order_by": ["sample_share", "samples"],
        "gap_weighted": False,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share",
                        "tag_to_retire_share"],
        "metric": ["dispatched-op frequency share", "op-count", "not a per-op cost"],
    },
    "ibsfetch": {
        "sample": SAMPLE_FETCH,
        "time": True,
        "tag": None,
        "order_by": ["time_share", "samples"],
        "gap_weighted": True,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share"],
        "metric": ["fetch-sample frequency share", "unverified"],
    },
    "ibs-op-tagless": {
        "sample": SAMPLE_OP_OPS,
        "time": True,
        "tag": None,
        "order_by": ["sample_share", "samples"],
        "gap_weighted": False,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share"],
        "metric": ["dispatched-op frequency share"],
    },
    "ibs-op-empty-tag": {
        "sample": SAMPLE_OP_OPS,
        "time": True,
        "tag": None,
        "order_by": ["sample_share", "samples"],
        "gap_weighted": False,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share"],
        "metric": ["dispatched-op frequency share"],
    },
    "ibs-op-zero-tag": {
        "sample": SAMPLE_OP_OPS,
        "time": True,
        "tag": None,
        "order_by": ["sample_share", "samples"],
        "gap_weighted": False,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share"],
        "metric": ["dispatched-op frequency share"],
    },
}


def expected_columns(mode):
    spec = MODES[mode]
    parts = ["samples = raw count", f"sample_share = {spec['sample']}"]
    if spec["time"]:
        parts.append("time_share = time share")
    else:
        parts.append(f"time_share absent ({TIME_ABSENT_PMC})")
    reason = TAG_ABSENT.get(mode)
    if spec["tag"]:
        parts.append("tag_to_retire_share = tag-to-retire cycle share")
    elif reason:
        parts.append(f"tag_to_retire_share absent ({reason})")
    return "COLUMNS: share columns are fractions 0.0-1.0; " + "; ".join(parts)


def expected_read(mode):
    spec = MODES[mode]
    if spec["order_by"][0] == "time_share":
        read = "time_share ranks where time went; sample_share is not a per-op cost"
        if spec["tag"]:
            read += "; tag_to_retire_share is total tag-to-retire cycles (higher = more wait)"
        if spec["sample"].startswith(("dispatched-op", "fetch-sample")):
            read += f"; {CAVEAT}"
    elif spec["sample"].startswith("not established"):
        read = ("sample_share is not established for this event; rows are ordered by raw sample "
                "count only, not a time or impact claim")
    elif spec["sample"].startswith("busy core-cycle"):
        read = ("sample_share is the busy core-cycle share, so the sample order ranks where "
                "cycles were spent")
    else:
        read = f"sample_share orders the rows: {spec['sample']}"
        if spec["sample"].startswith(("dispatched-op", "fetch-sample")):
            read += f"; {CAVEAT}"
        if spec["tag"]:
            read += "; tag_to_retire_share is total tag-to-retire cycles (higher = more wait)"
    return f"READ: {read}; {READ_TAIL}"


def dump_row(tick_ns, cpu, pid, module, tag=None):
    row = f"{tick_ns},{cpu},{pid},{pid},4096,{module}"
    return row if tag is None else f"{row},{tag}"


class OutputLegendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def session(self, mode):
        """Write one synthetic capture once and return its directory."""
        session = self.root / mode
        if session.is_dir():
            return session
        session.mkdir()
        if mode.startswith("pmc"):
            self._write_pmc(session, mode)
        else:
            self._write_ibs(session, mode)
        return session

    def _write_pmc(self, session, mode):
        established = mode == "pmc"
        event = {
            "hwId": 118 if established or mode == "pmc-118-unsampled" else 99,
            "interval": 4_700_000,
            "isSampling": established,
            "user": True,
            "os": False,
            "encodedEventId": 0x03000076,
        }
        meta = {
            "collection": {
                "schedule": {"elapsed": 1000},
                "events": {"pmcEvents": {"pmcEventList": [event]}},
                "target": {"systemWide": True},
            },
            "auxiliary": {},
        }
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                               "name VARCHAR)")
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries (processId UINTEGER, threadId UINTEGER, "
                "moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT, coreId UINTEGER, "
                f'"{EVENT_COLUMN}" DOUBLE)')
            connection.execute(r"INSERT INTO Process VALUES (100, 'c:\workload.exe')")
            connection.execute(r"INSERT INTO Module VALUES (1, 'c:\workload.exe')")
            connection.execute("INSERT INTO Function VALUES (1, 1, 'main')")
            connection.execute(
                "INSERT INTO UnifiedSampleSeries VALUES (100, 1, 1, 1, 4096, 0, 5)")
        finally:
            connection.close()

    def _write_ibs(self, session, mode):
        fetch = mode == "ibsfetch"
        tagless = mode == "ibs-op-tagless"
        empty_tag = mode == "ibs-op-empty-tag"
        zero_tag = mode == "ibs-op-zero-tag"
        events = {
            "op": not fetch,
            "fetch": fetch,
            "opInterval": 1_000_000,
            "fetchInterval": 500_000,
            "opCycleCount": mode == "ibs-op-cycles",
        }
        meta = {
            "translateCommand": "AMDuProfCLI.exe translate -i session --ascii "
                                + ("ibsfetch-event-dump" if fetch else "ibsop-event-dump"),
            "collection": {
                "schedule": {"elapsed": 4000},
                "events": {"ibsEvents": events},
                "target": {"systemWide": True},
            },
            "auxiliary": {"ibsOpSampling": not fetch},
        }
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                               "name VARCHAR)")
            connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                               "functionId UBIGINT, moduleRVA UBIGINT)")
            connection.execute(r"INSERT INTO Process VALUES (100, 'c:\workload.exe')")
        finally:
            connection.close()
        header = "DeltaTick,CPU_Number,TID,PID,Offset,ModuleName"
        tagged = not fetch and not tagless
        if tagged:
            header += ",IbsTagToRetCtr"
        rows = [header]
        for index, tick in enumerate((1_000_000_000, 2_000_000_000, 3_000_000_000), start=1):
            if zero_tag:
                tag = 0
            elif empty_tag:
                tag = ""
            else:
                tag = index * 100 if tagged else None
            rows.append(dump_row(tick, 0, 100, "workload.exe", tag=tag))
        name = "IbsFetchDump.csv" if fetch else "IbsOpDump.csv"
        (session / name).write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")

    def _write_ordering(self, fetch=False, name=None):
        """Two processes whose time and sample orders differ: B has fewer samples, more weight."""
        session = self.root / (name or ("ordering-fetch" if fetch else "ordering"))
        session.mkdir()
        events = ({"op": False, "fetch": True, "fetchInterval": 500_000}
                  if fetch else
                  {"op": True, "fetch": False, "opInterval": 1_000_000, "opCycleCount": False})
        meta = {
            "translateCommand": "AMDuProfCLI.exe translate -i session --ascii "
                                + ("ibsfetch-event-dump" if fetch else "ibsop-event-dump"),
            "collection": {
                "schedule": {"elapsed": 5},
                "events": {"ibsEvents": events},
                "target": {"systemWide": True},
            },
            "auxiliary": {"ibsOpSampling": not fetch},
        }
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                               "name VARCHAR)")
            connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                               "functionId UBIGINT, moduleRVA UBIGINT)")
            connection.execute("INSERT INTO Process VALUES (100, 'a.exe'), (200, 'b.exe')")
        finally:
            connection.close()
        rows = [
            "DeltaTick,CPU_Number,TID,PID,Offset,ModuleName",
            dump_row(0, 0, 100, "a.exe"),
            dump_row(4_000_000, 0, 100, "a.exe"),
            dump_row(4_000_100, 0, 100, "a.exe"),
            dump_row(0, 1, 200, "b.exe"),
            dump_row(4_999_999, 1, 200, "b.exe"),
        ]
        dump = "IbsFetchDump.csv" if fetch else "IbsOpDump.csv"
        (session / dump).write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
        return session

    def _write_tag_clip(self):
        """Two processes, one sample per CPU: A's tag exceeds the 16-bit field maximum."""
        session = self.root / "tag-clip"
        session.mkdir()
        meta = {
            "translateCommand": "AMDuProfCLI.exe translate -i session --ascii ibsop-event-dump",
            "collection": {
                "schedule": {"elapsed": 5},
                "events": {"ibsEvents": {"op": True, "fetch": False, "opInterval": 1_000_000,
                                         "opCycleCount": False}},
                "target": {"systemWide": True},
            },
            "auxiliary": {"ibsOpSampling": True},
        }
        (session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                               "name VARCHAR)")
            connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                               "functionId UBIGINT, moduleRVA UBIGINT)")
            connection.execute("INSERT INTO Process VALUES (100, 'a.exe'), (200, 'b.exe'), "
                               "(300, 'c.exe'), (400, 'd.exe')")
        finally:
            connection.close()
        rows = [
            "DeltaTick,CPU_Number,TID,PID,Offset,ModuleName,IbsTagToRetCtr",
            dump_row(4_999_999, 0, 100, "a.exe", tag=200000),
            dump_row(4_999_999, 1, 200, "b.exe", tag=1000),
            # Not unsigned decimal integers, so NULL cells excluded from the denominator.
            dump_row(4_999_999, 2, 300, "c.exe", tag=-4),
            dump_row(4_999_999, 3, 400, "d.exe", tag="1e5"),
        ]
        (session / "IbsOpDump.csv").write_text("\n".join(rows) + "\n", encoding="utf-8",
                                                newline="\n")
        return session

    def profile(self, session, *extra, by="process"):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(session), "--by", by, *extra],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def run_next(self, command):
        """Run one emitted NEXT command through the shell, as an agent would.

        The leading bare "python" is replaced by the interpreter running the tests, which is
        the one known to have DuckDB; the rest of the command runs unchanged.
        """
        prefix = "python "
        self.assertTrue(command.startswith(prefix), command)
        interpreter = (f'"{sys.executable}"' if os.name == "nt"
                       else shlex.quote(sys.executable))
        return subprocess.run(interpreter + " " + command[len(prefix):], shell=True,
                              capture_output=True, text=True, encoding="utf-8")

    def test_markdown_legend_per_mode(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                output = self.profile(self.session(mode))
                lines = output.splitlines()
                self.assertIn(expected_columns(mode), lines)
                self.assertIn(expected_read(mode), lines)
                next_line = next(line for line in lines if line.startswith("NEXT: "))
                self.assertIn("--session", next_line)
                self.assertIn("--by module", next_line)
                self.assertNotIn("hit-site", next_line)
                self.assertNotIn("pct_", output)
                header = next(line for line in lines if line.startswith("| process"))
                self.assertEqual(
                    [cell.strip() for cell in header.strip("|").split("|")],
                    MODES[mode]["row_columns"])

    def test_json_legend_per_mode(self):
        for mode, spec in MODES.items():
            with self.subTest(mode=mode):
                document = json.loads(self.profile(self.session(mode), "--json"))
                legend = document["legend"]
                self.assertEqual(legend["share_unit"], "fraction")
                self.assertEqual(legend["share_scale"], 1)
                self.assertEqual(legend["columns"]["samples"], "raw sample count")
                self.assertEqual(legend["columns"]["sample_share"], spec["sample"])
                self.assertEqual(legend["impact"]["order_by"], spec["order_by"])
                self.assertEqual(legend["impact"]["gap_weighted"], spec["gap_weighted"])
                self.assertEqual(legend["remainder_last"], True)
                expected_denominators = {"sample_share": "total_samples"}
                if spec["time"]:
                    expected_denominators["time_share"] = "total_weight"
                if spec["tag"]:
                    expected_denominators["tag_to_retire_share"] = "total_tag_cycles"
                self.assertEqual(legend["denominators"], expected_denominators)
                if spec["time"]:
                    self.assertEqual(legend["columns"]["time_share"], TIME_MEANING)
                else:
                    self.assertNotIn("time_share", legend["columns"])
                    self.assertIn("time_share", legend["absent"])
                if spec["tag"]:
                    self.assertEqual(legend["columns"]["tag_to_retire_share"], spec["tag"])
                    self.assertNotIn("tag_to_retire_share", legend["absent"])
                else:
                    self.assertNotIn("tag_to_retire_share", legend["columns"])
                    self.assertIn("tag_to_retire_share", legend["absent"])
                if mode in TAG_ABSENT:
                    self.assertEqual(legend["absent"]["tag_to_retire_share"],
                                     TAG_ABSENT[mode])
                self.assertEqual(list(document["rows"][0]), spec["row_columns"])

    def test_metric_per_mode(self):
        for mode, spec in MODES.items():
            with self.subTest(mode=mode):
                output = self.profile(self.session(mode))
                metric = next(line for line in output.splitlines()
                              if line.startswith("METRIC: "))
                for fragment in spec["metric"]:
                    self.assertIn(fragment, metric)

    def test_unsupported_pmc_is_not_established(self):
        document = json.loads(self.profile(self.session("pmc-unsupported"), "--json"))
        legend = document["legend"]
        self.assertTrue(legend["columns"]["sample_share"].startswith("not established"))
        self.assertIn("ordered by raw sample count", legend["impact"]["estimate"])
        output = self.profile(self.session("pmc-unsupported"))
        self.assertIn(
            "READ: sample_share is not established for this event; rows are ordered by raw "
            "sample count only, not a time or impact claim", output)
        self.assertNotIn("time_share ranks", output)
        self.assertNotIn("hwId 118 sampling", output)

    def test_fraction_unit_scale_and_sum(self):
        session = self._write_ordering()
        document = json.loads(self.profile(session, "--top", "all", "--json"))
        legend = document["legend"]
        self.assertEqual(legend["share_unit"], "fraction")
        self.assertEqual(legend["share_scale"], 1)
        rows = document["rows"]
        self.assertEqual([row["samples"] for row in rows], [3, 2])
        sample_sum = sum(row["sample_share"] for row in rows)
        time_sum = sum(row["time_share"] for row in rows)
        self.assertAlmostEqual(sample_sum, 1.0, places=12)
        self.assertAlmostEqual(time_sum, 1.0, places=12)
        for row in rows:
            self.assertGreaterEqual(row["sample_share"], 0.0)
            self.assertLessEqual(row["sample_share"], 1.0)
            self.assertGreaterEqual(row["time_share"], 0.0)
            self.assertLessEqual(row["time_share"], 1.0)

    def test_ordering_matches_views_sql(self):
        session = self._write_ordering()
        document = json.loads(self.profile(session, "--top", "all", "--json"))
        output_order = [row["process"] for row in document["rows"]]
        self.assertEqual(output_order, ["a.exe", "b.exe"])
        variables = {
            "uprof_image": "%", "uprof_pid": "", "uprof_top": "0",
            "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
            "uprof_ibs_dump": str(session / "IbsOpDump.csv"),
            "uprof_event_column": "", "uprof_session_meta": str(session / "session.uprof"),
            "uprof_tag_available": "", "uprof_symbols": "", "uprof_aliases": "",
        }
        with uprof_db.open_query(session / "cpu.db", [TOOLS_DIR / "views.sql"],
                                 variables) as connection:
            view_order = [row[0] for row in connection.sql(
                "SELECT process FROM v_process_rows ORDER BY sort_key, rn NULLS LAST").fetchall()]
        self.assertEqual(view_order, output_order)
        # The time order is the reverse, so the sample ordering is doing real work.
        self.assertEqual([row["process"] for row in sorted(
            document["rows"], key=lambda row: row["time_share"], reverse=True)],
            ["b.exe", "a.exe"])

    def test_process_ordering_and_top_follow_the_gap_weighted_measure(self):
        session = self._write_ordering(fetch=True, name="ordering-fetch")
        document = json.loads(self.profile(session, "--top", "all", "--json"))
        self.assertEqual([row["process"] for row in document["rows"]],
                         ["b.exe", "a.exe"])
        self.assertEqual(document["legend"]["impact"]["primary_measure"], "time_share")
        self.assertEqual(document["legend"]["impact"]["order_by"],
                         ["time_share", "samples"])
        self.assertEqual(document["legend"]["impact"]["gap_weighted"], True)

        top = json.loads(self.profile(session, "--top", "1", "--json"))
        self.assertEqual(top["rows"][0]["process"], "b.exe")

    def test_module_ordering_and_top_follow_the_sample_measure(self):
        session = self._write_ordering(name="ordering-module-op")
        document = json.loads(self.profile(session, "--top", "all", "--json", by="module"))
        self.assertEqual([row["module"] for row in document["rows"]],
                         ["a.exe", "b.exe"])
        self.assertEqual(document["legend"]["impact"]["primary_measure"], "sample_share")

        top = json.loads(self.profile(session, "--top", "1", "--json", by="module"))
        self.assertEqual(top["rows"][0]["module"], "a.exe")
        self.assertEqual(top["rows"][-1]["module"], "<other modules>")

    def test_module_ordering_and_top_follow_the_gap_weighted_measure(self):
        session = self._write_ordering(fetch=True, name="ordering-module-fetch")
        document = json.loads(self.profile(session, "--top", "all", "--json", by="module"))
        self.assertEqual([row["module"] for row in document["rows"]],
                         ["b.exe", "a.exe"])
        self.assertEqual(document["legend"]["impact"]["primary_measure"], "time_share")

        top = json.loads(self.profile(session, "--top", "1", "--json", by="module"))
        self.assertEqual(top["rows"][0]["module"], "b.exe")
        self.assertEqual(top["rows"][-1]["module"], "<other modules>")

    def test_instruction_order_follows_the_primary_measure(self):
        session = self._write_ordering(name="instruction-order")

        def instruction_order(measure):
            variables = {
                "uprof_image": "%", "uprof_pid": "", "uprof_top": "0",
                "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
                "uprof_ibs_dump": str(session / "IbsOpDump.csv"),
                "uprof_event_column": "", "uprof_session_meta": str(session / "session.uprof"),
                "uprof_tag_available": "", "uprof_symbols": "", "uprof_aliases": "",
                "uprof_primary_measure": measure,
            }
            with uprof_db.open_query(session / "cpu.db", [TOOLS_DIR / "views.sql"],
                                     variables) as connection:
                return [row[0] for row in connection.sql(
                    "SELECT module FROM v_instruction_rows "
                    "ORDER BY sort_key, rn NULLS LAST").fetchall()]

        self.assertEqual(instruction_order("sample_share"), ["a.exe", "b.exe"])
        self.assertEqual(instruction_order("time_share"), ["b.exe", "a.exe"])

    def test_instruction_legend_carries_the_sample_hot_caveat(self):
        sample = json.loads(self.profile(
            self._write_ordering(name="instruction-legend-op"), "--json", by="instruction"))
        impact = sample["legend"]["impact"]
        self.assertEqual(impact["primary_measure"], "sample_share")
        self.assertEqual(impact["order_by"], ["sample_share", "samples"])
        self.assertIn("first row is the sample-hot instruction", impact["sample_hot"])
        self.assertIn("agrees with the function or region hot_rva", impact["sample_hot"])

        timed = json.loads(self.profile(
            self._write_ordering(fetch=True, name="instruction-legend-fetch"),
            "--json", by="instruction"))
        impact = timed["legend"]["impact"]
        self.assertEqual(impact["primary_measure"], "time_share")
        self.assertEqual(impact["order_by"], ["time_share", "samples"])
        self.assertIn("not sample-hot", impact["sample_hot"])
        self.assertIn("do not read its first row as the sample-hot RVA",
                      impact["sample_hot"])
        self.assertIn("no single instruction exceeds", impact["sample_hot"])

    def test_tagless_and_empty_tag_absence(self):
        for mode, reason in (("ibs-op-tagless", TAG_ABSENT["ibs-op-tagless"]),
                             ("ibs-op-empty-tag", TAG_ABSENT["ibs-op-empty-tag"]),
                             ("ibs-op-zero-tag", TAG_ABSENT["ibs-op-zero-tag"])):
            with self.subTest(mode=mode):
                output = self.profile(self.session(mode))
                legend = json.loads(self.profile(self.session(mode), "--json"))["legend"]
                self.assertEqual(legend["absent"]["tag_to_retire_share"], reason)
                self.assertIn(f"tag_to_retire_share absent ({reason})", output)

    def test_other_remainder_is_last(self):
        session = self._write_ordering()
        document = json.loads(self.profile(session, "--top", "1", "--json"))
        rows = document["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["process"], "<other processes>")
        self.assertIsNone(rows[-1]["pid"])
        self.assertEqual(document["legend"]["remainder_last"], True)
        output = self.profile(session, "--top", "1")
        self.assertIn(READ_TAIL, output)

    def test_next_calls_are_runnable_and_keep_scope(self):
        session = self._write_ordering()
        document = json.loads(self.profile(session, "--pid", "100", "--json"))
        calls = document["legend"]["next"]
        self.assertEqual(len(calls), 1)
        self.assertIn("--session", calls[0])
        self.assertIn("--pid 100", calls[0])
        self.assertIn("--by module", calls[0])
        self.assertNotIn("--by instruction", calls[0])
        self.assertFalse(any("hit-site" in call for call in calls))
        result = self.run_next(calls[0])
        self.assertEqual(result.returncode, 0, result.stderr)

        image_document = json.loads(self.profile(self.session("ibs-op-ops"),
                                                 "--image", "_:_workload.exe", "--json"))
        image_calls = image_document["legend"]["next"]
        self.assertTrue(all("--image _:_workload.exe" in call for call in image_calls))
        self.assertTrue(any("--by module" in call for call in image_calls))
        # The image-scope command must run as printed.
        result = self.run_next(image_calls[0])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoped coverage (--image", result.stdout)
        # A % pattern is not plain in every shell (cmd.exe expands %NAME%), so it is a
        # placeholder in the call and named as data.
        pattern_document = json.loads(self.profile(self.session("ibs-op-ops"),
                                                   "--image", "%workload%", "--json"))
        self.assertTrue(all("--image <IMAGE>" in call
                            for call in pattern_document["legend"]["next"]))
        self.assertEqual(pattern_document["legend"]["next_data"], {"IMAGE": "%workload%"})

        top_document = json.loads(self.profile(session, "--pid", "100", "--top", "1", "--json"))
        self.assertTrue(all("--top 1" in call for call in top_document["legend"]["next"]))
        default_document = json.loads(self.profile(session, "--pid", "100", "--json"))
        self.assertFalse(any("--top" in call for call in default_document["legend"]["next"]))

    def test_markdown_next_is_runnable(self):
        for mode in ("ibs-op-cycles", "ibs-op-ops"):
            with self.subTest(mode=mode):
                session = self.session(mode)
                output = self.profile(session, "--pid", "100", "--top", "2")
                line = next(line for line in output.splitlines()
                            if line.startswith("NEXT: "))
                text = line.removeprefix("NEXT: ")
                self.assertNotIn("; then", text)
                commands = text.split(" && ")
                self.assertGreaterEqual(len(commands), 1)
                for command in commands:
                    self.assertIn("--session", command)
                    self.assertIn("--pid 100", command)
                    self.assertIn("--top 2", command)
                    self.assertIn("--by", command)
                    result = self.run_next(command)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_next_runs_with_the_test_interpreter(self):
        # The emitted command starts with a bare "python"; the PATH one may lack DuckDB, so the
        # runnable checks must use the interpreter running the tests. A failing "python" that
        # comes first on PATH must not be the one that runs.
        fake_dir = self.root / "fake-python"
        fake_dir.mkdir()
        if os.name == "nt":
            (fake_dir / "python.cmd").write_text("@exit /b 3\r\n", encoding="ascii")
        else:
            fake = fake_dir / "python"
            fake.write_text("#!/bin/sh\nexit 3\n", encoding="ascii")
            fake.chmod(0o755)
        session = self._write_ordering()
        command = json.loads(self.profile(session, "--json"))["legend"]["next"][0]
        self.assertTrue(command.startswith("python "), command)
        with mock.patch.dict(os.environ,
                             {"PATH": str(fake_dir) + os.pathsep + os.environ["PATH"]}):
            result = self.run_next(command)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_attribution_definition_is_emitted(self):
        definition = "unique / derived (synthetic binary range) / unresolved (no symbol)"
        session = self._write_ordering()
        document = json.loads(self.profile(session, "--by", "function", "--json"))
        self.assertEqual(document["legend"]["columns"]["attribution"], definition)
        self.assertIn(f"attribution = {definition}",
                      self.profile(session, "--by", "function"))
        process = json.loads(self.profile(session, "--json"))
        self.assertNotIn("attribution", process["legend"]["columns"])

    def test_time_share_header_warns_about_few_samples(self):
        output = self.profile(self.session("ibs-op-ops"))
        row = next(line for line in output.splitlines()
                   if line.startswith("TIME_SHARE: "))
        self.assertIn("with few samples the share is coarse, so check sample_share", row)

    def test_tag_clip_at_field_maximum(self):
        session = self._write_tag_clip()
        document = json.loads(self.profile(session, "--top", "all", "--json"))
        rows = {row["process"]: row for row in document["rows"]}
        self.assertAlmostEqual(rows["a.exe"]["tag_to_retire_share"], 65535 / 66535, places=12)
        self.assertAlmostEqual(rows["b.exe"]["tag_to_retire_share"], 1000 / 66535, places=12)
        self.assertIsNone(rows["c.exe"]["tag_to_retire_share"])
        self.assertIsNone(rows["d.exe"]["tag_to_retire_share"])
        self.assertEqual(document["legend"]["columns"]["tag_to_retire_share"], TAG_MEANING)
        output = self.profile(session)
        self.assertIn("clipped at 65535", output)
        self.assertNotIn("131070", output)
        self.assertNotIn("131070", json.dumps(document))

    def test_all_zero_tag_column_is_reported_absent(self):
        session = self.session("ibs-op-zero-tag")
        document = json.loads(self.profile(session, "--top", "all", "--json"))
        self.assertNotIn("tag_to_retire_share", document["rows"][0])
        self.assertNotIn("tag_to_retire_share", document["legend"]["columns"])
        self.assertEqual(document["legend"]["absent"]["tag_to_retire_share"],
                         TAG_ABSENT["ibs-op-zero-tag"])
        self.assertIn("no nonzero values", document["legend"]["absent"]["tag_to_retire_share"])
        output = self.profile(session)
        self.assertIn("TAG_TO_RETIRE_SHARE", output)
        self.assertIn("no nonzero values", output)

    def test_json_legend_is_deterministic(self):
        session = self.session("ibs-op-ops")
        first = json.loads(self.profile(session, "--json"))
        second = json.loads(self.profile(session, "--json"))
        self.assertEqual(first["legend"], second["legend"])
        self.assertEqual(first["rows"], second["rows"])


if __name__ == "__main__":
    unittest.main()
