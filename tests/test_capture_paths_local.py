"""Paths taken from a capture never reach the network or a device.

Module.path, the CodeView PDB path inside an image and session.uprof's translateCommand come from
whoever made the capture. Opening a UNC path makes Windows authenticate to that host with the
user's credentials, so such paths are only ever data; the user's own --image-path and
--symbol-path arguments may still name a share.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_cache
import uprof_symbols
import uprof_util

REMOTE = (
    r"\\192.0.2.1\share\app.exe",
    "//192.0.2.1/share/app.exe",
    r"\\?\UNC\192.0.2.1\share\app.exe",
    r"\\.\PhysicalDrive0",
)


class _Touched(Exception):
    """A filesystem call reached a remote path."""


def _guard(path, *args, **kwargs):
    text = os.fspath(path)
    if text.startswith(("\\\\", "//")):
        raise _Touched(text)
    return _real_stat(path, *args, **kwargs)


_real_stat = os.stat


def _no_remote_touch():
    """Fail on any stat-like call that reaches a remote path, including the C fast paths of
    os.path on Windows that bypass os.stat."""
    import contextlib

    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch("os.stat", side_effect=_guard))
    for name in ("isfile", "isdir", "exists"):
        real = getattr(os.path, name)

        def checked(path, real=real):
            if os.fspath(path).startswith(("\\\\", "//")):
                raise _Touched(os.fspath(path))
            return real(path)

        stack.enter_context(mock.patch(f"os.path.{name}", side_effect=checked))
    return stack


class RemotePathTest(unittest.TestCase):
    def test_unc_and_device_paths_are_remote(self):
        for path in REMOTE:
            with self.subTest(path=path):
                self.assertTrue(uprof_util.is_remote_path(path))

    def test_local_paths_are_not_remote(self):
        local = r"C:\app\app.exe" if os.name == "nt" else "/opt/app/app"
        self.assertFalse(uprof_util.is_remote_path(local))
        self.assertFalse(uprof_util.is_remote_path("app.exe"))


class NoRemoteTouchTest(unittest.TestCase):
    def test_a_remote_module_path_is_never_opened(self):
        for path in REMOTE:
            with self.subTest(path=path), _no_remote_touch():
                self.assertEqual(uprof_symbols.resolve_image("app.exe", path, (), ()), (None, None))

    def test_a_recorded_remote_pdb_path_is_no_candidate(self):
        with tempfile.TemporaryDirectory() as tmp, _no_remote_touch():
            candidates = uprof_symbols._candidate_pdbs(
                Path(tmp) / "app.exe",
                "app.pdb",
                None,
                None,
                recorded_pdb=Path(r"\\192.0.2.1\share\app.pdb"),
            )
        self.assertFalse([c for c in candidates if str(c).startswith(("\\\\", "//"))])

    def test_the_cache_key_stats_no_remote_module_directory(self):
        with _no_remote_touch():
            uprof_cache.search_identity(None, None, list(REMOTE))

    def test_translate_command_names_no_symbol_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            store.mkdir()
            (Path(tmp) / "session.uprof").write_text(
                json.dumps(
                    {"translateCommand": f"AMDuProfCLI translate --symbol-cache-dir {store}"}
                ),
                encoding="utf-8",
            )
            stores = uprof_symbols.symbol_stores()
        self.assertNotIn(store, stores)


class PdbNameTest(unittest.TestCase):
    def test_only_a_plain_pdb_file_name_is_kept(self):
        for recorded, expected in (
            (r"C:\build\app.pdb", "app.pdb"),
            (r"C:\build\..", "app.pdb"),
            (r"C:\build\*.pdb", "app.pdb"),
            (r"C:\build\[a].pdb", "app.pdb"),
        ):
            with self.subTest(recorded=recorded):
                self.assertEqual(uprof_symbols._plain_pdb_name(recorded, "app.exe"), expected)


if __name__ == "__main__":
    unittest.main()
