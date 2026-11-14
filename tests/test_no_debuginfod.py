"""llvm-symbolizer never contacts a debuginfod server.

With DEBUGINFOD_URLS set (several Linux distributions set it system-wide), llvm-symbolizer
fetches debug info by build ID from those servers unless --no-debuginfod is passed. profile
never contacts a symbol URL, so every symbolizer command carries the option.
"""

import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import debug_fixtures as F
import uprof_context
import uprof_symbols

FIXTURE = F.ELF / "fixture_buildid"


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


class CommandTest(unittest.TestCase):
    def test_the_symbolization_pass_disables_debuginfod(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(uprof_symbols, "run", return_value=_Completed()) as run,
        ):
            rvas = Path(tmp) / "rvas.txt"
            rvas.write_text("0x1380\n", encoding="utf-8")
            uprof_symbols._run_symbolizer(
                Path("llvm-symbolizer"), str(FIXTURE), None, rvas, Path(tmp) / "out.json"
            )
        self.assertIn("--no-debuginfod", run.call_args.args[0])

    def test_instruction_context_disables_debuginfod(self):
        with mock.patch.object(uprof_context, "run", return_value=_Completed()) as run:
            uprof_context.symbolize_rvas(str(FIXTURE), [0x1380], symbolizer="llvm-symbolizer")
        self.assertIn("--no-debuginfod", run.call_args.args[0])


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list[str]] = []

    def do_GET(self):
        self.requests.append(self.path)
        self.send_error(404)

    def log_message(self, *_args):
        pass


@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
class ListenerTest(unittest.TestCase):
    """Both symbolizer calls against a local server named by DEBUGINFOD_URLS."""

    def setUp(self):
        _Recorder.requests = []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        environment = mock.patch.dict(os.environ, {"DEBUGINFOD_URLS": url})
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_symbolization_pass_sends_no_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            rvas = Path(tmp) / "rvas.txt"
            rvas.write_text(f"0x{F.ELF_ENTRY:X}\n", encoding="utf-8")
            uprof_symbols._run_symbolizer(
                F.SYMBOLIZER, str(FIXTURE), None, rvas, Path(tmp) / "out.json"
            )
        self.assertEqual(_Recorder.requests, [])

    def test_instruction_context_sends_no_request(self):
        uprof_context.symbolize_rvas(str(FIXTURE), [F.ELF_ENTRY], symbolizer=str(F.SYMBOLIZER))
        self.assertEqual(_Recorder.requests, [])


if __name__ == "__main__":
    unittest.main()
