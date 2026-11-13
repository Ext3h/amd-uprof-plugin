"""translate is bounded in time and names the likely cause when the bound expires (#323).

AMDuProfCLI fetches symbols during translate, from the servers it is given and the shares in
the caller's _NT_SYMBOL_PATH. A slow or unreachable one stalled a translate for more than 30
minutes with no output, so the run is bounded, and the failure names the cause and a runnable
alternative that needs no network symbol source.

Run from the repository root: python tests/run_tests.py test_translate_bound
"""

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import uprof_native
from uprof_util import UprofError


class TranslateBoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.session = root / "capture"
        (self.session / "cpu").mkdir(parents=True)
        (self.session / "session.uprof").write_text("{}", encoding="utf-8")
        self.out = root / "translated"
        self.timeouts = []

    def stalled(self, command, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    def test_a_stalled_translate_fails_with_the_cause_and_a_local_symbols_alternative(self):
        with (
            mock.patch.object(uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")),
            mock.patch.object(uprof_native.subprocess, "run", side_effect=self.stalled),
            mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": "srv*c:\\sym*\\\\slow\\share"}),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(UprofError) as raised:
                uprof_native.translate(self.session, self.out)

        message = str(raised.exception)
        self.assertEqual(self.timeouts, [uprof_native.TRANSLATE_TIMEOUT])
        minutes = uprof_native.TRANSLATE_TIMEOUT // 60
        self.assertIn(f"translate did not finish within {minutes} min", message)
        self.assertIn("slow or unreachable symbol server or share", message)
        self.assertIn("srv*c:\\sym*\\\\slow\\share", message)
        alternative = next(
            line for line in message.splitlines() if line.startswith("ALTERNATIVE: ")
        )
        self.assertIn(
            f"translate --session {self.session} --out-dir {self.out} --force", alternative
        )
        self.assertTrue(alternative.split(": ", 2)[2].startswith("python "), alternative)
        self.assertIn("run it with _NT_SYMBOL_PATH unset or set to a local folder only", message)


if __name__ == "__main__":
    unittest.main()
