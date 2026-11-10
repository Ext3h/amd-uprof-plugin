"""Function ranges derived from a PE image: boundaries, ends, source priority and names.

`profile --by function` groups unresolved samples by these ranges, so the consumer never maps
`.pdata` by hand. The checked-in fixture supplies the real LLVM text formats; the unit cases pin
the boundary rule without a toolchain.

Run from the repository root: python tests/run_tests.py test_function_ranges
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_context
import uprof_functions
import uprof_query
import uprof_symbols
from uprof_context import PeSection

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fixture.exe"
READOBJ = uprof_context.find_readobj()
TEXT = PeSection(0x1000, 0x62, True)
DATA = PeSection(0x3000, 0x40, False)


class BoundaryTest(unittest.TestCase):
    def test_export_wins_at_one_rva_and_each_range_ends_at_the_next_start(self):
        ranges = uprof_functions.build_ranges(
            "m.dll", [TEXT],
            public_starts=[(0x1020, "public_fn")],
            pdata_starts=[(0x1000, None), (0x1030, None), (0x1050, None)],
            export_starts=[(0x1000, "entry"), (0x1030, "second")])

        self.assertEqual(
            [(r.start, r.end, r.source, r.name) for r in ranges],
            [(0x1000, 0x1020, "export", "entry"),
             (0x1020, 0x1030, "pdb_public", "public_fn"),
             (0x1030, 0x1050, "export", "second"),
             (0x1050, 0x1062, "pdata", None)])

    def test_a_pdb_public_beats_a_pdata_start_at_the_same_rva(self):
        ranges = uprof_functions.build_ranges(
            "m.dll", [TEXT], public_starts=[(0x1000, "public_fn")],
            pdata_starts=[(0x1000, None)], export_starts=[(0x1050, "tail")])

        self.assertEqual([(r.source, r.name) for r in ranges],
                         [("pdb_public", "public_fn"), ("export", "tail")])

    def test_a_code_section_without_a_boundary_yields_no_range(self):
        ranges = uprof_functions.build_ranges("m.dll", [TEXT])

        self.assertEqual(ranges, [])

    def test_non_code_sections_and_outside_starts_are_ignored(self):
        ranges = uprof_functions.build_ranges(
            "m.dll", [TEXT, DATA], pdata_starts=[(0x3000, None), (0x9000, None)])

        self.assertEqual(ranges, [])

    def test_a_folded_public_uses_its_first_name(self):
        starts = uprof_functions.public_starts_from_names(
            [(0x1000, "first | second"), (0x2000, ""), (0x3000, "  spaced  ")])

        self.assertEqual(starts, [(0x1000, "first"), (0x2000, None), (0x3000, "spaced")])

    def test_csv_carries_the_range_columns_with_lf_newlines(self):
        ranges = uprof_functions.build_ranges(
            "m.dll", [TEXT], pdata_starts=[(0x1000, None), (0x1030, "named")])
        with tempfile.TemporaryDirectory() as tmp:
            path = uprof_functions.write_ranges_csv(Path(tmp) / "functions.csv", ranges)
            text = path.read_bytes()

        self.assertFalse(text.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", text)
        lines = text.decode("utf-8").split("\n")
        self.assertEqual(lines[0], "module_name,function_start,function_end,function_source,"
                                   "function_name")
        self.assertEqual(lines[1], "m.dll,4096,4144,pdata,")
        self.assertEqual(lines[2], "m.dll,4144,4194,pdata,named")


@unittest.skipUnless(READOBJ, "llvm-readobj is required")
class FixtureDerivationTest(unittest.TestCase):
    def setUp(self):
        # A private cache root makes every run a miss, so parse_image is always exercised even on
        # a host whose warm cache already holds this fixture.
        self.cache = tempfile.TemporaryDirectory(prefix="uprof-ranges-")
        self.addCleanup(self.cache.cleanup)
        patcher = mock.patch.object(uprof_symbols, "ARTIFACT_CACHE_ROOT", Path(self.cache.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fixture_pdata_exports_and_section_end_define_the_ranges(self):
        result = uprof_functions.derive_module_ranges("fixture.exe", FIXTURE)

        self.assertIsNone(result.reason)
        self.assertEqual(
            [(r.start, r.end, r.source, r.name) for r in result.ranges],
            [(0x1000, 0x1030, "export", "fixture_entry"),
             (0x1030, 0x1050, "export", "fixture_second"),
             (0x1050, 0x1062, "pdata", None)])

    def test_missing_readobj_reports_the_reason_instead_of_failing(self):
        with mock.patch.object(uprof_context, "find_readobj", return_value=None):
            result = uprof_functions.derive_module_ranges("m.dll", FIXTURE)

        self.assertEqual(result.ranges, [])
        self.assertEqual(result.reason, "readobj_missing")

    def test_a_malformed_ranges_entry_is_deleted_when_recompute_stores_nothing(self):
        uprof_functions.derive_module_ranges("fixture.exe", FIXTURE)
        entry = next((Path(self.cache.name) / "v1" / "ranges").rglob("meta.json")).parent
        ranges_path = entry / "ranges.json"
        ranges_path.write_text('{"ranges": "bad"}', encoding="utf-8")
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        meta["files"]["ranges.json"] = {
            "size": ranges_path.stat().st_size,
            "sha256": uprof_symbols.artifact_file_digest(ranges_path)}
        (entry / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with mock.patch.object(uprof_functions, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = uprof_functions.derive_module_ranges("fixture.exe", FIXTURE)

        self.assertIsNotNone(result.reason)
        self.assertFalse(entry.exists())


@unittest.skipUnless(READOBJ, "llvm-readobj is required")
class DeriveCandidatesTest(unittest.TestCase):
    def test_parallel_results_keep_module_order_and_fall_back_for_a_missing_module(self):
        with tempfile.TemporaryDirectory(prefix="uprof-derive-pool-") as tmp:
            root = Path(tmp)
            fixture = root / "fixture.exe"
            fixture.write_bytes(FIXTURE.read_bytes())
            missing = root / "missing.exe"
            candidates = [("fixture.exe", str(fixture)), ("missing.exe", str(missing))]

            results = uprof_query._derive_candidates(candidates, {}, READOBJ)

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].ranges)
        self.assertEqual(results[1].ranges, [])
        self.assertIsNotNone(results[1].reason)


class DeriveCandidatesPoolTest(unittest.TestCase):
    def test_the_pool_derives_each_candidate_and_keeps_submission_order(self):
        candidates = [("a.dll", "a"), ("b.dll", "b")]
        created = []

        class FakeFuture:
            def __init__(self, value):
                self.value = value

            def result(self):
                return self.value

        class FakePool:
            def __init__(self, max_workers=None):
                created.append(max_workers)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def submit(self, fn, module_name, module_path, starts, readobj):
                return FakeFuture(fn(module_name, module_path, starts, readobj))

        def derive(module_name, _module_path, _starts=(), _readobj=None):
            return uprof_functions.RangeDerivation(
                [uprof_functions.FunctionRange(module_name, 0x1000, 0x1010, "pdata", None)])

        with mock.patch.object(uprof_query.uprof_functions, "derive_module_ranges",
                               side_effect=derive), \
                mock.patch.object(uprof_query, "ProcessPoolExecutor", FakePool):
            results = uprof_query._derive_candidates(candidates, {}, "readobj")

        self.assertEqual(len(created), 1)
        self.assertEqual([r.ranges[0].module_name for r in results], ["a.dll", "b.dll"])


if __name__ == "__main__":
    unittest.main()
