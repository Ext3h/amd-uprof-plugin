"""NEXT steers an unscoped system-wide view to the process ranking, and nothing else to it.

The first profile call depends on the question: an attached capture goes straight to the
module or function lens. Only an unscoped view of a system-wide capture mixes every process,
so its NEXT names `--by process` first; a --pid/--image or --function scope, an attached
capture and the process view itself keep their drill-down calls.
Run from the repository root: python tests/run_tests.py test_next_steering
"""

import unittest

import uprof
import uprof_query

SESSION = "C:/captures/run" if uprof_query.os.name == "nt" else "/captures/run"


def next_calls(*argv, system_wide, unresolved=0):
    args = uprof.build_parser().parse_args(["profile", "--session", SESSION, *argv])
    return uprof_query._next_calls(args, {"unresolved": unresolved}, system_wide=system_wide)


def bys(calls):
    return [call.split("--by ")[1].split()[0] for call in calls]


class NextSteeringTest(unittest.TestCase):
    def test_an_unscoped_system_wide_view_names_the_process_ranking_first(self):
        for by, rest in (("module", ["function"]), ("function", []), ("region", []),
                         ("line", ["instruction"])):
            with self.subTest(by=by):
                self.assertEqual(bys(next_calls("--by", by, system_wide=True)),
                                 ["process", *rest])

    def test_a_scoped_system_wide_view_keeps_its_drill_down(self):
        for scope in (["--pid", "100"], ["--image", "app.exe"]):
            with self.subTest(scope=scope):
                self.assertEqual(bys(next_calls("--by", "module", *scope, system_wide=True)),
                                 ["function"])

    def test_a_function_scoped_view_is_not_sent_back_to_processes(self):
        calls = next_calls("--by", "line", "--function", "main", "--module", "app.exe",
                           system_wide=True)
        self.assertNotIn("process", bys(calls))

    def test_an_attached_capture_keeps_its_drill_down(self):
        self.assertEqual(bys(next_calls("--by", "module", system_wide=False)), ["function"])
        self.assertEqual(bys(next_calls("--by", "function", system_wide=False)), [])

    def test_the_process_view_still_points_to_modules(self):
        self.assertEqual(bys(next_calls("--by", "process", system_wide=True)), ["module"])


if __name__ == "__main__":
    unittest.main()
