"""translate stops a stalled AMDuProfCLI and names the likely cause (#323).

AMDuProfCLI fetches symbols during translate, from the servers it is given and the shares in
the caller's _NT_SYMBOL_PATH. A slow or unreachable one stalled a translate for more than 30
minutes with no output, while a healthy cold translate also took about 31 minutes. So a short
wall-clock bound cannot tell them apart: translate is stopped when it makes no progress (its
CLI log, output and symbol cache stop changing) for TRANSLATE_STALL seconds, with
TRANSLATE_CEILING as an absolute backstop. Either failure names the cause and a runnable
alternative that needs no network symbol source.

Run from the repository root: python tests/run_tests.py test_translate_bound
"""

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import uprof_native
import uprof_shell
from uprof_util import UprofError

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def writer(path: Path, seconds: float) -> list[str]:
    """A child that appends to path every 0.1 s for seconds, then exits 0."""
    code = (
        "import sys, time\n"
        "end = time.monotonic() + float(sys.argv[2])\n"
        "while time.monotonic() < end:\n"
        "    with open(sys.argv[1], 'a') as handle:\n"
        "        handle.write('x')\n"
        "    time.sleep(0.1)\n"
        "print('done')\n"
    )
    return [sys.executable, "-c", code, str(path), str(seconds)]


class RunWatchedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.progress_file = Path(self.tmp.name) / "progress.log"

    def size(self):
        return self.progress_file.stat().st_size if self.progress_file.exists() else -1

    def test_a_child_whose_progress_stops_is_stopped_as_stalled(self):
        started = time.monotonic()
        with self.assertRaises(uprof_native.ToolStalled):
            uprof_native.run_watched(
                SLEEPER, progress=lambda: 0, stall_s=1.0, ceiling_s=60.0, poll_s=0.1
            )

        self.assertLess(time.monotonic() - started, 30)

    def test_a_child_that_keeps_progressing_outlives_the_stall_window(self):
        # The child writes for longer than the stall window, so only its progress keeps it alive.
        completed = uprof_native.run_watched(
            writer(self.progress_file, 8.0),
            progress=self.size,
            stall_s=4.0,
            ceiling_s=60.0,
            poll_s=0.1,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("done", completed.stdout)

    def test_the_ceiling_stops_a_child_that_keeps_progressing(self):
        with self.assertRaises(uprof_native.ToolTimeout):
            uprof_native.run_watched(
                writer(self.progress_file, 60),
                progress=self.size,
                stall_s=5.0,
                ceiling_s=1.5,
                poll_s=0.1,
            )


class TranslateStallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.session = root / "capture"
        (self.session / "cpu").mkdir(parents=True)
        (self.session / "session.uprof").write_text("{}", encoding="utf-8")
        self.out = root / "translated"

    def failure(self, error):
        with (
            mock.patch.object(uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")),
            mock.patch.object(uprof_native, "run_watched", side_effect=error) as watched,
            mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": "srv*c:\\sym*\\\\slow\\share"}),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(UprofError) as raised:
                uprof_native.translate(self.session, self.out)
        kwargs = watched.call_args.kwargs
        self.assertEqual(kwargs["stall_s"], uprof_native.TRANSLATE_STALL)
        self.assertEqual(kwargs["ceiling_s"], uprof_native.TRANSLATE_CEILING)
        return str(raised.exception)

    def assert_alternative(self, message):
        self.assertIn("slow or unreachable symbol server or share", message)
        self.assertIn("srv*c:\\sym*\\\\slow\\share", message)
        alternative = next(
            line for line in message.splitlines() if line.startswith("ALTERNATIVE: ")
        )
        session, _ = uprof_shell.path_arg(self.session, "<SESSION_DIR>")
        out, _ = uprof_shell.path_arg(self.out, "<OUT_DIR>")
        self.assertIn(f"translate --session {session} --out-dir {out} --force", alternative)
        self.assertTrue(alternative.split(": ", 2)[2].startswith("python "), alternative)
        self.assertIn("run it with _NT_SYMBOL_PATH unset or set to a local folder only", message)

    def test_a_stalled_translate_names_the_stall_and_the_alternative(self):
        message = self.failure(uprof_native.ToolStalled("stalled"))

        minutes = uprof_native.TRANSLATE_STALL // 60
        self.assertIn(f"translate made no progress for {minutes} min", message)
        self.assert_alternative(message)

    def test_a_translate_past_the_ceiling_names_the_bound_and_the_alternative(self):
        message = self.failure(uprof_native.ToolTimeout("timed out"))

        minutes = uprof_native.TRANSLATE_CEILING // 60
        self.assertIn(f"translate did not finish within {minutes} min", message)
        self.assert_alternative(message)


if __name__ == "__main__":
    unittest.main()
