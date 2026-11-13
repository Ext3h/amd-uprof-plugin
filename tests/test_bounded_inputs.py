"""Hostile capture files fail with a cause instead of exhausting memory or writing through links.

--out-file is written through a temporary file beside the destination; a link planted at a
predictable temporary name would redirect the write. An oversized session.uprof or a dump field
larger than any real dump cell is refused with a one-line cause, not a traceback.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_native
import uprof_output
import uprof_session
import uprof_symbols
import uprof_util


class OutFileTempTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="uprof-bounded-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))

    def test_a_link_at_the_old_temporary_name_is_not_written_through(self):
        victim = self.root / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        out = self.root / "table.txt"
        link = out.with_name(out.name + ".tmp")
        try:
            link.symlink_to(victim)
        except OSError as exc:
            self.skipTest(f"host: cannot create a file symlink here: {exc}")
        uprof_output.write_payload("rows\n", out)
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")
        self.assertEqual(out.read_bytes(), b"rows\n")

    def test_concurrent_writers_use_distinct_temporary_files(self):
        out = self.root / "table.txt"
        names = []
        real_replace = os.replace

        def record(source, target):
            names.append(Path(source).name)
            return real_replace(source, target)

        with mock.patch.object(uprof_output.os, "replace", side_effect=record):
            uprof_output.write_payload("a\n", out)
            uprof_output.write_payload("b\n", out)
        self.assertEqual(len(set(names)), 2, names)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["table.txt"])

    @unittest.skipIf(os.name == "nt", "host: POSIX permission bits do not apply on Windows")
    def test_the_written_file_follows_the_umask_like_a_plain_create(self):
        out = self.root / "table.txt"
        old = os.umask(0o022)
        try:
            uprof_output.write_payload("rows\n", out)
        finally:
            os.umask(old)
        self.assertEqual(out.stat().st_mode & 0o777, 0o644)

    def test_skill_md_names_no_fixed_temporary_file(self):
        skill = (Path(uprof_output.__file__).resolve().parents[1] / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("<path>.tmp", skill)


class SessionMetaSizeTest(unittest.TestCase):
    def test_an_oversized_session_file_is_refused_with_its_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "session.uprof"
            with open(meta, "wb") as handle:
                handle.truncate(uprof_util.SESSION_META_LIMIT + 1)
            with self.assertRaisesRegex(uprof_util.UprofError, "session.uprof") as raised:
                uprof_session.read_meta(tmp)
            self.assertIn(str(uprof_util.SESSION_META_LIMIT + 1), str(raised.exception))
            self.assertIn(f"{uprof_util.SESSION_META_LIMIT}-byte limit", str(raised.exception))


class SymbolizerTimeoutTest(unittest.TestCase):
    def run_symbolizer(self, fake_run):
        with tempfile.TemporaryDirectory() as tmp:
            rvas = Path(tmp) / "rvas.txt"
            rvas.write_text("0x1000\n", encoding="utf-8")
            with mock.patch.object(uprof_symbols, "run", side_effect=fake_run):
                uprof_symbols._run_symbolizer(
                    Path("llvm-symbolizer"),
                    "app.exe",
                    None,
                    rvas,
                    Path(tmp) / "out.jsonl",
                    "linkage",
                )

    def test_one_module_gets_minutes_not_hours(self):
        seen = []

        def fake_run(command, timeout=None, **kwargs):
            seen.append(timeout)
            return subprocess.CompletedProcess(command, 0, "", "")

        self.run_symbolizer(fake_run)
        self.assertLessEqual(seen[0], 10 * 60)

    def test_a_timeout_names_a_runnable_alternative(self):
        def fake_run(command, timeout=None, **kwargs):
            raise uprof_native.ToolTimeout(f"llvm-symbolizer timed out after {timeout}s")

        with self.assertRaisesRegex(uprof_util.UprofError, "app.exe") as raised:
            self.run_symbolizer(fake_run)
        message = str(raised.exception)
        # The build-identity check refuses another build, so --image-path is no alternative.
        self.assertNotIn("--image-path", message)
        self.assertIn("profile --session <SESSION_DIR> --by module", message)


if __name__ == "__main__":
    unittest.main()
