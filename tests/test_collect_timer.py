"""collect records a timer capture itself: system-wide, or with --launch for one app.

Where IBS and the cycle event are unavailable, a timer capture is the alternative. collect runs it
as `-e event=timer,interval=<ms>` (--sample-interval in whole milliseconds, default 1 ms) and
verifies the session records that timer. Attaching a timer capture to a PID records no samples on
Windows (measured elevated and unelevated), so --event timer --process is refused with the
system-wide call as the alternative; the caller narrows a system-wide capture with profile
--pid/--image. --launch starts one app under a timer capture. The failure alternatives for a timer
capture are uprof.py collect calls, not raw AMDuProfCLI commands.
Run from the repository root: python tests/run_tests.py test_collect_timer
"""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof
import uprof_collect
import uprof_native
import uprof_session
import uprof_shell
from uprof_util import UprofError

FIXTURE_META = Path(__file__).resolve().parent / "fixtures" / "timer" / "session.uprof"
APP = FIXTURE_META.with_name("heap_churn.exe")


def options(**overrides):
    values = {"out_dir": "out", "seconds": 3, "event": "timer", "dry_run": True}
    values.update(overrides)
    return uprof_collect.CollectOptions(**values)


def dry_run(opts):
    """collect with --dry-run: prints the lines, runs no capture."""
    out = io.StringIO()
    with mock.patch.object(uprof_collect, "find_uprof", return_value=None), \
            mock.patch.object(uprof_collect, "is_admin", return_value=False), \
            mock.patch.object(uprof_collect, "run", side_effect=AssertionError("no capture")), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        uprof_collect.collect(opts)
    return out.getvalue().splitlines()


def command_line(lines):
    return next(line for line in lines if line.startswith("COMMAND="))


