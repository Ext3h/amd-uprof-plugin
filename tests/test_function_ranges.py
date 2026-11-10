"""Function ranges derived from a PE image: boundaries, ends, source priority and names.

`profile --by function` groups unresolved samples by these ranges, so the consumer never maps
`.pdata` by hand. The checked-in fixture supplies the real LLVM text formats; the unit cases pin
the boundary rule without a toolchain.

Run from the repository root: python tests/run_tests.py test_function_ranges
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures.process import BrokenProcessPool
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


class FakeFuture:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


class FakePool:
    def __init__(self, created, max_workers=None, crash=None, fail_start=None,
                 fail_submit=None):
        self.created = created
        self.created.append(max_workers)
        self.crash = crash
        self.fail_start = fail_start
        self.fail_submit = fail_submit

    def __enter__(self):
        if self.fail_start is not None:
            raise self.fail_start
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, module_name, module_path, starts, readobj, cache_key):
        if self.fail_submit is not None:
            raise self.fail_submit
        if module_name == self.crash:
            return FakeFuture(error=BrokenProcessPool("worker died"))
        return FakeFuture(fn(module_name, module_path, starts, readobj, cache_key))


def fake_pool(created, **kwargs):
    return lambda max_workers=None: FakePool(created, max_workers=max_workers, **kwargs)


def derive_sentinel(module_name, _module_path, _starts=(), readobj=None, cache_key=None):
    return uprof_functions.RangeDerivation(
        [uprof_functions.FunctionRange(module_name, 0x1000, 0x1010, "pdata", None)])


def _real_cache_entries():
    root = uprof_symbols._cache_root("ranges")
    return len(list(root.rglob("meta.json"))) if root is not None and root.exists() else 0


@unittest.skipUnless(READOBJ, "llvm-readobj is required")
class DeriveCandidatesTest(unittest.TestCase):
    def test_parallel_results_keep_module_order_and_fall_back_for_a_missing_module(self):
        with tempfile.TemporaryDirectory(prefix="uprof-derive-pool-") as tmp:
            root = Path(tmp)
            fixture = root / "fixture.exe"
            fixture.write_bytes(FIXTURE.read_bytes())
            missing = root / "missing.exe"
            cache = root / "cache"
            cache.mkdir()
            candidates = [("fixture.exe", str(fixture)), ("missing.exe", str(missing))]
            before = _real_cache_entries()

            with mock.patch.object(uprof_symbols, "ARTIFACT_CACHE_ROOT", cache), \
                    mock.patch.dict(os.environ,
                                    {"UPROF_ARTIFACT_CACHE_ROOT": str(cache)}):
                results = uprof_query._derive_candidates(candidates, {}, READOBJ)

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].ranges)
        self.assertEqual(results[1].ranges, [])
        self.assertIsNotNone(results[1].reason)
        self.assertEqual(before, _real_cache_entries())


@unittest.skipUnless(READOBJ, "llvm-readobj is required")
class PoolCrashTest(unittest.TestCase):
    def test_a_worker_that_dies_after_the_pool_starts_falls_back_serially(self):
        with tempfile.TemporaryDirectory(prefix="uprof-pool-crash-") as tmp:
            root = Path(tmp)
            # The child imports the callable's module by name; shadowing it makes the worker
            # exit after the pool has started, which is the BrokenProcessPool path.
            shadow = root / "shadow"
            shadow.mkdir()
            (shadow / "uprof_functions.py").write_text(
                "import os\n\n\ndef derive_module_ranges(*args, **kwargs):\n"
                "    os._exit(3)\n", encoding="utf-8")
            fixture = root / "fixture.exe"
            fixture.write_bytes(FIXTURE.read_bytes())
            second = root / "second.exe"
            second.write_bytes(FIXTURE.read_bytes())
            cache = root / "cache"
            cache.mkdir()
            before = _real_cache_entries()
            original_path = list(sys.path)
            sys.path.insert(0, str(shadow))
            try:
                with mock.patch.object(uprof_symbols, "ARTIFACT_CACHE_ROOT", cache), \
                        mock.patch.dict(os.environ,
                                        {"UPROF_ARTIFACT_CACHE_ROOT": str(cache)}):
                    results = uprof_query._derive_candidates(
                        [("fixture.exe", str(fixture)), ("second.exe", str(second))],
                        {}, READOBJ)
            finally:
                sys.path[:] = original_path

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.ranges for result in results))
        self.assertTrue(all(result.reason is None for result in results))
        self.assertEqual(before, _real_cache_entries())


class DeriveCandidatesPoolTest(unittest.TestCase):
    def test_the_pool_derives_each_candidate_and_keeps_submission_order(self):
        candidates = [("a.dll", "a"), ("b.dll", "b")]
        created = []
        with mock.patch.object(uprof_query.uprof_functions, "derive_module_ranges",
                               side_effect=derive_sentinel), \
                mock.patch.object(uprof_query, "ProcessPoolExecutor", fake_pool(created)):
            results = uprof_query._derive_candidates(candidates, {}, "readobj")

        self.assertEqual(created, [2])
        self.assertEqual([r.ranges[0].module_name for r in results], ["a.dll", "b.dll"])

    def test_the_worker_cap_bounds_cores_and_module_count(self):
        for cpus, count, expected in ((32, 20, 8), (3, 20, 3), (16, 2, 2)):
            created = []
            with mock.patch.object(uprof_query.os, "cpu_count", return_value=cpus), \
                    mock.patch.object(uprof_query.uprof_functions, "derive_module_ranges",
                                      side_effect=derive_sentinel), \
                    mock.patch.object(uprof_query, "ProcessPoolExecutor", fake_pool(created)):
                uprof_query._derive_candidates(
                    [(f"m{index}.dll", "p") for index in range(count)], {}, "readobj")
            self.assertEqual(created, [expected])

    def test_one_cpu_or_one_module_uses_the_serial_loop(self):
        for cpus, count in ((1, 4), (16, 1)):
            created = []
            with mock.patch.object(uprof_query.os, "cpu_count", return_value=cpus), \
                    mock.patch.object(uprof_query.uprof_functions, "derive_module_ranges",
                                      side_effect=derive_sentinel), \
                    mock.patch.object(uprof_query, "ProcessPoolExecutor", fake_pool(created)):
                uprof_query._derive_candidates(
                    [(f"m{index}.dll", "p") for index in range(count)], {}, "readobj")
            self.assertEqual(created, [])

    def test_a_pool_that_cannot_start_falls_back_to_the_serial_loop(self):
        candidates = [("a.dll", "p"), ("b.dll", "p")]
        calls = []

        def derive(name, *args, **kwargs):
            calls.append(name)
            return derive_sentinel(name, None)

        with mock.patch.object(uprof_query.uprof_functions, "derive_module_ranges",
                               side_effect=derive), \
                mock.patch.object(uprof_query, "ProcessPoolExecutor",
                                  fake_pool([], fail_start=OSError("no spawn"))):
            results = uprof_query._derive_candidates(candidates, {}, "readobj")

        self.assertEqual(calls, ["a.dll", "b.dll"])
        self.assertTrue(all(result.ranges for result in results))

    def test_a_submit_failure_falls_back_to_the_serial_loop(self):
        candidates = [("a.dll", "p"), ("b.dll", "p")]
        calls = []

        def derive(name, *args, **kwargs):
            calls.append(name)
            return derive_sentinel(name, None)

        with mock.patch.object(uprof_query.uprof_functions, "derive_module_ranges",
                               side_effect=derive), \
                mock.patch.object(uprof_query, "ProcessPoolExecutor",
                                  fake_pool([], fail_submit=RuntimeError("broken"))):
            results = uprof_query._derive_candidates(candidates, {}, "readobj")

        self.assertEqual(calls, ["a.dll", "b.dll"])
        self.assertTrue(all(result.ranges for result in results))

    def test_a_worker_exception_is_reported_in_module_order(self):
        class CrashingPool:
            def __init__(self, max_workers=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def submit(self, fn, module_name, module_path, starts, readobj, cache_key):
                if module_name == "b.dll":
                    return FakeFuture(error=ValueError("bad module"))
                return FakeFuture(derive_sentinel(module_name, module_path))

        with mock.patch.object(uprof_query, "ProcessPoolExecutor", CrashingPool):
            results = uprof_query._derive_candidates(
                [("a.dll", "p"), ("b.dll", "p")], {}, "readobj")

        self.assertEqual(results[0].ranges[0].module_name, "a.dll")
        self.assertIsInstance(results[1], ValueError)

    def test_a_broken_pool_reruns_every_module_serially(self):
        calls = []

        def derive(name, *args, **kwargs):
            calls.append(name)
            return derive_sentinel(name, None)

        with mock.patch.object(uprof_query.uprof_functions, "derive_module_ranges",
                               side_effect=derive), \
                mock.patch.object(uprof_query, "ProcessPoolExecutor",
                                  fake_pool([], crash="a.dll")):
            results = uprof_query._derive_candidates(
                [("a.dll", "p"), ("b.dll", "p")], {}, "readobj")

        self.assertEqual(calls[-2:], ["a.dll", "b.dll"])
        self.assertTrue(all(result.ranges for result in results))

    def test_a_base_exception_during_the_pool_propagates(self):
        class InterruptingPool:
            def __init__(self, max_workers=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def submit(self, *args):
                return FakeFuture(error=KeyboardInterrupt())

        with mock.patch.object(uprof_query, "ProcessPoolExecutor", InterruptingPool):
            with self.assertRaises(KeyboardInterrupt):
                uprof_query._derive_candidates([("a.dll", "p"), ("b.dll", "p")], {},
                                               "readobj")


class _FakeRows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, publics):
        self.publics = publics

    def execute(self, _sql, _params=None):
        return _FakeRows(self.publics)


@unittest.skipUnless(READOBJ, "llvm-readobj is required")
class AssembledRangesCacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="uprof-assembled-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        patcher = mock.patch.object(uprof_symbols, "ARTIFACT_CACHE_ROOT", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, run_name, publics):
        artifacts = self.root / run_name
        artifacts.mkdir(exist_ok=True)
        stream = io.StringIO()
        with mock.patch.object(uprof_query.uprof_db, "fetch",
                               return_value=([], [("fixture.exe", str(FIXTURE), 1, 2)])), \
                mock.patch.object(uprof_query.uprof_context, "find_readobj",
                                  return_value=READOBJ):
            path, info = uprof_query._derive_scope_ranges(
                _FakeConnection(publics), artifacts, stream)
        return path, info, stream.getvalue()

    def _run_rows(self, run_name, rows, publics):
        artifacts = self.root / run_name
        artifacts.mkdir(exist_ok=True)
        stream = io.StringIO()
        with mock.patch.object(uprof_query.uprof_db, "fetch", return_value=([], rows)), \
                mock.patch.object(uprof_query.uprof_context, "find_readobj",
                                  return_value=READOBJ):
            path, info = uprof_query._derive_scope_ranges(
                _FakeConnection(publics), artifacts, stream)
        return path, info, stream.getvalue()

    @staticmethod
    def _derivation(name):
        return uprof_functions.RangeDerivation([
            uprof_functions.FunctionRange(name, 0x1000, 0x1030, "pdata", name)])

    def _entries(self):
        return list((self.cache / "v1" / "ranges-assembled").rglob("meta.json"))

    def test_the_second_assembly_is_staged_without_deriving(self):
        first = self._run("run1", [("fixture.exe", 0x1000, "fixture_entry")])
        with mock.patch.object(uprof_query, "_derive_candidates",
                               side_effect=AssertionError("re-derived")):
            second = self._run("run2", [("fixture.exe", 0x1000, "fixture_entry")])

        self.assertEqual(first[0].read_bytes(), second[0].read_bytes())
        self.assertEqual(first[2], second[2])
        self.assertEqual(first[1], second[1])
        entry = next((self.cache / "v1" / "ranges-assembled").rglob("meta.json")).parent
        self.assertTrue(os.path.samefile(second[0], entry / "functions.csv"))
        shutil.rmtree(entry)
        self.assertEqual(second[0].read_bytes(), first[0].read_bytes())

    def test_changed_public_starts_reassemble(self):
        original = uprof_query._derive_candidates
        calls = []

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with mock.patch.object(uprof_query, "_derive_candidates", side_effect=counting):
            self._run("run1", [("fixture.exe", 0x1000, "first_name")])
            self._run("run2", [("fixture.exe", 0x1000, "second_name")])

        self.assertEqual(len(calls), 2)

    def test_a_transient_derivation_failure_is_not_cached(self):
        rows = [("a.exe", str(FIXTURE), 1, 2), ("b.exe", str(FIXTURE), 1, 2)]
        publics = [("a.exe", 0x1000, "a"), ("b.exe", 0x1000, "b")]
        with mock.patch.object(uprof_query, "_derive_candidates",
                               return_value=[self._derivation("a.exe"),
                                             RuntimeError("readobj timed out")]):
            _path, info, _text = self._run_rows("run1", rows, publics)
        self.assertIn("range derivation failed", info["absent_by_module"]["b.exe"])
        self.assertEqual(self._entries(), [])

        with mock.patch.object(uprof_query, "_derive_candidates",
                               return_value=[self._derivation("a.exe"),
                                             self._derivation("b.exe")]):
            _path, info, _text = self._run_rows("run2", rows, publics)
        self.assertNotIn("b.exe", info["absent_by_module"])
        self.assertEqual(len(self._entries()), 1)

    def test_a_cached_transient_failure_is_rebuilt(self):
        rows = [("a.exe", str(FIXTURE), 1, 2), ("b.exe", str(FIXTURE), 1, 2)]
        publics = [("a.exe", 0x1000, "a"), ("b.exe", 0x1000, "b")]
        with mock.patch.object(uprof_query, "_derive_candidates",
                               return_value=[self._derivation("a.exe"),
                                             self._derivation("b.exe")]):
            self._run_rows("run1", rows, publics)
        entry = self._entries()[0].parent
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        meta["info"]["absent_by_module"]["b.exe"] = "readobj_failed: timed out"
        (entry / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        with mock.patch.object(uprof_query, "_derive_candidates",
                               return_value=[self._derivation("a.exe"),
                                             self._derivation("b.exe")]) as derive:
            _path, info, _text = self._run_rows("run2", rows, publics)

        self.assertEqual(derive.call_count, 1)
        self.assertNotIn("b.exe", info["absent_by_module"])
        healed = json.loads(self._entries()[0].read_text(encoding="utf-8"))
        self.assertNotIn("b.exe", healed["info"]["absent_by_module"])

    def test_a_changed_attempted_row_set_reassembles(self):
        rows = [("a.exe", str(FIXTURE), 1, 2)]
        publics = [("a.exe", 0x1000, "a")]
        with mock.patch.object(uprof_query, "_derive_candidates",
                               return_value=[self._derivation("a.exe")]):
            self._run_rows("run1", rows, publics)

        with mock.patch.object(uprof_query, "_derive_candidates",
                               return_value=[self._derivation("a.exe")]) as derive:
            _path, info, _text = self._run_rows(
                "run2", rows + [("ghost.exe", "", 1, 2)], publics)

        self.assertEqual(derive.call_count, 1)
        self.assertIn("ghost.exe", info["absent_by_module"])

    def test_a_layout_version_change_reassembles(self):
        rows = [("a.exe", str(FIXTURE), 1, 2)]
        publics = [("a.exe", 0x1000, "a")]
        with mock.patch.object(uprof_query, "_derive_candidates",
                               return_value=[self._derivation("a.exe")]):
            self._run_rows("run1", rows, publics)

        with mock.patch.object(uprof_query.uprof_functions, "ASSEMBLED_RANGES_LAYOUT", 2), \
                mock.patch.object(uprof_query, "_derive_candidates",
                                  return_value=[self._derivation("a.exe")]) as derive:
            self._run_rows("run2", rows, publics)

        self.assertEqual(derive.call_count, 1)

    def test_a_staging_failure_keeps_the_cached_entry(self):
        rows = [("a.exe", str(FIXTURE), 1, 2)]
        publics = [("a.exe", 0x1000, "a")]
        with mock.patch.object(uprof_query, "_derive_candidates",
                               return_value=[self._derivation("a.exe")]):
            self._run_rows("run1", rows, publics)
        self.assertEqual(len(self._entries()), 1)

        with mock.patch.object(uprof_symbols, "stage_cached_artifact",
                               side_effect=OSError("no space")), \
                mock.patch.object(uprof_query, "_derive_candidates", return_value=[]):
            path, _info, _text = self._run_rows("run2", rows, publics)

        self.assertIsNone(path)
        self.assertEqual(len(self._entries()), 1)


if __name__ == "__main__":
    unittest.main()
