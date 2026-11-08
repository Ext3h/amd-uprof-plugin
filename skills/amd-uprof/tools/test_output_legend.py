"""The profile output contract: measure-first share columns, fractions and a short legend.

Seven synthetic captures cover the modes whose `sample_share` means different things: PMC
cycles, an unsupported PMC event, IBS OP cycle-controlled, IBS OP op-count, IBS fetch, IBS OP
without a tag column and IBS OP with an all-blank tag column. Two more fixtures check the row
order, the remainder row and the runnable NEXT calls. No capture or external tool is used.

Run from this directory: python -m unittest test_output_legend -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

import uprof_db

TOOLS_DIR = Path(__file__).resolve().parent
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
        "order_by": ["samples"],
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
        "order_by": ["time_share", "samples"],
        "gap_weighted": True,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share",
                        "tag_to_retire_share"],
        "metric": ["core-cycle share", "cycle-controlled", "not a per-op cost"],
    },
    "ibs-op-ops": {
        "sample": SAMPLE_OP_OPS,
        "time": True,
        "tag": TAG_MEANING,
        "order_by": ["time_share", "samples"],
        "gap_weighted": True,
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
        "order_by": ["time_share", "samples"],
        "gap_weighted": True,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share"],
        "metric": ["dispatched-op frequency share"],
    },
    "ibs-op-empty-tag": {
        "sample": SAMPLE_OP_OPS,
        "time": True,
        "tag": None,
        "order_by": ["time_share", "samples"],
        "gap_weighted": True,
        "row_columns": ["process", "pid", "samples", "sample_share", "time_share"],
        "metric": ["dispatched-op frequency share"],
    },
    "ibs-op-zero-tag": {
        "sample": SAMPLE_OP_OPS,
        "time": True,
        "tag": None,
        "order_by": ["time_share", "samples"],
        "gap_weighted": True,
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
    if spec["gap_weighted"]:
        read = "time_share ranks where time went; sample_share is not a per-op cost"
        if spec["tag"]:
            read += "; tag_to_retire_share is total tag-to-retire cycles (higher = more wait)"
        if spec["sample"].startswith(("dispatched-op", "fetch-sample")):
            read += f"; {CAVEAT}"
    elif spec["sample"].startswith("not established"):
        read = ("sample_share is not established for this event; rows are ordered by raw sample "
                "count only, not a time or impact claim")
    else:
        read = ("sample_share is the busy core-cycle share, so the sample order ranks where "
                "cycles were spent")
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

    def _write_ordering(self):
        """Two processes whose time and sample orders differ: B has fewer samples, more weight."""
        session = self.root / "ordering"
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
        (session / "IbsOpDump.csv").write_text("\n".join(rows) + "\n", encoding="utf-8",
                                                newline="\n")
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

    def profile(self, session, *extra):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(session), "--by", "process", *extra],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

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
        self.assertIn("not a time or impact claim", legend["impact"]["estimate"])
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
        self.assertEqual([row["samples"] for row in rows], [2, 3])
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
        self.assertEqual(output_order, ["b.exe", "a.exe"])
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
        # The sample order is the reverse, so the time ordering is doing real work.
        self.assertEqual([row["process"] for row in sorted(
            document["rows"], key=lambda row: row["samples"], reverse=True)], ["a.exe", "b.exe"])

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
        self.assertTrue(calls)
        self.assertTrue(all("--session" in call for call in calls))
        self.assertTrue(all("--pid 100" in call for call in calls))
        self.assertTrue(any("--by module" in call for call in calls))
        self.assertIn("--by instruction", calls[-1])
        self.assertTrue(calls[-1].endswith("--json"))
        self.assertFalse(any("hit-site" in call for call in calls))
        result = subprocess.run(calls[0], shell=True, capture_output=True, text=True,
                                encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

        image_document = json.loads(self.profile(self.session("ibs-op-ops"),
                                                 "--image", "%workload%", "--json"))
        image_calls = image_document["legend"]["next"]
        self.assertTrue(all("--image" in call for call in image_calls))
        expected_pattern = "%%workload%%" if os.name == "nt" else "%workload%"
        self.assertTrue(all(expected_pattern in call for call in image_calls))
        self.assertTrue(any("--by module" in call for call in image_calls))
        # The image-scope command must run, including the escaped pattern.
        result = subprocess.run(image_calls[0], shell=True, capture_output=True, text=True,
                                encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scoped coverage (--image", result.stdout)

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
                self.assertGreaterEqual(len(commands), 2)
                self.assertTrue(commands[-1].endswith("--json"))
                for command in commands:
                    self.assertIn("--session", command)
                    self.assertIn("--pid 100", command)
                    self.assertIn("--top 2", command)
                    self.assertIn("--by", command)
                    result = subprocess.run(command, shell=True, capture_output=True,
                                            text=True, encoding="utf-8")
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_attribution_definition_is_emitted(self):
        definition = "unique / shared (folded names) / unresolved (no symbol)"
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