class TimerCollectTest(unittest.TestCase):
    def test_the_parser_accepts_the_timer_event_and_a_launch(self):
        args = uprof.build_parser().parse_args(
            ["collect", "--out-dir", "out", "--event", "timer", "--launch", "app.exe", "-x", "1"])
        self.assertEqual(args.event, "timer")
        self.assertEqual(args.launch, ["app.exe", "-x", "1"])

    def test_a_system_wide_timer_capture_uses_the_timer_event(self):
        lines = dry_run(options())
        command = command_line(lines)
        self.assertIn(" collect -e event=timer,interval=1 -a ", command)
        self.assertIn("SAMPLE_INTERVAL=1ms", "\n".join(lines))

    def test_the_sample_interval_sets_the_timer_in_milliseconds(self):
        self.assertIn("event=timer,interval=10", command_line(dry_run(options(
            sample_interval="10ms"))))
        self.assertIn("event=timer,interval=2", command_line(dry_run(options(
            sample_interval="2000us"))))

    def test_a_sub_millisecond_interval_is_refused(self):
        with self.assertRaises(UprofError) as caught:
            dry_run(options(sample_interval="500us"))
        self.assertIn("whole milliseconds", str(caught.exception))

    def test_attaching_a_timer_capture_is_refused_with_the_system_wide_call(self):
        with self.assertRaises(UprofError) as caught:
            dry_run(options(pids=[42]))
        message = str(caught.exception)
        self.assertIn("records no samples", message)
        self.assertIn("collect --out-dir out --seconds 3 --event timer", message)
        self.assertIn("profile", message)

    def test_the_refusal_names_a_placeholder_out_dir_as_data(self):
        with self.assertRaises(UprofError) as caught:
            dry_run(options(out_dir="o$x", pids=[42]))
        message = str(caught.exception)
        self.assertIn("--out-dir <OUT_DIR>", message)
        self.assertIn("OUT_DIR=" + repr("o$x"), message.splitlines())

    def test_a_launch_puts_the_resolved_app_last(self):
        command = command_line(dry_run(options(launch=[str(APP), "-x", "1"])))
        self.assertTrue(command.endswith(f" {uprof_shell.quote(str(APP.resolve()))} -x 1"),
                        command)
        self.assertNotIn(" -a ", command)

    def test_a_launch_app_must_be_an_existing_file(self):
        for app in ("-o", "@resp.txt", "no_such_app.exe"):
            with self.subTest(app=app), self.assertRaises(UprofError) as caught:
                dry_run(options(launch=[app, "C:/x"]))
            self.assertIn("existing", str(caught.exception))

    def test_an_empty_launch_is_refused_not_turned_into_a_system_wide_capture(self):
        with self.assertRaises(UprofError) as caught:
            dry_run(options(launch=[]))
        self.assertIn("--launch", str(caught.exception))

    def test_the_command_line_quotes_or_names_the_launch_arguments(self):
        lines = dry_run(options(launch=[str(APP), "x y", "$(whoami)"]))
        command = command_line(lines)
        self.assertIn(' "x y" ', command + " ")
        self.assertNotIn("$(whoami)", command)
        self.assertIn("<APP_ARG_2>", command)
        self.assertIn("APP_ARG_2=" + repr("$(whoami)"), lines)

    def test_the_parser_keeps_an_empty_launch_distinct(self):
        args = uprof.build_parser().parse_args(["collect", "--out-dir", "o", "--event", "timer",
                                                "--launch"])
        self.assertEqual(args.launch, [])
        args = uprof.build_parser().parse_args(["collect", "--out-dir", "o"])
        self.assertIsNone(args.launch)

    def test_a_launch_needs_the_timer_event(self):
        with self.assertRaises(UprofError) as caught:
            dry_run(options(event="cycles", launch=[str(APP)]))
        self.assertIn("--event timer", str(caught.exception))

    def test_a_launch_cannot_also_attach(self):
        with self.assertRaises(UprofError):
            dry_run(options(pids=[42], launch=[str(APP)]))

    def test_the_cli_starts_the_app_after_the_options(self):
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "stop here")

        opts = options(dry_run=False, launch=[str(APP), "-x"])
        with mock.patch.object(uprof_collect, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            uprof_collect._run_cli("AMDuProfCLI", uprof_collect._event_args(opts, "timer", 1),
                                   uprof_collect._target_args(opts)[0], opts)
        self.assertEqual(calls[0][:4], ["AMDuProfCLI", "collect", "-e", "event=timer,interval=1"])
        self.assertEqual(calls[0][-2:], [str(APP.resolve()), "-x"])


class TimerAlternativesTest(unittest.TestCase):
    def test_both_timer_alternatives_are_uprof_collect_calls(self):
        with mock.patch.object(uprof_native, "find_uprof",
                               return_value=Path("C:/Program Files/AMD/AMDuProfCLI.exe")):
            message = str(uprof_native.failure("cause", ["timer capture",
                                                         "timer capture (launch)"]))
        lines = [line for line in message.splitlines() if line.startswith("ALTERNATIVE: ")]
        self.assertEqual(len(lines), 2)
        for line in lines:
            first = line.split(": ", 2)[2]
            self.assertTrue(first.startswith("python "), line)
            self.assertIn(" collect --out-dir <NEW_OUT_DIR> --seconds <S> --event timer", first)
            self.assertNotIn("AMDuProfCLI", first.split(";")[0])
        self.assertIn("--launch <APP>", lines[1])
        self.assertNotIn("--process", message)


class TimerVerifyTest(unittest.TestCase):
    def session(self, meta):
        temp = tempfile.TemporaryDirectory(prefix="uprof-timer-verify-")
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        return Path(temp.name)

    def fixture_meta(self):
        return json.loads(FIXTURE_META.read_text(encoding="utf-8"))

    def test_a_launched_timer_session_passes(self):
        meta = uprof_session.verify_collect(self.session(self.fixture_meta()), "timer", 1, "op",
                                            False, [])
        self.assertIn("timerInterval=1", uprof_session.collect_summary(meta, "timer"))

    def test_a_different_interval_fails(self):
        with self.assertRaises(UprofError) as caught:
            uprof_session.verify_collect(self.session(self.fixture_meta()), "timer", 10, "op",
                                         False, [])
        self.assertIn("timer interval", str(caught.exception))

    def test_a_session_without_a_timer_fails(self):
        meta = self.fixture_meta()
        meta["collection"]["events"]["timerInterval"] = 0
        with self.assertRaises(UprofError) as caught:
            uprof_session.verify_collect(self.session(meta), "timer", 1, "op", False, [])
        self.assertIn("timer", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
