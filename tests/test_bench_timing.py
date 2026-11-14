"""The timing harness in bench/ measures the tools without touching the caller's caches.

bench/bench_timing.py is maintainer tooling for #19: it times the skill's commands in plain and
instrumented runs. These tests cover its own logic with a stand-in for uprof.main where the
tools' work does not matter, and one end-to-end run of the real tools on a committed
defect-lab capture.

Run from the repository root: python tests/run_tests.py test_bench_timing
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bench"))
import bench_timing

CAPTURE = REPO / "trials" / "defect_lab" / "captures" / "windows-x64-llvm-23.1.4" / "chain.timer"
CACHE_ENV = "UPROF_SESSION_CACHE_ROOT"


def fake_session(root: Path) -> Path:
    session = root / "session"
    session.mkdir()
    (session / "session.uprof").write_text("{}", encoding="utf-8")
    (session / "cpu.db").write_bytes(b"")
    return session


class FakeUprof(types.ModuleType):
    """Stands in for the tools' uprof module: records each call and the cache root it saw."""

    def __init__(self) -> None:
        super().__init__("uprof")
        self.calls: list[dict[str, object]] = []

    def main(self, argv: list[str]) -> int:
        self.calls.append({"argv": list(argv), "cache_root": os.environ.get(CACHE_ENV)})
        if "--out-file" in argv:
            Path(argv[argv.index("--out-file") + 1]).write_text("rows\n", encoding="utf-8")
        print("stdout line")
        print("stderr line", file=sys.stderr)
        return 0


