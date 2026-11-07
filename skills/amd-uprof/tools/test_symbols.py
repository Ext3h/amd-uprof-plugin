"""Public-symbol cache aggregation.

Run from this directory: python -m unittest test_symbols -v
"""

import csv
import io
import tempfile
import unittest
from pathlib import Path

import uprof_symbols


class PublicSymbolsTest(unittest.TestCase):
    def setUp(self):
        try:
            import duckdb  # noqa: F401
        except ImportError:
            self.skipTest("DuckDB is required to aggregate public symbols")
        self.directory = tempfile.TemporaryDirectory(prefix="uprof-publics-")
        self.addCleanup(self.directory.cleanup)

    def aggregate(self, pairs_text):
        pairs = Path(self.directory.name) / "pairs.csv"
        out = Path(self.directory.name) / "publics.csv"
        pairs.write_text(pairs_text, encoding="utf-8", newline="\n")
        uprof_symbols._aggregate_publics(pairs, out)
        return list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))

    def test_a_single_name_start_keeps_its_name(self):
        rows = self.aggregate("rva,name\n4096,entry\n4112,a\n4112,b\n")

        self.assertEqual(rows[0]["names"], "entry")

    def test_a_shared_start_lists_every_name(self):
        rows = self.aggregate("rva,name\n4112,b\n4112,a\n")

        self.assertEqual(rows[0]["name_count"], "2")
        self.assertEqual(rows[0]["names"], "a | b")


if __name__ == "__main__":
    unittest.main()
