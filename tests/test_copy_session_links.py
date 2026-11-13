"""copy_session refuses a capture that contains links instead of copying what they point at.

A capture archive can carry symlinks or junctions. Followed, `sources -> ~/.ssh` copies private
files into the output, `sources -> /` copies a whole disk, and a link to its own parent copies
the tree into itself until the path limit.
"""

import os
import tempfile
import unittest
from pathlib import Path

import uprof_util

LINK = "contains a link"


class CopySessionLinkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="uprof-copylinks-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.capture = self.base / "capture"
        (self.capture / "cpu").mkdir(parents=True)
        (self.capture / "binaries").mkdir()
        (self.capture / "session.uprof").write_text("{}", encoding="utf-8")
        (self.capture / "cpu" / "a.prd").write_bytes(b"raw")
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "id_rsa"
        self.secret.write_text("secret", encoding="utf-8")
        self.out = self.base / "out"

    def link_dir(self, link: Path, target: Path) -> None:
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

    def assert_refused(self, link: Path):
        with self.assertRaisesRegex(uprof_util.UprofError, LINK) as raised:
            uprof_util.copy_session(self.capture, self.out)
        self.assertIn(str(link), str(raised.exception))
        self.assertIn("re-run", str(raised.exception))
        copied = [p for p in self.out.rglob("*")] if self.out.exists() else []
        self.assertFalse([p for p in copied if p.name == self.secret.name], copied)

    def test_a_linked_raw_directory_is_refused(self):
        link = self.capture / "sources"
        self.link_dir(link, self.outside)
        self.assert_refused(link)

    def test_a_link_deep_inside_a_raw_directory_is_refused(self):
        (self.capture / "binaries" / "x64").mkdir()
        link = self.capture / "binaries" / "x64" / "keys"
        self.link_dir(link, self.outside)
        self.assert_refused(link)

    def test_a_self_referencing_link_is_refused_without_copying(self):
        link = self.capture / "binaries" / "loop"
        self.link_dir(link, self.capture / "binaries")
        self.assert_refused(link)
        self.assertFalse(self.out.exists() and any(self.out.rglob("loop")))

    def test_a_linked_raw_file_is_refused(self):
        link = self.capture / "cpu" / "b.prd"
        self.link_file(link, self.secret)
        self.assert_refused(link)

    def test_a_linked_session_file_is_refused(self):
        (self.capture / "session.uprof").unlink()
        link = self.capture / "session.uprof"
        self.link_file(link, self.secret)
        self.assert_refused(link)

    def test_a_capture_without_links_is_copied(self):
        uprof_util.copy_session(self.capture, self.out)
        self.assertEqual((self.out / "cpu" / "a.prd").read_bytes(), b"raw")


if __name__ == "__main__":
    unittest.main()
