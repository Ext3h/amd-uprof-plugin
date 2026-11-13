"""translate warns about a plain-http symbol server.

AMDuProfCLI downloads and parses PDBs from every symbol server it is given. Over plain http a
network attacker can substitute those PDBs, so the run says so instead of using the server
silently.
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_native


class _Stop(Exception):
    """Raised once translate has printed its symbol settings."""


class HttpSymbolServerTest(unittest.TestCase):
    def stderr(self, servers=None, nt_symbol_path=None):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session"
            session.mkdir()
            (session / "session.uprof").write_text("{}", encoding="utf-8")
            env = {"_NT_SYMBOL_PATH": nt_symbol_path} if nt_symbol_path else {}
            err = io.StringIO()
            with (
                mock.patch.dict(os.environ, env),
                mock.patch.object(uprof_native, "require_uprof", side_effect=_Stop()),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(err),
            ):
                if not nt_symbol_path:
                    os.environ.pop("_NT_SYMBOL_PATH", None)
                with self.assertRaises(_Stop):
                    uprof_native.translate(session, Path(tmp) / "out", symbol_servers=servers)
            return err.getvalue()

    def warnings(self, text):
        return [line for line in text.splitlines() if line.startswith("WARNING:")]

    def test_a_plain_http_server_option_warns(self):
        lines = self.warnings(self.stderr(servers=["http://symbols.example/store"]))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("http://symbols.example/store", lines[0])
        self.assertIn("https://", lines[0])

    def test_a_plain_http_server_in_the_symbol_path_warns(self):
        lines = self.warnings(
            self.stderr(nt_symbol_path=r"srv*C:\symcache*HTTP://symbols.example/store")
        )
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("HTTP://symbols.example/store", lines[0])

    def test_https_servers_do_not_warn(self):
        self.assertEqual(self.warnings(self.stderr(servers=["https://symbols.example/store"])), [])


if __name__ == "__main__":
    unittest.main()