@contextlib.contextmanager
def quiet():
    """Swallow the harness's own progress and report lines."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class StatisticsTest(unittest.TestCase):
    def test_p95_is_a_measured_value_by_nearest_rank(self):
        self.assertEqual(bench_timing._p95([float(v) for v in range(1, 21)]), 19.0)
        self.assertEqual(bench_timing._p95([3.0]), 3.0)
        self.assertEqual(bench_timing._p95([]), 0.0)

    def test_summary_skips_missing_values(self):
        summary = bench_timing._summarize([2.0, None, 1.0, 3.0])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["median_s"], 2.0)
        self.assertEqual((summary["min_s"], summary["max_s"]), (1.0, 3.0))
        self.assertEqual(bench_timing._summarize([None]), {"count": 0})

    def test_cpu_load_compares_two_system_time_samples(self):
        # Idle, kernel (which includes idle) and user ticks; 25 of 100 ticks were busy.
        samples = iter([(0, 0, 0), (75, 80, 20)])
        load = bench_timing._cpu_load_percent(sample=lambda: next(samples), sleep=lambda s: None)
        self.assertAlmostEqual(load, 25.0)
        self.assertIsNone(bench_timing._cpu_load_percent(sample=lambda: None, sleep=lambda s: None))


class CommandLabelTest(unittest.TestCase):
    def test_label_names_the_tool_and_its_first_two_markers(self):
        self.assertEqual(
            bench_timing._command_label(["C:/LLVM/bin/llvm-pdbutil.exe", "dump", "--publics", "x"]),
            "llvm-pdbutil.exe:dump,--publics",
        )
        self.assertEqual(
            bench_timing._command_label(
                ["AMDuProfCLI", "translate", "--ascii", "ibsop-event-dump", "-i", "s"]
            ),
            "AMDuProfCLI:translate,--ascii",
        )
        self.assertEqual(bench_timing._command_label([]), "?")


class DuckdbProxyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bench-proxy-")
        self.addCleanup(directory.cleanup)
        self.recorder = bench_timing.Recorder(Path(directory.name))
        restore = bench_timing.install_duckdb_proxy(self.recorder)
        self.addCleanup(restore)
        import duckdb

        self.connection = duckdb.connect()
        self.addCleanup(self.connection.close)

    def test_a_select_is_profiled_after_its_result_is_consumed(self):
        self.assertEqual(self.connection.sql("SELECT 42").fetchall(), [(42,)])
        self.connection.execute("SELECT * FROM range(10)").fetchall()
        self.assertEqual(len(self.recorder.statements), 2)
        for record in self.recorder.statements:
            self.assertEqual(record["statement_type"], "SELECT")
            self.assertIsNotNone(record["profile"])
            self.assertIsNotNone(record["wall_fetch_s"])

    def test_a_view_definition_is_timed_without_a_profile(self):
        self.connection.execute("CREATE TEMP VIEW v AS SELECT 1 AS one")
        (record,) = self.recorder.statements
        self.assertEqual(record["statement_type"], "CREATE")
        self.assertIsNone(record["profile"])

    def test_restore_puts_the_original_connect_back(self):
        import duckdb

        proxy = duckdb.connect
        bench_timing.install_duckdb_proxy(self.recorder)()
        self.assertIs(duckdb.connect, proxy)


class RunProbeTest(unittest.TestCase):
    def test_probe_times_run_and_run_watched_wherever_they_were_imported(self):
        def run(command, *args, **kwargs):
            return types.SimpleNamespace(returncode=0)

        def run_watched(command, **kwargs):
            return types.SimpleNamespace(returncode=3)

        native = types.ModuleType("uprof_native")
        native.run, native.run_watched = run, run_watched
        user = types.ModuleType("uprof_user")
        user.run, user.run_watched = run, run_watched
        recorder = bench_timing.Recorder(Path(tempfile.mkdtemp(prefix="bench-probe-")))
        with mock.patch.dict(sys.modules, {"uprof_native": native, "uprof_user": user}):
            restore = bench_timing.install_run_probe(recorder)
            try:
                user.run(["llvm-symbolizer", "--version"])
                user.run_watched(["AMDuProfCLI", "translate"], progress=None)
            finally:
                restore()
            self.assertIs(user.run, run)
            self.assertIs(native.run_watched, run_watched)
        labels = [(r["label"], r["returncode"]) for r in recorder.subprocesses]
        self.assertEqual(labels, [("llvm-symbolizer:--version", 0), ("AMDuProfCLI:translate", 3)])


class IsolationTest(unittest.TestCase):
    def test_caches_and_temp_point_below_the_root_and_are_restored(self):
        root = Path(tempfile.mkdtemp(prefix="bench-iso-"))
        before = {name: os.environ.get(name) for name in bench_timing.ISOLATED_VARS}
        tempdir_before = tempfile.tempdir
        with (
            mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": "srv*C:/sym"}),
            bench_timing.isolated_environment(root),
        ):
            for name in bench_timing.ISOLATED_VARS:
                self.assertTrue(Path(os.environ[name]).is_relative_to(root), name)
            self.assertTrue(Path(tempfile.gettempdir()).is_relative_to(root))
            # Symbol search is part of what a user's run costs, so it stays as configured.
            self.assertEqual(os.environ["_NT_SYMBOL_PATH"], "srv*C:/sym")
        self.assertEqual({n: os.environ.get(n) for n in bench_timing.ISOLATED_VARS}, before)
        self.assertEqual(tempfile.tempdir, tempdir_before)


class SessionIdentityTest(unittest.TestCase):
    def test_a_dump_in_the_tools_dump_cache_is_found(self):
        import uprof_dump

        root = Path(tempfile.mkdtemp(prefix="bench-identity-"))
        session = fake_session(root)
        with bench_timing.isolated_environment(root / "env"):
            self.assertIsNone(bench_timing._session_identity(session)["dump"])
            cache = uprof_dump.cache_dir(session)
            cache.mkdir(parents=True)
            (cache / "IbsOpDump.csv").write_text("h\n1\n2\n", encoding="utf-8")
            identity = bench_timing._session_identity(session)
        self.assertEqual(identity["dump_rows"], 2)
        self.assertFalse(identity["dump_beside_session"])


class SessionCacheModeTest(unittest.TestCase):
    def run_profile(self, mode: str, warmup: int) -> list[dict[str, object]]:
        root = Path(tempfile.mkdtemp(prefix="bench-mode-"))
        session = fake_session(root)
        args = bench_timing.build_parser().parse_args(
            ["--session", str(session), "--out", str(root / "r.json"), "--reps", "2"]
            + ["--instrumented-reps", "1", "--warmup", str(warmup)]
        )
        fake = FakeUprof()
        with mock.patch.dict(sys.modules, {"uprof": fake}), quiet():
            result = bench_timing.run_case("profile-process", mode, args, root / "work", session)
        self.assertTrue(result["plain"]["all_zero_exit"])
        self.assertEqual(result["session_cache"], mode)
        return fake.calls

    def test_cold_gives_every_timed_run_an_empty_cache(self):
        calls = self.run_profile("cold", warmup=1)
        roots = [call["cache_root"] for call in calls]
        self.assertEqual(len(roots), 4)  # warmup, two plain, one instrumented
        self.assertEqual(len(set(roots)), 4)

    def test_warm_shares_one_cache_filled_by_at_least_one_warmup(self):
        calls = self.run_profile("warm", warmup=0)
        roots = {call["cache_root"] for call in calls}
        self.assertEqual(len(calls), 4)  # the forced warmup, two plain, one instrumented
        self.assertEqual(len(roots), 1)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bench-main-"))
        self.session = fake_session(self.root)

    def main(self, *extra: str) -> int:
        fake = FakeUprof()
        with mock.patch.dict(sys.modules, {"uprof": fake}), quiet():
            return bench_timing.main(
                ["--session", str(self.session), "--cases", "profile-process", "--reps", "1"]
                + ["--instrumented-reps", "0", "--session-cache", "warm", *extra]
            )

    def test_results_inside_the_repository_are_refused(self):
        out = REPO / "bench" / "refused-results.json"
        self.assertEqual(self.main("--out", str(out)), 2)
        self.assertFalse(out.exists())

    def test_results_name_each_profile_case_with_its_cache_mode(self):
        out = self.root / "results" / "r.json"
        self.assertEqual(self.main("--out", str(out)), 0)
        results = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(list(results["cases"]), ["profile-process/warm"])
        self.assertTrue(out.with_suffix(".md").is_file())

    def test_keep_work_keeps_each_runs_console_output(self):
        out = self.root / "r.json"
        work = self.root / "work"
        self.assertEqual(self.main("--out", str(out), "--keep-work", str(work)), 0)
        stdout = sorted(work.rglob("plain-0.stdout"))
        stderr = sorted(work.rglob("plain-0.stderr"))
        self.assertEqual(len(stdout), 1)
        self.assertIn("stdout line", stdout[0].read_text(encoding="utf-8"))
        self.assertIn("stderr line", stderr[0].read_text(encoding="utf-8"))


@unittest.skipUnless((CAPTURE / "cpu.db").is_file(), "the defect-lab capture is not checked out")
class EndToEndTest(unittest.TestCase):
    def test_profile_process_on_a_committed_capture(self):
        root = Path(tempfile.mkdtemp(prefix="bench-e2e-"))
        out = root / "r.json"
        with quiet():
            code = bench_timing.main(
                ["--session", str(CAPTURE), "--out", str(out), "--cases", "profile-process"]
                + ["--reps", "1", "--instrumented-reps", "1"]
            )
        self.assertEqual(code, 0)
        results = json.loads(out.read_text(encoding="utf-8"))
        for mode in ("cold", "warm"):
            case = results["cases"][f"profile-process/{mode}"]
            self.assertTrue(case["plain"]["all_zero_exit"], case["plain"]["reps"])
            self.assertTrue(case["instrumented_matches_plain"])
            duck = case["instrumented"][0]["duckdb"]
            self.assertGreater(duck["profiled_statements"], 0)


if __name__ == "__main__":
    unittest.main()
