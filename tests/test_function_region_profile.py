"""`--by function` derives PE boundaries; `--by region` is the 4 KiB address-page fallback.

The fixture image supplies real exports and `.pdata` runtime-function starts, so the end-to-end
cases read the same LLVM text a capture does. The page cases pin alignment, cross-module grouping,
sampled_addresses and the hot RVA; the ordering cases pin the mode measure for --top.

Run from the repository root: python tests/run_tests.py test_function_region_profile
"""
# Spec: SPEC-56, SPEC-57, SPEC-58, SPEC-59, SPEC-60, SPEC-61, SPEC-62

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb
import uprof_context
import uprof_query
import uprof_session

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fixture.exe"
READOBJ = uprof_context.find_readobj()
SKIP = "llvm-readobj is required"
PAGE_MEANING = uprof_query._region_meaning(
    uprof_session.Sampler(ibs_dump_kind="ibsop-event-dump", interval_units="cycles")
)


def module_key(path):
    return str(path).lower().replace("\\", "/")


class Session:
    """One synthetic capture whose module rows point at real PE files."""

    def __init__(self, root, name, *, fetch=False):
        self.path = root / name
        self.path.mkdir()
        self.fetch = fetch
        self.modules = []
        self.functions = []
        self.unified = []
        self.rows = []
        self.tick = 0
        self.elapsed_ms = 10_000

    def module(self, module_id, path, system=False):
        self.modules.append((module_id, str(path), system))
        return self

    def function(self, function_id, module_id, name):
        self.functions.append((function_id, module_id, name))
        return self

    def sample(self, module_id, function_id, rva, count=1, gap_ns=100_000_000):
        for _ in range(count):
            self.tick += gap_ns
            self.rows.append((self.tick, rva, self.modules[module_id - 1][1]))
        if not any(row[0] == module_id and row[2] == rva for row in self.unified):
            self.unified.append((module_id, function_id, rva))
        return self

    def write(self):
        events = (
            {"op": False, "fetch": True, "fetchInterval": 500_000}
            if self.fetch
            else {"op": {"interval": 4_700_000}, "opInterval": 4_700_000, "opCycleCount": True}
        )
        meta = {
            "translateCommand": "AMDuProfCLI.exe translate -i session --ascii "
            + ("ibsfetch-event-dump" if self.fetch else "ibsop-event-dump"),
            "collection": {
                "schedule": {"elapsed": self.elapsed_ms},
                "events": {"ibsEvents": events},
                "target": {"systemWide": True},
            },
            "auxiliary": {"ibsOpSampling": not self.fetch},
        }
        (self.path / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        connection = duckdb.connect(str(self.path / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute(
                "CREATE TABLE Module (id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)"
            )
            connection.execute(
                "CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)"
            )
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                "functionId UBIGINT, moduleRVA UBIGINT)"
            )
            connection.execute("INSERT INTO Process VALUES (100, 'c:\\capture\\workload.exe')")
            connection.executemany("INSERT INTO Module VALUES (?, ?, ?)", self.modules)
            connection.executemany("INSERT INTO Function VALUES (?, ?, ?)", self.functions)
            connection.executemany("INSERT INTO UnifiedSampleSeries VALUES (?, ?, ?)", self.unified)
        finally:
            connection.close()
        name = "IbsFetchDump.csv" if self.fetch else "IbsOpDump.csv"
        with open(self.path / name, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["DeltaTick", "CPU_Number", "TID", "PID", "Offset", "ModuleName"])
            for tick, rva, module in self.rows:
                writer.writerow([tick, 0, 1, 100, rva, module])
        return self.path


@unittest.skipUnless(READOBJ, SKIP)
class FunctionRangeProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_profile(self, session, *extra, by="function"):
        path = session.path if isinstance(session, Session) else session
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(path),
                "--by",
                by,
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def fixture_session(self, name="derived"):
        session = Session(self.root, name)
        session.module(1, FIXTURE, False).function(10, 1, "")
        session.sample(1, 10, 0x101B, 3).sample(1, 10, 0x1018, 1)
        session.sample(1, 10, 0x1030, 2).sample(1, 10, 0x1050, 2)
        session.sample(1, 10, 0x1055, 1)
        return session.write()

    def document(self, session, *extra, by="function"):
        return json.loads(self.run_profile(session, "--json", *extra, by=by).stdout)

    def test_function_rows_carry_the_derived_range_and_the_synthetic_name(self):
        document = self.document(self.fixture_session())
        rows = {row["function"]: row for row in document["rows"]}

        entry = rows["fixture_entry"]
        self.assertEqual(entry["samples"], 4)
        self.assertEqual((entry["function_start"], entry["function_end"]), ("0x1000", "0x1030"))
        self.assertEqual(entry["function_source"], "export")
        self.assertFalse(entry["function_derived"])
        self.assertEqual(entry["attribution"], "unique")
        self.assertIsNone(entry["function_reason"])
        self.assertEqual((entry["hot_rva"], entry["hot_samples"]), ("0x101B", 3))
        self.assertEqual(entry["other_samples"], 1)
        self.assertAlmostEqual(entry["hot_sample_share"], 3 / 9)
        self.assertFalse(entry["isSystemModule"])
        self.assertIsNotNone(entry["cpu_seconds"])

        synthetic = rows["pdata_0x1050"]
        self.assertEqual(synthetic["samples"], 3)
        self.assertEqual(
            (synthetic["function_start"], synthetic["function_end"]), ("0x1050", "0x1062")
        )
        self.assertEqual(synthetic["function_source"], "pdata")
        self.assertTrue(synthetic["function_derived"])
        self.assertEqual(synthetic["attribution"], "derived")
        self.assertEqual((synthetic["hot_rva"], synthetic["hot_samples"]), ("0x1050", 2))
        self.assertEqual(synthetic["other_samples"], 1)

        self.assertEqual(rows["fixture_second"]["function_source"], "export")
        self.assertEqual(
            [row["function"] for row in document["rows"]],
            ["fixture_entry", "pdata_0x1050", "fixture_second"],
        )
        ranges = document["legend"]["function_ranges"]
        self.assertEqual((ranges["derived_modules"], ranges["ranges"]), (1, 3))
        self.assertEqual(ranges["absent_by_module"], {})

    def test_text_output_names_the_measure_and_the_range_source(self):
        result = self.run_profile(self.fixture_session())

        self.assertIn("sample_share orders the functions", result.stdout)
        self.assertIn(
            "attribution = unique / shared (folded names) / derived (synthetic binary "
            "range) / unresolved",
            result.stdout,
        )
        self.assertIn(
            "function_source = boundary source of the range: pdb_public, export, pdata",
            result.stdout,
        )
        self.assertIn("FUNCTION_RANGES: ", result.stderr)
        self.assertIn("HOT_RVA: ", result.stdout)
        self.assertIn(
            "other_samples 1 = the function's 4 samples minus hot_samples 3", result.stdout
        )
        # Every row is resolved, so the drill-down stops here and offers no region call; this
        # unscoped view of a system-wide capture names its process-scoped form and the ranking.
        next_lines = [line for line in result.stdout.splitlines() if line.startswith("NEXT: ")]
        self.assertEqual(len(next_lines), 1)
        self.assertIn("--by function --image %<EXE>", next_lines[0])
        self.assertIn("--by process", next_lines[0])
        self.assertNotIn("--by region", next_lines[0])

    def test_next_offers_region_only_when_a_function_is_unresolved(self):
        # Scoped to the process, so only the function stop rule decides NEXT.
        resolved = self.document(self.fixture_session("resolved-stop"), "--pid", "100")
        self.assertEqual(resolved["legend"]["next"], [])

        session = Session(self.root, "unresolved-stop")
        session.module(1, self.root / "missing.dll", False).function(10, 1, "")
        session.sample(1, 10, 0x2010, 2)
        calls = self.document(session.write(), "--pid", "100")["legend"]["next"]
        self.assertEqual(len(calls), 1)
        self.assertIn("--by region", calls[0])
        self.assertNotIn("--by instruction", calls[0])

    def test_coverage_and_agreement_split_named_derived_and_unresolved(self):
        result = self.run_profile(self.fixture_session())

        self.assertIn(
            "rows named 6/9 samples (66.7%), derived 3/9 samples (33.3%) from a "
            "synthetic binary range, unresolved 0/9 samples (0.0%) (<no symbol>)",
            result.stdout,
        )
        self.assertIn("every scoped sample is attributed below module/RVA", result.stdout)
        agreement = next(
            line for line in result.stdout.splitlines() if line.startswith("AGREEMENT: ")
        )
        self.assertIn("named 6 samples", agreement)
        self.assertIn("derived 3 samples", agreement)
        self.assertIn("unresolved 0 samples", agreement)
        self.assertNotIn("unresolved=", agreement)

        legend = self.document(self.fixture_session("derived-json"))["legend"]["symbol_coverage"]
        self.assertEqual(
            legend["profile_symbolizer"],
            {
                "source": "the emitted rows: llvm-symbolizer names plus derived PE ranges",
                "named_samples": 6,
                "derived_samples": 3,
                "resolved_samples": 9,
                "unresolved_samples": 0,
                "scoped_samples": 9,
            },
        )
        self.assertTrue(legend["rows_drillable"])

    def test_a_fully_named_module_skips_derivation_and_says_so(self):
        session = Session(self.root, "named")
        session.module(1, FIXTURE, False).function(10, 1, "fixture_entry")
        session.sample(1, 10, 0x101B, 2)
        session.write()

        document = self.document(session)
        self.assertEqual(
            document["rows"][0]["function_reason"],
            "fully named from the uProf DB; function-range derivation skipped",
        )
        self.assertEqual(document["legend"]["function_ranges"]["derived_modules"], 0)
        self.assertEqual(document["legend"]["function_ranges"]["absent_by_module"], {})

    def test_the_function_legend_has_the_full_range_object_and_no_region_key(self):
        legend = self.document(self.fixture_session())["legend"]

        self.assertNotIn("region", legend)
        self.assertEqual(
            list(legend["function_ranges"]),
            ["source", "derived_modules", "ranges", "absent_by_module", "fallback", "meaning"],
        )

    def test_the_region_legend_carries_its_own_reduced_range_object(self):
        legend = self.document(self.fixture_session(), by="region")["legend"]

        self.assertNotIn("function_ranges", legend)
        self.assertEqual(legend["region"]["function_ranges"], {"derived_modules": 1, "ranges": 3})
        self.assertIn("derived_function", legend["region"])
        self.assertEqual(legend["region"]["page_size"], 4096)

    def test_region_groups_by_page_per_module_and_keeps_the_hot_address(self):
        second = self.root / "second.exe"
        shutil.copyfile(FIXTURE, second)
        session = Session(self.root, "pages")
        session.module(1, FIXTURE, False).function(10, 1, "")
        session.module(2, second, False).function(20, 2, "")
        session.sample(1, 10, 0x101B, 2).sample(1, 10, 0x1018, 1)
        session.sample(1, 10, 0x201B, 1)
        session.sample(2, 20, 0x101B, 1)
        session.write()

        document = self.document(session, by="region")
        rows = document["rows"]
        self.assertEqual(
            [(row["module"], row["page_start"]) for row in rows],
            [
                (module_key(FIXTURE), "0x1000"),
                (module_key(second), "0x1000"),
                (module_key(FIXTURE), "0x2000"),
            ],
        )
        first = rows[0]
        self.assertEqual((first["page_end"], first["sampled_addresses"]), ("0x2000", 2))
        self.assertEqual((first["hot_rva"], first["hot_samples"]), ("0x101B", 2))
        self.assertEqual(first["function"], "fixture_entry")
        self.assertEqual(first["function_source"], "export")
        self.assertFalse(first["function_derived"])
        self.assertEqual(first["attribution"], "unique")

        outside = rows[2]
        self.assertEqual((outside["sampled_addresses"], outside["hot_rva"]), (1, "0x201B"))
        self.assertEqual(outside["function_source"], "none")
        self.assertFalse(outside["function_derived"])

    def test_region_legend_states_the_page_meaning_verbatim(self):
        session = Session(self.root, "legend")
        session.module(1, FIXTURE, False).function(10, 1, "")
        session.sample(1, 10, 0x101B, 2)
        session.write()

        document = self.document(session, by="region")
        legend = document["legend"]
        self.assertEqual(legend["columns"]["region"], PAGE_MEANING)
        self.assertEqual(legend["region"]["page_size"], 4096)
        self.assertEqual(legend["region"]["key"], "module + RVA aligned down to 4096")
        self.assertIn("derived_function", legend["region"])
        output = self.run_profile(session, by="region").stdout
        self.assertIn(PAGE_MEANING, output)

    def ordering_session(self, name, *, fetch, second_rva):
        session = Session(self.root, name, fetch=fetch)
        session.module(1, FIXTURE, False).function(10, 1, "")
        session.sample(1, 10, 0x101B, 2, gap_ns=100_000_000)
        session.sample(1, 10, second_rva, 1, gap_ns=800_000_000)
        return session.write()

    def test_region_ordering_and_top_follow_the_gap_weighted_measure(self):
        session = self.ordering_session("fetch-pages", fetch=True, second_rva=0x201B)

        document = self.document(session, "--top", "1", by="region")
        self.assertEqual(
            [(row["module"], row["page_start"]) for row in document["rows"]],
            [(module_key(FIXTURE), "0x2000"), ("<other regions>", None)],
        )
        self.assertEqual(document["legend"]["impact"]["primary_measure"], "time_share")
        self.assertEqual(document["legend"]["impact"]["order_by"], ["time_share", "samples"])

    def test_function_ordering_and_top_follow_the_gap_weighted_measure(self):
        session = self.ordering_session("fetch-functions", fetch=True, second_rva=0x1030)

        document = self.document(session, "--top", "1")
        self.assertEqual(
            [row["function"] for row in document["rows"]], ["fixture_second", "<other functions>"]
        )
        self.assertEqual(document["legend"]["impact"]["primary_measure"], "time_share")

        # The top row is time-hot, but HOT_RVA is the highest-sample address across all rows.
        output = self.run_profile(session, "--top", "1").stdout
        hot = next(line for line in output.splitlines() if line.startswith("HOT_RVA: "))
        self.assertIn("fixture_entry", hot)
        self.assertIn("0x101B", hot)
        self.assertIn("2 samples", hot)
        self.assertNotIn("fixture_second", hot)

    def test_ordering_and_top_follow_the_sample_measure(self):
        session = self.ordering_session("cycle-order", fetch=False, second_rva=0x201B)

        document = self.document(session, "--top", "1", by="region")
        self.assertEqual(
            [(row["module"], row["page_start"]) for row in document["rows"]],
            [(module_key(FIXTURE), "0x1000"), ("<other regions>", None)],
        )
        self.assertEqual(document["legend"]["impact"]["primary_measure"], "sample_share")

    def test_a_missing_module_is_explained_and_region_is_the_fallback(self):
        missing = str(self.root / "missing" / "ghost.dll")
        session = Session(self.root, "missing")
        session.module(1, missing, False).function(10, 1, "ghost_fn").function(11, 1, "")
        session.sample(1, 10, 0x1000, 2).sample(1, 11, 0x1100, 1)
        session.write()

        document = self.document(session)
        rows = {row["function"]: row for row in document["rows"]}
        self.assertEqual(rows["ghost_fn"]["attribution"], "unique")
        self.assertEqual(rows["ghost_fn"]["function_source"], "none")
        self.assertIn("no boundary source", rows["ghost_fn"]["function_reason"])
        self.assertEqual(rows["<unresolved>"]["attribution"], "unresolved")
        self.assertIsNone(rows["<unresolved>"]["function_start"])
        absent = document["legend"]["function_ranges"]["absent_by_module"]
        self.assertIn(module_key(missing), absent)
        self.assertIn("not on this host", absent[module_key(missing)])

        region = self.document(session, by="region")["rows"][0]
        self.assertIsNone(region["function"])
        self.assertEqual(region["function_source"], "none")
        self.assertEqual(region["attribution"], "unresolved")


if __name__ == "__main__":
    unittest.main()
