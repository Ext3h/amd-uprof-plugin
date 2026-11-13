"""Hostile capture files fail with a cause instead of exhausting memory or writing through links.

--out-file is written through a temporary file beside the destination; a link planted at a
predictable temporary name would redirect the write. An oversized session.uprof or a dump field
larger than any real dump cell is refused with a one-line cause, not a traceback.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_native
import uprof_output
import uprof_session
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


class SessionMetaSizeTest(unittest.TestCase):
    def test_an_oversized_session_file_is_refused_with_its_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "session.uprof"
            with open(meta, "wb") as handle:
                handle.truncate(uprof_util.SESSION_META_LIMIT + 1)
            with self.assertRaisesRegex(uprof_util.UprofError, "session.uprof") as raised:
                uprof_session.read_meta(tmp)
            self.assertIn(str(uprof_util.SESSION_META_LIMIT + 1), str(raised.exception))


class DumpFieldTest(unittest.TestCase):
    def test_a_field_larger_than_any_dump_cell_is_a_cause_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "IbsOpDump.csv"
            dump.write_text(
                "DeltaTick,CPU_Number,ModuleName\n1,0," + "x" * 200_000 + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(uprof_util.UprofError, str(dump).replace("\\", "\\\\")):
                uprof_native.dump_cadence(dump)


if __name__ == "__main__":
    unittest.main()
