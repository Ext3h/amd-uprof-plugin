"""The dump cache is private to the user who runs the tool.

Another local user must not be able to plant a directory, a forged dump or a link in it, and a
link planted there must never redirect a write to the file it points at.
"""

import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import debug_fixtures as F
import uprof_dump
import uprof_util

HEADER = "DeltaTick,CPU_Number,TID,PID,Offset,ModuleName,IbsTagToRetCtr"
NOT_PRIVATE = "is not private to this user"
SAMPLER = types.SimpleNamespace(ibs_dump_kind="ibsop-event-dump")


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="uprof-private-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.root = self.tmp / "user-cache" / "uprof-dump-cache"
        patcher = mock.patch.object(uprof_dump, "cache_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = self.tmp / "session"
        self.session.mkdir()
        (self.session / "session.uprof").write_text('{"translateCommand": ""}', encoding="utf-8")
        self.victim = self.tmp / "victim"
        self.victim.mkdir()
        (self.victim / "keep.txt").write_text("keep", encoding="utf-8")

    def link_dir(self, link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)

    def link_file(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"host: cannot create a file symlink here: {exc}")

    def dump_beside(self, header=HEADER) -> uprof_dump.DumpInfo:
        dump = self.session / "IbsOpDump.csv"
        row = "1,0,7,100,16,c:\\a.exe" + (",5" if header.endswith("IbsTagToRetCtr") else "")
        dump.write_text(f"{header}\n{row}\n", encoding="utf-8", newline="\n")
        return uprof_dump.DumpInfo(dump, "beside")

    def assert_refused(self, call):
        with self.assertRaisesRegex(uprof_util.UprofError, NOT_PRIVATE) as raised:
            call()
        self.assertIn("set to a new directory you own", str(raised.exception))
        self.assertEqual(sorted(p.name for p in self.victim.iterdir()), ["keep.txt"])


class CacheRootLocationTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "host: the Windows temp directory is per user")
    def test_the_root_is_outside_the_shared_temp_directory(self):
        home = Path(os.sep) / "home" / "user" / ".cache"
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(home)}):
            root = uprof_dump.cache_root()
            self.assertTrue(uprof_util.contains(home, root), root)
            self.assertFalse(uprof_util.contains(Path(tempfile.gettempdir()), root), root)


class PrivateRootTest(_Case):
    def test_a_linked_root_is_refused_before_reading_the_cache(self):
        self.link_dir(self.root, self.victim)
        self.assert_refused(lambda: uprof_dump.resolve_dump(self.session, SAMPLER))

    def test_a_linked_session_cache_directory_is_refused(self):
        self.root.mkdir(parents=True, mode=0o700)
        self.link_dir(uprof_dump.cache_dir(self.session), self.victim)
        self.assert_refused(lambda: uprof_dump.resolve_dump(self.session, SAMPLER))

    @unittest.skipIf(F.duckdb_skip(), F.duckdb_skip())
    def test_a_linked_root_is_refused_for_the_parquet_copy_of_a_dump_beside(self):
        dump = self.dump_beside()
        self.link_dir(self.root, self.victim)
        self.assert_refused(lambda: uprof_dump.samples_parquet(dump, self.session))

    @unittest.skipIf(os.name == "nt", "host: POSIX permission bits do not apply on Windows")
    def test_a_root_other_users_can_write_is_refused(self):
        self.root.mkdir(parents=True)
        os.chmod(self.root, 0o777)
        self.assert_refused(lambda: uprof_dump.resolve_dump(self.session, SAMPLER))

    @unittest.skipIf(os.name == "nt", "host: POSIX owners do not apply on Windows")
    def test_a_root_owned_by_another_user_is_refused(self):
        self.root.mkdir(parents=True, mode=0o700)
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            self.assert_refused(lambda: uprof_dump.resolve_dump(self.session, SAMPLER))

    @unittest.skipIf(os.name == "nt", "host: POSIX permission bits do not apply on Windows")
    @unittest.skipIf(F.duckdb_skip(), F.duckdb_skip())
    def test_new_cache_directories_are_private(self):
        dump = self.dump_beside()
        old = os.umask(0o022)
        try:
            self.assertIsNotNone(uprof_dump.samples_parquet(dump, self.session))
        finally:
            os.umask(old)
        for path in (self.root, uprof_dump.cache_dir(self.session)):
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700, path)
        self.assertIsNotNone(uprof_dump.samples_parquet(dump, self.session))


class LinkedSidecarTest(_Case):
    def setUp(self):
        super().setUp()
        self.cache = self.tmp / "cache"
        self.cache.mkdir()
        self.secret = self.victim / "secret.txt"
        self.secret.write_text("secret", encoding="utf-8")

    def dump_info(self, header=HEADER) -> uprof_dump.DumpInfo:
        info = self.dump_beside(header)
        return uprof_dump.DumpInfo(info.path, "reused", cache_dir=self.cache)

    def assert_not_followed(self, link: Path):
        self.assertEqual(self.secret.read_text(encoding="utf-8"), "secret")
        self.assertFalse(link.is_symlink(), link)

    @unittest.skipIf(F.duckdb_skip(), F.duckdb_skip())
    def test_a_linked_samples_sidecar_is_replaced_not_followed(self):
        link = self.cache / "IbsOpDump.samples.parquet.json"
        self.link_file(link, self.secret)
        self.assertIsNotNone(uprof_dump.samples_parquet(self.dump_info(), self.session))
        self.assert_not_followed(link)

    @unittest.skipIf(F.duckdb_skip(), F.duckdb_skip())
    def test_a_linked_unsupported_marker_is_replaced_not_followed(self):
        link = self.cache / "IbsOpDump.samples.parquet.unsupported.json"
        self.link_file(link, self.secret)
        header = HEADER.replace(",IbsTagToRetCtr", "")
        self.assertIsNone(uprof_dump.samples_parquet(self.dump_info(header), self.session))
        self.assert_not_followed(link)

    def test_a_linked_cache_json_is_replaced_not_followed(self):
        link = self.cache / "cache.json"
        self.link_file(link, self.secret)
        uprof_dump._write_cache_meta(
            link, self.session, "ibsop-event-dump", None, self.cache / "IbsOpDump.csv", []
        )
        self.assert_not_followed(link)


if __name__ == "__main__":
    unittest.main()
