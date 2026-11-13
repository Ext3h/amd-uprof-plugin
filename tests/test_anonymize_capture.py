"""anonymize_capture keeps the named processes of a capture and nothing that names the host.

A capture recorded on someone's machine names the machine, the user and every running process.
trials/anonymize_capture.py deletes every other process with its rows, rewrites host and user
paths, empties HostInfo, reduces session.uprof to the keys the tools read, and writes a plain
dump for review that it scans for the local machine name, user name and profile path itself.

The input is the trial builder's system-wide session with host details planted in it; the local
machine and user names are patched to the planted ones.

Run from the repository root: python tests/run_tests.py test_anonymize_capture
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import debug_fixtures as F

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "trials"))
import anonymize_capture  # noqa: E402
import build_captures  # noqa: E402

HOST, USER = "BUILD-HOST-7", "alice"
PROFILE = f"C:\\Users\\{USER}\\work"


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class AnonymizeCaptureTest(unittest.TestCase):
    def setUp(self):
        import duckdb

        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-anonymize-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.session = build_captures.build(root / "c", ["systemwide"])["systemwide"]
        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE HostInfo (id INTEGER, hostName VARCHAR)")
            connection.execute("INSERT INTO HostInfo VALUES (1, ?)", [HOST])
            connection.execute("UPDATE Process SET name = ? || '\\' || name", [PROFILE])
            connection.execute(
                "UPDATE Module SET path = ? || '\\fixture_full.exe' WHERE id = 1", [PROFILE]
            )
        finally:
            connection.close()
        meta = json.loads((self.session / "session.uprof").read_text(encoding="utf-8"))
        meta["hpc"] = {"hostName": HOST}
        meta["collectCommand"] = f"AMDuProfCLI.exe collect -a -o {PROFILE}\\out"
        meta["collection"]["schedule"]["hostDurations"] = [{"host": HOST, "start": 1, "end": 2}]
        meta["collection"]["target"]["workingDir"] = PROFILE
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        self.out = root / "out"

    def run_main(self, *args, names=(HOST, USER)):
        stderr = io.StringIO()
        with (
            mock.patch.object(anonymize_capture, "host_names", return_value=list(names)),
            contextlib.redirect_stderr(stderr),
        ):
            code = anonymize_capture.main(
                [
                    str(self.session),
                    "--keep-process",
                    "storage_test.exe",
                    "--out",
                    str(self.out),
                    *args,
                ]
            )
        return code, stderr.getvalue()

    def query(self, sql):
        import duckdb

        connection = duckdb.connect(str(self.out / "cpu.db"), read_only=True)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_only_the_kept_process_and_its_rows_remain(self):
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            self.query("SELECT id, name FROM Process"),
            [(build_captures.PID, "c:\\capture\\storage_test.exe")],
        )
        self.assertEqual(
            self.query("SELECT DISTINCT processId FROM UnifiedSampleSeries"),
            [(build_captures.PID,)],
        )
        modules = {path for (path,) in self.query("SELECT path FROM Module")}
        self.assertNotIn("node.exe", " ".join(modules))
        self.assertIn("c:\\capture\\fixture_full.exe", modules)
        self.assertEqual(self.query("SELECT count(*) FROM HostInfo"), [(0,)])

    def test_session_meta_keeps_the_keys_the_tools_read_and_no_host_value(self):
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        text = (self.out / "session.uprof").read_text(encoding="utf-8")
        meta = json.loads(text)
        self.assertIn("ibsEvents", meta["collection"]["events"])
        self.assertTrue(meta["collection"]["target"]["systemWide"])
        self.assertEqual(meta["collection"]["schedule"]["start"], 1_000_000_000)
        self.assertNotIn(HOST, text)
        self.assertNotIn(USER, text)
        self.assertNotIn("hpc", meta)

    def test_the_review_dump_lists_every_table_and_the_distinct_strings(self):
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        review = (self.out / "review.txt").read_text(encoding="utf-8")
        for table in ("Process", "Module", "Function", "UnifiedSampleSeries", "HostInfo"):
            self.assertIn(f"== table {table}", review)
        self.assertIn("== distinct strings", review)
        self.assertIn("c:\\capture\\storage_test.exe", review)
        self.assertNotIn(HOST, review)
        self.assertNotIn(PROFILE, review)

    def test_a_host_string_left_in_the_dump_fails_the_run(self):
        # A name that is not a path is not rewritten, so the scan must catch it.
        code, stderr = self.run_main(names=(HOST, "fixture_entry"))
        self.assertNotEqual(code, 0)
        self.assertIn("fixture_entry", stderr)
        self.assertIn("review.txt", stderr)

    def test_a_cpu_db_without_the_expected_schema_is_refused(self):
        import duckdb

        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("ALTER TABLE UnifiedSampleSeries DROP COLUMN moduleId")
        finally:
            connection.close()
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("UnifiedSampleSeries.moduleId", stderr)
        self.assertFalse((self.out / "cpu.db").exists())

    def change_schema(self, *statements):
        import duckdb

        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            for statement in statements:
                connection.execute(statement)
        finally:
            connection.close()

    def test_keys_and_foreign_keys_in_the_capture_do_not_stop_the_filter(self):
        # A schema with a primary key on Process and a foreign key from its threads must not
        # make deleting another process's rows fail.
        self.change_schema(
            "CREATE TABLE keyed (id UINTEGER PRIMARY KEY, name VARCHAR)",
            "INSERT INTO keyed SELECT id, name FROM Process",
            "DROP TABLE Process",
            "ALTER TABLE keyed RENAME TO Process",
            "CREATE TABLE ProcessThread (id UBIGINT, processId UINTEGER REFERENCES Process(id), "
            "threadId UINTEGER, threadName VARCHAR)",
            "INSERT INTO ProcessThread SELECT DISTINCT threadId, processId, threadId, "
            "'C:\\Users\\alice\\t' FROM UnifiedSampleSeries",
        )
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            self.query("SELECT DISTINCT processId FROM ProcessThread"), [(build_captures.PID,)]
        )

    def test_a_duckdb_failure_ends_in_one_cause_line(self):
        self.change_schema(
            "CREATE TABLE Odd (processId VARCHAR)", "INSERT INTO Odd VALUES ('not a number')"
        )
        code, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", stderr)
        lines = [line for line in stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, stderr)
        self.assertIn("Odd", lines[0])
        self.assertIn("DuckDB", lines[0])
        # A failed run leaves nothing that looks like a finished capture.
        self.assertEqual(
            [path.name for path in self.out.iterdir()] if self.out.exists() else [], []
        )

    def test_the_recording_script_fails_when_an_anonymization_fails(self):
        sys.path.insert(0, str(REPO / "trials" / "defect_lab"))
        import record

        failed = mock.Mock(returncode=1)
        with (
            mock.patch.object(record, "record", return_value=self.session),
            mock.patch.object(record.subprocess, "run", return_value=failed),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = record.main(
                [
                    str(self.out),
                    str(self.out / "raw"),
                    "--modes",
                    "timer",
                    "--classes",
                    "lock",
                    "--anonymize",
                    str(self.out / "anon"),
                ]
            )
        self.assertEqual(code, 1)

    def test_the_ibs_dump_is_handed_over_filtered_and_profile_reads_it(self):
        import csv
        import subprocess

        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        with open(self.out / "IbsOpDump.csv", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertEqual({row["PID"] for row in rows}, {str(build_captures.PID)})
        modules = {path for (path,) in self.query("SELECT path FROM Module")}
        self.assertLessEqual({row["ModuleName"] for row in rows}, modules)
        review = (self.out / "review.txt").read_text(encoding="utf-8")
        self.assertIn("== dump IbsOpDump.csv", review)
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"),
                "profile",
                "--session",
                str(self.out),
                "--by",
                "module",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("GENERATING DUMP", result.stderr)
        self.assertIn("c:/capture/fixture_full.exe", result.stdout)

    def test_a_host_string_in_the_dump_fails_the_run(self):
        dump = self.session / "IbsOpDump.csv"
        lines = dump.read_text(encoding="utf-8").splitlines()
        dump.write_text(
            "\n".join([lines[0] + ",Host"] + [f"{line},{HOST}" for line in lines[1:]]) + "\n",
            encoding="utf-8",
        )
        code, stderr = self.run_main()
        self.assertEqual(code, 1, stderr)
        self.assertIn(HOST, stderr)

    def test_ibs_captures_are_translated_with_their_ascii_dump(self):
        sys.path.insert(0, str(REPO / "trials" / "defect_lab"))
        import record

        with mock.patch.object(record.subprocess, "run") as run:
            record.translate(self.session, self.out / "t", "ibs")
            record.translate(self.session, self.out / "t", "timer")
        ibs, timer = (call.args[0] for call in run.call_args_list)
        self.assertIn("--ascii", ibs)
        self.assertEqual(ibs[ibs.index("--ascii") + 1], "ibsop-event-dump")
        self.assertNotIn("--ascii", timer)

    def test_the_capture_set_samples_coarsely_by_default_and_options_override_it(self):
        sys.path.insert(0, str(REPO / "trials" / "defect_lab"))
        import record

        def commands(settings):
            with mock.patch.object(record.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="SESSION=x\n", stderr="")
                for mode in ("timer", "ibs", "pmc"):
                    record.collect(mode, self.out / mode, settings)
            return [call.args[0] for call in run.call_args_list]

        timer, ibs, pmc = commands(record.Settings())
        self.assertIn("event=timer,interval=4", timer)
        self.assertEqual(timer[timer.index("-d") + 1], "2")
        self.assertEqual(ibs[ibs.index("--sample-interval") + 1], "16000000")
        self.assertEqual(ibs[ibs.index("--seconds") + 1], "2")
        self.assertEqual(pmc[pmc.index("--sample-interval") + 1], "4ms")
        self.assertEqual(record.Settings().stressors, 15)
        settings = record.settings_from(
            record.parser().parse_args(
                [
                    "b",
                    "r",
                    "--seconds",
                    "1",
                    "--ibs-interval",
                    "4000000",
                    "--timer-ms",
                    "10",
                    "--stressors",
                    "8",
                ]
            )
        )
        timer, ibs, _pmc = commands(settings)
        self.assertIn("event=timer,interval=10", timer)
        self.assertEqual(ibs[ibs.index("--sample-interval") + 1], "4000000")
        self.assertEqual(ibs[ibs.index("--seconds") + 1], "1")
        self.assertEqual(settings.stressors, 8)

    def write_callstacks(self):
        import duckdb

        connection = duckdb.connect(str(self.session / "callstack.db"))
        try:
            connection.execute(
                "CREATE TABLE CallstackSample (startTime UBIGINT, processId "
                "UINTEGER, threadId UINTEGER, callstackId UBIGINT)"
            )
            connection.execute(
                "CREATE TABLE CallstackFrame (callstackId UBIGINT, functionId "
                'UBIGINT, depth UINTEGER, "offset" UBIGINT)'
            )
            connection.execute("CREATE TABLE Srcfile (id UINTEGER, name VARCHAR)")
            connection.execute(
                "INSERT INTO CallstackSample VALUES (1, ?, 11, 7), (2, ?, 77, 8)",
                [build_captures.PID, build_captures.OTHER_PID],
            )
            connection.execute(
                "INSERT INTO CallstackFrame VALUES (7, 100, 0, 16), (7, 101, 1, 4), (8, 102, 0, 32)"
            )
            connection.execute("INSERT INTO Srcfile VALUES (1, ?)", [PROFILE + "\\main.c"])
        finally:
            connection.close()

    def test_call_stacks_are_handed_over_filtered_like_cpu_db(self):
        import duckdb

        self.write_callstacks()
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        connection = duckdb.connect(str(self.out / "callstack.db"), read_only=True)
        try:
            samples = connection.execute(
                "SELECT processId, callstackId FROM CallstackSample"
            ).fetchall()
            frames = connection.execute(
                "SELECT callstackId, functionId FROM CallstackFrame ORDER BY ALL"
            ).fetchall()
            files = connection.execute("SELECT name FROM Srcfile").fetchall()
        finally:
            connection.close()
        self.assertEqual(samples, [(build_captures.PID, 7)])
        self.assertEqual(frames, [(7, 100), (7, 101)])
        self.assertEqual(files, [("c:\\capture\\main.c",)])
        review = (self.out / "review.txt").read_text(encoding="utf-8")
        self.assertIn("== callstack.db", review)
        self.assertIn("== table CallstackFrame (2 rows)", review)

    def test_functions_the_kept_call_stacks_reach_stay_in_cpu_db(self):
        import duckdb

        # A frame may name a function in a module the kept processes never sampled.
        connection = duckdb.connect(str(self.session / "cpu.db"), read_only=True)
        try:
            (function,) = connection.execute(
                "SELECT f.id FROM Function f WHERE f.moduleId = 5"
            ).fetchone()
        finally:
            connection.close()
        self.write_callstacks()
        connection = duckdb.connect(str(self.session / "callstack.db"))
        try:
            connection.execute("INSERT INTO CallstackFrame VALUES (7, ?, 2, 8)", [function])
        finally:
            connection.close()
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.query(f"SELECT count(*) FROM Function WHERE id = {function}"), [(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM Module WHERE id = 5"), [(1,)])

    def test_a_missing_callstack_db_is_noted(self):
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        self.assertFalse((self.out / "callstack.db").exists())
        self.assertIn("callstack.db: absent", (self.out / "review.txt").read_text(encoding="utf-8"))

    def test_commands_lose_symbol_servers_and_every_embedded_drive_path(self):
        command = (
            "AMDuProfCLI.exe translate -i C:\\cap --symbol-cache-dir D:\\Sym "
            "--symbol-server srv*C:\\SymCache\\Ms*https://symbols.example.invalid/ms "
            "--symbol-server=SRV*C:\\SymCache\\Game*http://symbols.example.invalid/game "
            "--symbol-path=E:\\pdbs;F:\\more --ascii ibsop-event-dump"
        )
        cleaned = anonymize_capture.sanitize_command(command)
        for leaked in (
            "SymCache",
            "example.invalid",
            "C:\\",
            "D:\\",
            "E:\\",
            "F:\\",
            "srv*",
            "SRV*",
        ):
            self.assertNotIn(leaked, cleaned)
        self.assertEqual(cleaned.count("--symbol-server"), 2)
        self.assertIn("--ascii ibsop-event-dump", cleaned)
        self.assertIn("translate", cleaned)

        meta = json.loads((self.session / "session.uprof").read_text(encoding="utf-8"))
        meta["translateCommand"] = command
        meta["collectCommand"] = "AMDuProfCLI.exe collect -o out --symbol-server srv*C:\\S*x"
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        code, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        text = (self.out / "session.uprof").read_text(encoding="utf-8")
        self.assertNotIn("SymCache", text)
        self.assertNotIn("example.invalid", text)
        self.assertNotIn("C:\\\\", text)

    def test_call_graph_captures_pass_the_call_graph_option(self):
        sys.path.insert(0, str(REPO / "trials" / "defect_lab"))
        import record

        settings = record.Settings()
        timer = record.callgraph_command("timer-cg", self.out / "t", settings)
        ibs = record.callgraph_command("ibs-cg", self.out / "i", settings)
        for command in (timer, ibs):
            self.assertEqual(command[command.index("--call-graph") + 1], "1:64:user:fpo")
            self.assertIn("-a", command)
        self.assertIn("event=timer,interval=4", timer)
        self.assertIn("event=ibs-op,ibsop-count-control=1,interval=16000000", ibs)
        self.assertIn("timer-cg", record.MODES)
        self.assertIn("ibs-cg", record.MODES)

    def test_the_overhead_notes_compare_the_iteration_rates(self):
        sys.path.insert(0, str(REPO / "trials" / "defect_lab"))
        import record

        self.assertEqual(record.ns_per_iteration("class ns/iteration: 34\n"), 34)
        self.assertIsNone(record.ns_per_iteration("nothing printed"))
        notes = record.overhead_notes("lock", "timer-cg", 34, 51)
        self.assertIn("without call graph: 34 ns", notes)
        self.assertIn("with call graph: 51 ns", notes)
        self.assertIn("1.50", notes)
        self.assertIn("not measured", record.overhead_notes("lock", "ibs-cg", 34, None))

    def test_call_graph_modes_need_an_elevated_shell(self):
        sys.path.insert(0, str(REPO / "trials" / "defect_lab"))
        import record

        stderr = io.StringIO()
        with (
            mock.patch.object(record, "is_elevated", return_value=False),
            mock.patch.object(record, "record") as recorded,
            mock.patch.object(record, "record_callgraph") as callgraph,
            contextlib.redirect_stderr(stderr),
        ):
            code = record.main([str(self.out), str(self.out / "raw"), "--modes", "timer,timer-cg"])
        self.assertEqual(code, 2)
        recorded.assert_not_called()
        callgraph.assert_not_called()
        self.assertIn("elevated", stderr.getvalue())
        self.assertIn("driver", stderr.getvalue())

    def test_an_unknown_process_is_refused(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = anonymize_capture.main(
                [str(self.session), "--keep-process", "absent.exe", "--out", str(self.out)]
            )
        self.assertEqual(code, 2)
        self.assertIn("absent.exe", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
