"""An IBS capture the machine cannot perform fails; collect never starts another sampler itself.

The caller asked for IBS to answer a question only IBS answers (per-op fields, time_share,
--time-filter), so a cycle capture taken in its place would answer a different question. collect
stops with one cause line and the alternatives as runnable calls built from the caller's own
values (issue #202).

Run from the repository root: python tests/run_tests.py test_collect_ibs_unavailable
"""

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_collect
import uprof_native
import uprof_shell

FAKE_CLI = "AMDuProfCLI"


class IbsUnavailableTest(unittest.TestCase):
    def collect(self, **fields):
        tmp = tempfile.TemporaryDirectory(prefix="uprof-ibs-")
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "out"
        calls = []

        def fake_run_cli(_uprof, event_args, _target_args, options):
            calls.append((" ".join(event_args), options.out_dir))
            return subprocess.CompletedProcess(event_args, 1, "", "IBS counters are not available")

        options = uprof_collect.CollectOptions(
            out_dir=str(out), seconds=5, event="ibs-op", **fields
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(uprof_collect, "is_admin", return_value=True),
            mock.patch.object(uprof_collect, "find_uprof", return_value=FAKE_CLI),
            mock.patch.object(uprof_collect, "require_uprof", return_value=FAKE_CLI),
            mock.patch.object(
                uprof_collect, "ibs_unavailable_reason", return_value="VBS is running"
            ),
            mock.patch.object(uprof_collect, "_run_cli", side_effect=fake_run_cli),
            mock.patch.object(
                uprof_collect, "_core_frequency", return_value=(4_700_000_000, "4700 MHz", "test")
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(uprof_native.UprofError) as raised,
        ):
            uprof_collect.collect(options)
        return str(raised.exception), calls, stdout.getvalue(), out

    def test_no_other_sampler_is_started(self):
        _, calls, stdout, _ = self.collect()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("ibs-op", calls[0][0])
        self.assertNotIn("SAMPLER:", stdout)
        self.assertNotIn("SESSION=", stdout)

    def test_one_cause_line_names_the_reason(self):
        text, _, _, _ = self.collect()
        lines = text.splitlines()
        self.assertEqual(lines[0], "IBS unavailable: VBS is running")
        self.assertTrue(all(line.startswith("ALTERNATIVE: ") for line in lines[1:3]), lines)

    def test_the_alternatives_are_runnable_calls_with_the_callers_values(self):
        for fields, target in (({}, ""), ({"pids": [4242, 17]}, " --process 4242,17")):
            with self.subTest(fields=fields):
                text, _, _, out = self.collect(**fields)
                alternatives = text.splitlines()[1:3]
                # A path that is not plain is a placeholder named on its own data line.
                shown = uprof_shell.path_arg(str(out), "<OUT_DIR>")[0]
                self.assertEqual(
                    [line.split(": ", 2)[1] for line in alternatives],
                    ["cycle capture (same target)", "timer capture (system-wide)"],
                )
                for line in alternatives:
                    for placeholder in ("<NEW_OUT_DIR>", "<S>", "<PID>"):
                        self.assertNotIn(placeholder, line)
                    self.assertIn(f"collect --out-dir {shown} --seconds 5", line)
                self.assertTrue(alternatives[0].endswith(f"--seconds 5{target} --event cycles"))
                # A timer capture attached to PIDs records nothing on Windows: system-wide.
                self.assertTrue(alternatives[1].endswith("--seconds 5 --event timer"))


if __name__ == "__main__":
    unittest.main()
