"""_combine_artifacts joins real Parquet and CSV files, including Windows absolute paths."""

import tempfile
import unittest
from pathlib import Path

import duckdb
import uprof_symbols


class CombineArtifactsTest(unittest.TestCase):
    def test_the_join_path_is_forward_slashed(self):
        self.assertEqual(uprof_symbols._forward_slash(Path(r"C:\a\b.csv")), "C:/a/b.csv")

    def _parquet(self, path: Path, rows):
        with duckdb.connect() as connection:
            connection.execute(
                "CREATE TEMP TABLE rows(address VARCHAR, function VARCHAR, "
                "function_short VARCHAR, file VARCHAR, line INTEGER, "
                "containing_function VARCHAR)"
            )
            connection.executemany("INSERT INTO rows VALUES (?, ?, ?, ?, ?, ?)", rows)
            connection.execute("COPY rows TO ? (FORMAT parquet)", [str(path)])

    def _publics(self, path: Path, rows):
        lines = ["rva,name_count,names"]
        lines += [f"{rva},{count},{names}" for rva, count, names in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def test_real_files_keep_the_production_order_and_normalize_paths(self):
        with tempfile.TemporaryDirectory(prefix="uprof-combine-") as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            # Insertion order differs from the name order, and the rows inside one CSV are not
            # in rva order: the production ORDER BY position, rva must be visible in the result.
            first = root / "b.parquet"
            second = root / "a.parquet"
            self._parquet(first, [("0x20", "beta", "beta", "b.c", 2, "beta")])
            self._parquet(second, [("0x10", "alpha", "alpha", "a.c", 1, "alpha")])
            first_csv = root / "b.publics.csv"
            second_csv = root / "a.publics.csv"
            self._publics(first_csv, [(32, 1, "beta"), (16, 1, "alpha")])
            self._publics(second_csv, [(64, 1, "gamma")])

            symbols, publics = uprof_symbols._combine_artifacts(
                [("b.exe", first), ("a.exe", second)],
                [("b.exe", first_csv), ("a.exe", second_csv)],
                out,
            )

            self.assertTrue(symbols.is_file())
            self.assertIsNotNone(publics)
            with duckdb.connect() as connection:
                rows = connection.execute(
                    "SELECT module_name, address FROM read_parquet(?)", [str(symbols)]
                ).fetchall()
                joined = connection.execute(
                    "SELECT module_name, rva, names FROM read_csv(?, header = true)", [str(publics)]
                ).fetchall()
            self.assertEqual(rows, [("b.exe", "0x20"), ("a.exe", "0x10")])
            # The Windows paths are joined only after the backslash-to-slash normalization.
            self.assertEqual(
                joined, [("b.exe", 16, "alpha"), ("b.exe", 32, "beta"), ("a.exe", 64, "gamma")]
            )


if __name__ == "__main__":
    unittest.main()
