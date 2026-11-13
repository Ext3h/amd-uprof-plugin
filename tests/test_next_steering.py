"""NEXT steers an unscoped system-wide rollup to the process ranking, and nothing else to it.

The first profile call depends on the question: an attached capture goes straight to the
module or function lens. Only an unscoped module, function or region rollup of a system-wide
capture mixes every process, so its NEXT names `--by process` first; a --pid/--image or
--function scope, line and instruction drill-downs, an attached capture and the process view
itself keep their calls.
Run from the repository root: python tests/run_tests.py test_next_steering
"""
# Spec: SPEC-77

import unittest

import uprof
import uprof_query

SESSION = "C:/captures/run" if uprof_query.os.name == "nt" else "/captures/run"


def next_calls(*argv, system_wide, unresolved=0, top_process=None):
    args = uprof.build_parser().parse_args(["profile", "--session", SESSION, *argv])
    return uprof_query._next_calls(
        args, {"unresolved": unresolved}, system_wide=system_wide, top_process=top_process
    )


def argv_of(call):
    """The profile arguments of an emitted call, the way the next call would be parsed."""
    return call.split(" profile ", 1)[1].replace('"', "").split()[2:]


def bys(calls):
    return [call.split("--by ")[1].split()[0] for call in calls]


class NextSteeringTest(unittest.TestCase):
    def test_an_unscoped_system_wide_rollup_names_the_process_ranking_first(self):
        for by, rest in (("module", ["function"]), ("function", []), ("region", [])):
            with self.subTest(by=by):
                self.assertEqual(bys(next_calls("--by", by, system_wide=True)), ["process", *rest])

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

    def test_a_system_wide_process_view_scopes_its_module_call_to_the_top_process(self):
        calls = next_calls("--by", "process", system_wide=True, top_process=(4242, "app.exe"))
        self.assertEqual(bys(calls), ["module"])
        self.assertIn("--pid 4242", calls[0])

    def test_a_top_process_without_a_pid_scopes_by_its_image_and_ends_the_chain(self):
        calls = next_calls("--by", "process", system_wide=True, top_process=(None, "app.exe"))
        self.assertEqual(bys(calls), ["module"])
        self.assertIn("--image app.exe", calls[0])
        follow = next_calls(*argv_of(calls[0]), system_wide=True)
        self.assertEqual(bys(follow), ["function"])

    def test_no_process_row_offers_no_unscoped_module_call(self):
        self.assertEqual(next_calls("--by", "process", system_wide=True, top_process=None), [])

    def test_following_next_from_the_system_wide_process_ranking_never_loops(self):
        # process -> module --pid -> function --pid, never back to the process ranking.
        seen = ["process"]
        calls = next_calls("--by", "process", system_wide=True, top_process=(4242, "app.exe"))
        while calls:
            self.assertEqual(len(calls), 1, calls)
            argv = argv_of(calls[0])
            self.assertIn("--pid", argv)
            by = argv[argv.index("--by") + 1]
            self.assertNotIn(by, seen)
            seen.append(by)
            calls = next_calls(*argv, system_wide=True)
        self.assertEqual(seen, ["process", "module", "function"])


if __name__ == "__main__":
    unittest.main()
