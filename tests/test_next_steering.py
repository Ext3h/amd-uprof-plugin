"""NEXT steers an unscoped system-wide rollup to a process scope, and nothing else to it.

The first profile call depends on the question: an attached capture goes straight to the
module or function lens. Only an unscoped module, function or region rollup of a system-wide
capture mixes every process, so its NEXT names the same view scoped to the user's process
(`--image %<EXE>`) first and the process ranking second, for a question that names no process;
a --pid/--image or --function scope, line and instruction drill-downs, an attached capture and
the process view itself keep their calls.
Run from the repository root: python tests/run_tests.py test_next_steering
"""
# Spec: SPEC-77

import unittest

import uprof
import uprof_query

SESSION = "C:/captures/run" if uprof_query.os.name == "nt" else "/captures/run"


def next_calls(*argv, system_wide, unresolved=0):
    args = uprof.build_parser().parse_args(["profile", "--session", SESSION, *argv])
    return uprof_query._next_calls(args, {"unresolved": unresolved}, system_wide=system_wide)


def argv_of(call):
    """The profile arguments of an emitted call, the way the next call would be parsed."""
    return call.split(" profile ", 1)[1].replace('"', "").split()[2:]


def bys(calls):
    return [call.split("--by ")[1].split()[0] for call in calls]


class NextSteeringTest(unittest.TestCase):
    def test_an_unscoped_system_wide_rollup_names_its_process_scoped_form_first(self):
        # A question that names the process reaches the scoped view without the process
        # ranking (#384); the ranking stays for a question that names none.
        for by in ("module", "function", "region"):
            with self.subTest(by=by):
                calls = next_calls("--by", by, system_wide=True)
                self.assertEqual(bys(calls), [by, "process"])
                self.assertIn(f"--by {by} --image %<EXE>", calls[0])

    def test_following_the_process_scoped_form_never_returns_to_the_ranking(self):
        # module -> module --image %<EXE> -> function --image %<EXE>; the caller fills <EXE>.
        calls = next_calls("--by", "module", system_wide=True)
        argv = [value.replace("<EXE>", "app.exe") for value in argv_of(calls[0])]
        calls = next_calls(*argv, system_wide=True)
        self.assertEqual(bys(calls), ["function"])
        self.assertIn("--image %app.exe", calls[0])

    def test_line_and_instruction_drill_downs_keep_their_calls(self):
        self.assertEqual(bys(next_calls("--by", "line", system_wide=True)), ["instruction"])
        self.assertEqual(bys(next_calls("--by", "instruction", system_wide=True)), [])

    def test_a_scoped_system_wide_view_keeps_its_drill_down(self):
        for scope in (["--pid", "100"], ["--image", "app.exe"]):
            with self.subTest(scope=scope):
                self.assertEqual(
                    bys(next_calls("--by", "module", *scope, system_wide=True)), ["function"]
                )

    def test_a_function_scoped_view_is_not_sent_back_to_processes(self):
        calls = next_calls(
            "--by", "line", "--function", "main", "--module", "app.exe", system_wide=True
        )
        self.assertNotIn("process", bys(calls))

    def test_an_attached_capture_keeps_its_drill_down(self):
        self.assertEqual(bys(next_calls("--by", "module", system_wide=False)), ["function"])
        self.assertEqual(bys(next_calls("--by", "function", system_wide=False)), [])

    def test_the_process_view_still_points_to_modules(self):
        self.assertEqual(bys(next_calls("--by", "process", system_wide=False)), ["module"])

    def test_a_system_wide_process_view_leaves_the_process_to_the_caller(self):
        # Which process to drill into is the caller's choice from the rows, never the top row.
        calls = next_calls("--by", "process", system_wide=True)
        self.assertEqual(bys(calls), ["module"])
        self.assertIn("--pid <PID>", calls[0])

    def test_no_next_call_carries_a_value_the_caller_did_not_give(self):
        # Only the caller's own session, scope, filter and paths are carried forward.
        for argv, system_wide in (
            (["--by", "process"], True),
            (["--by", "process"], False),
            (["--by", "module"], True),
            (["--by", "function"], False),
            (["--by", "line", "--function", "f", "--module", "m.dll"], True),
            (["--by", "instruction", "--function", "f", "--module", "m.dll"], False),
        ):
            with self.subTest(argv=argv, system_wide=system_wide):
                given = set(argv) | {SESSION}
                for call in next_calls(*argv, system_wide=system_wide, unresolved=1):
                    values = argv_of(call)
                    for flag in ("--pid", "--image", "--function", "--module", "--function-start"):
                        if flag in values:
                            value = values[values.index(flag) + 1]
                            # The only value a NEXT call may leave open is the process the caller
                            # picks from the rows or names in the question.
                            self.assertTrue(
                                value in given
                                or (flag, value) in (("--pid", "<PID>"), ("--image", "%<EXE>")),
                                f"{flag} {value} in {call}",
                            )

    def test_following_next_from_the_system_wide_process_ranking_never_loops(self):
        # process -> module --pid -> function --pid, never back to the process ranking; the
        # caller fills <PID> from the rows.
        seen = ["process"]
        calls = next_calls("--by", "process", system_wide=True)
        while calls:
            self.assertEqual(len(calls), 1, calls)
            argv = [value.replace("<PID>", "4242") for value in argv_of(calls[0])]
            self.assertIn("--pid", argv)
            by = argv[argv.index("--by") + 1]
            self.assertNotIn(by, seen)
            seen.append(by)
            calls = next_calls(*argv, system_wide=True)
        self.assertEqual(seen, ["process", "module", "function"])


if __name__ == "__main__":
    unittest.main()
