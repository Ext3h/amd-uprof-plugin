"""SYMBOL_SERVERS= and unconsulted symbol-server evidence must redact credentials.

Run from repository root: python tests/run_tests.py test_symbol_server_credentials
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
import uprof_symbols
import uprof_util


class RedactCredentialsTest(unittest.TestCase):
    def test_clean_urls_are_unchanged(self):
        url = "https://msdl.microsoft.com/download/symbols"
        self.assertEqual(uprof_util.redact_credentials(url), url)
        srv = "srv*C:\\cache*https://msdl.microsoft.com/download/symbols"
        self.assertEqual(uprof_util.redact_credentials(srv), srv)

    def test_credentials_are_redacted_in_urls_and_srv_entries(self):
        url = "https://alice:secret-token@symbols.corp.net/download"
        self.assertEqual(uprof_util.redact_credentials(url), "https://***@symbols.corp.net/download")

        srv = "srv*C:\\cache*https://token-user:ghp_123456789@symbols.corp.net/download"
        self.assertEqual(uprof_util.redact_credentials(srv), "srv*C:\\cache*https://***@symbols.corp.net/download")


class TranslateSymbolServersRedactTest(unittest.TestCase):
    def test_symbol_servers_header_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            (session / "session.uprof").write_bytes(b"session")
            out = root / "out"

            recorded_commands = []

            def fake_run(command, **_kwargs):
                recorded_commands.append(command)
                (out / "cpu.db").write_text("x", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "Translation finished\n", "")

            stream = io.StringIO()
            symbol_path = "srv*C:\\cache*https://token-user:secret-token@symbols.example.com"
            custom_server = "https://myuser:mypass@custom.symbols.com"
            with mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": symbol_path}), \
                    mock.patch.object(uprof_native, "require_uprof",
                                       return_value=Path("AMDuProfCLI")), \
                    mock.patch.object(uprof_native, "run", side_effect=fake_run), \
                    mock.patch.object(uprof_native, "symbolization_counts",
                                       return_value=(1, 1, 1)), \
                    redirect_stdout(stream):
                uprof_native.translate(session, out, symbol_servers=[custom_server])

            text = stream.getvalue()
            # Credentials must never be echoed in stdout
            self.assertNotIn("token-user", text)
            self.assertNotIn("secret-token", text)
            self.assertNotIn("myuser", text)
            self.assertNotIn("mypass", text)
            self.assertIn("SYMBOL_SERVERS=srv*C:\\cache*https://***@symbols.example.com | https://***@custom.symbols.com", text)
            # Underlying command must retain unredacted credentials for AMDuProfCLI to authenticate
            command_str = " ".join(recorded_commands[0])
            self.assertIn("https://token-user:secret-token@symbols.example.com", command_str)
            self.assertIn("https://myuser:mypass@custom.symbols.com", command_str)


class EvidenceSymbolServersRedactTest(unittest.TestCase):
    def test_evidence_line_redacts_credentials_in_unconsulted_urls(self):
        evidence = uprof_symbols.ModuleEvidence(
            "amdvlk64.dll", "C:/app/amdvlk64.dll", image_guid="ABCD", image_age=1,
            search=uprof_symbols.PdbSearch(
                probes=[uprof_symbols.PdbProbe(Path("C:/app/amdvlk64.pdb"), "not_found",
                                               detail="not found")],
                urls=["https://token-user:secret-token@symbols.example.com"]),
            outcome="no_pdb")
        stream = io.StringIO()

        uprof_symbols._evidence_line(evidence, stream)

        text = stream.getvalue()
        self.assertNotIn("token-user", text)
        self.assertNotIn("secret-token", text)
        self.assertIn("https://***@symbols.example.com", text)


if __name__ == "__main__":
    unittest.main()
