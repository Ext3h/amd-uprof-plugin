"""Function ranges derived from a PE image: boundaries, ends, source priority and names.

`profile --by function` groups unresolved samples by these ranges, so the consumer never maps
`.pdata` by hand. The checked-in fixture supplies the real LLVM text formats; the unit cases pin
the boundary rule without a toolchain.

Run from the repository root: python tests/run_tests.py test_function_ranges
"""
# Spec: SPEC-56, SPEC-57

import io
import os
import sys
import tempfile
import unittest
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest import mock

import uprof_context
import uprof_functions
import uprof_query
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

    def submit(self, fn, module_name, module_path, starts, readobj):
        if self.fail_submit is not None:
            raise self.fail_submit
        if module_name == self.crash:
            return FakeFuture(error=BrokenProcessPool("worker died"))
        return FakeFuture(fn(module_name, module_path, starts, readobj))


def fake_pool(created, **kwargs):
    return lambda max_workers=None: FakePool(created, max_workers=max_workers, **kwargs)


def derive_sentinel(module_name, _module_path, _starts=(), readobj=None):
    return uprof_functions.RangeDerivation(
        [uprof_functions.FunctionRange(module_name, 0x1000, 0x1010, "pdata", None)])


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
            original_path = list(sys.path)
            sys.path.insert(0, str(shadow))
            try:
                results = uprof_query._derive_candidates(
                    [("fixture.exe", str(fixture)), ("second.exe", str(second))],
                    {}, READOBJ)
            finally:
                sys.path[:] = original_path

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.ranges for result in results))
        self.assertTrue(all(result.reason is None for result in results))


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

            def submit(self, fn, module_name, module_path, starts, readobj):
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


class ReadobjResolutionTest(unittest.TestCase):
    def test_readobj_is_resolved_only_when_the_pool_is_used(self):
        with mock.patch.object(uprof_query.os, "cpu_count", return_value=8), \
                mock.patch.object(uprof_query.uprof_context, "find_readobj",
                                  return_value="tool") as find:
            self.assertIsNone(uprof_query._readobj_for(1))
            self.assertEqual(find.call_count, 0)
            self.assertEqual(uprof_query._readobj_for(2), "tool")
            self.assertEqual(find.call_count, 1)


class SerialDerivationTest(unittest.TestCase):
    def test_a_serial_derivation_exception_is_reported_per_module(self):
        with tempfile.TemporaryDirectory(prefix="uprof-serial-derive-") as tmp:
            image = Path(tmp) / "a.exe"
            image.write_bytes(b"MZ")
            stream = io.StringIO()
            connection = mock.Mock()
            connection.execute.return_value.fetchall.return_value = []
            with mock.patch.object(uprof_query.uprof_db, "fetch",
                                   return_value=([], [("a.exe", str(image), 1, 2)])), \
                    mock.patch.object(uprof_query, "_pool_workers", return_value=None), \
                    mock.patch.object(uprof_functions, "derive_module_ranges",
                                      side_effect=OSError("boom")):
                path, info = uprof_query._derive_scope_ranges(connection, None, stream)

        self.assertIsNone(path)
        self.assertIn("range derivation failed", info["absent_by_module"]["a.exe"])
        self.assertIn("WARNING: no function range derived for a.exe", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
