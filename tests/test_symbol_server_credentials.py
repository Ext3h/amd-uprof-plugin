"""Printed symbol-server URLs and echoed AMDuProfCLI output must not carry credentials.

Run from repository root: python tests/run_tests.py test_symbol_server_credentials
"""

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import uprof_dump
import uprof_native
import uprof_symbols
import uprof_util

SECRETS = ("token-user", "secret-token", "myuser", "mypass")


class RedactCredentialsTest(unittest.TestCase):
    def test_clean_urls_are_unchanged(self):
        for text in (
            "https://msdl.microsoft.com/download/symbols",
            "srv*C:\\cache*https://msdl.microsoft.com/download/symbols",
            "user@example.com wrote C:\\a@b\\x.pdb",
            "see https://example.com/a@b",
        ):
            with self.subTest(text=text):
                self.assertEqual(uprof_util.redact_credentials(text), text)

    def test_credentials_are_redacted_in_urls_and_srv_entries(self):
        self.assertEqual(
            uprof_util.redact_credentials("https://alice:secret-token@symbols.corp.net/download"),
            "https://***@symbols.corp.net/download",
        )
        self.assertEqual(
            uprof_util.redact_credentials(
                "srv*C:\\cache*https://token-user:ghp_123456789@symbols.corp.net/download"
            ),
            "srv*C:\\cache*https://***@symbols.corp.net/download",
        )

    def test_a_raw_at_in_the_password_is_redacted_too(self):
        self.assertEqual(
            uprof_util.redact_credentials("https://user:p@ss@host.example/x"),
            "https://***@host.example/x",
        )


class TranslateRedactTest(unittest.TestCase):
    def assert_clean(self, text):
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_translate_prints_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            (session / "session.uprof").write_text("{}", encoding="utf-8")
            out = root / "out"
            commands = []
            echoed = "fetching https://token-user:secret-token@symbols.example.com/x.pdb\n"

            def fake_run(command, **_kwargs):
                commands.append(command)
                (out / "cpu.db").write_text("x", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, echoed, echoed)

            stdout, stderr = io.StringIO(), io.StringIO()
            symbol_path = "srv*C:\\cache*https://token-user:secret-token@symbols.example.com"
            custom = "http://myuser:mypass@custom.symbols.com"
            with ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {"_NT_SYMBOL_PATH": symbol_path}))
                for name, kwargs in (
                    ("require_uprof", {"return_value": Path("AMDuProfCLI")}),
                    ("run_watched", {"side_effect": fake_run}),
                    ("symbolization_counts", {"return_value": (1, 1, 1)}),
                ):
                    stack.enter_context(mock.patch.object(uprof_native, name, **kwargs))
                stack.enter_context(redirect_stdout(stdout))
                stack.enter_context(redirect_stderr(stderr))
                uprof_native.translate(session, out, symbol_servers=[custom])

            self.assert_clean(stdout.getvalue())
            self.assert_clean(stderr.getvalue())
            self.assertIn(
                "SYMBOL_SERVERS=srv*C:\\cache*https://***@symbols.example.com | "
                "http://***@custom.symbols.com",
                stdout.getvalue(),
            )
            self.assertIn(
                "symbol server http://***@custom.symbols.com uses plain http", stderr.getvalue()
            )
            self.assertIn("https://***@symbols.example.com/x.pdb", stdout.getvalue())
            # AMDuProfCLI itself still receives the credentials it needs to authenticate.
            command = " ".join(map(str, commands[0]))
            self.assertIn("https://token-user:secret-token@symbols.example.com", command)
            self.assertIn("http://myuser:mypass@custom.symbols.com", command)

    def test_failure_detail_redacts_credentials(self):
        completed = subprocess.CompletedProcess(
            ["AMDuProfCLI"], 1, "https://myuser:mypass@a.example/x\n", "error\n"
        )
        detail = uprof_native.failure_detail(completed)
        self.assert_clean(detail)
        self.assertIn("https://***@a.example/x", detail)

    def test_translate_stopped_redacts_credentials(self):
        error = uprof_native.translate_stopped(
            Path("C:/s"), Path("C:/o"), ["https://myuser:mypass@a.example"], stalled=True
        )
        text = str(error)
        self.assert_clean(text)
        self.assertIn("https://***@a.example", text)


class DumpRedactTest(unittest.TestCase):
    def test_a_missing_or_empty_dump_error_carries_no_credentials(self):
        echoed = "fetching https://myuser:mypass@a.example/x.pdb"
        for empty in (False, True):
            with self.subTest(empty=empty), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                session = root / "session"
                session.mkdir()
                (session / "session.uprof").write_text("{}", encoding="utf-8")

                def fake_run(command, empty_dump=empty, **_kwargs):
                    if empty_dump:
                        staging = Path(command[command.index("-i") + 1])
                        (staging / uprof_util.DUMP_FILENAMES["ibsop-event-dump"]).touch()
                    return subprocess.CompletedProcess(command, 0, echoed, echoed)

                with ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            uprof_dump, "require_uprof", return_value=Path("AMDuProfCLI")
                        )
                    )
                    stack.enter_context(mock.patch.object(uprof_dump, "run", side_effect=fake_run))
                    stack.enter_context(redirect_stderr(io.StringIO()))
                    with self.assertRaises(uprof_util.UprofError) as cm:
                        uprof_dump._make_dump(
                            session, "ibsop-event-dump", None, root / "cache" / "dump", None
                        )
                text = str(cm.exception)
                for secret in SECRETS:
                    self.assertNotIn(secret, text)
                self.assertIn("https://***@a.example/x.pdb", text)


class EvidenceRedactTest(unittest.TestCase):
    def test_unconsulted_urls_are_redacted(self):
        evidence = uprof_symbols.ModuleEvidence(
            "amdvlk64.dll",
            "C:/app/amdvlk64.dll",
            image_guid="ABCD",
            image_age=1,
            search=uprof_symbols.PdbSearch(
                probes=[
                    uprof_symbols.PdbProbe(
                        Path("C:/app/amdvlk64.pdb"), "not_found", detail="not found"
                    )
                ],
                urls=["https://token-user:secret-token@symbols.example.com"],
            ),
            outcome="no_pdb",
        )
        stream = io.StringIO()
        uprof_symbols._evidence_line(evidence, stream)
        text = stream.getvalue()
        self.assertNotIn("token-user", text)
        self.assertNotIn("secret-token", text)
        self.assertIn("https://***@symbols.example.com", text)


if __name__ == "__main__":
    unittest.main()
