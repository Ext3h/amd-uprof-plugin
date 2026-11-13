"""--image matches a process image's file name and also its full path (#355).

Real uProf stores Process.name as the full image path, so a pattern matched against the path
alone selects nothing for a bare file name such as `defect_lab.exe`. The session here stores
full paths, as real captures do.

Run from the repository root: python tests/run_tests.py test_image_match
"""

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import duckdb

import uprof_db
import uprof_query

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
TARGET = "c:\\capture\\defect_lab.exe"
OTHER = "c:\\tools\\other.exe"


class ImageMatchTest(unittest.TestCase):
    def scope(self, image: str) -> tuple[list[int], list[tuple[str, str]]]:
        """The PIDs the --image pattern selects and the empty-scope fields it prints."""
        connection = duckdb.connect(":memory:")
        try:
            connection.execute("CREATE TABLE Process(id UINTEGER, name VARCHAR)")
            connection.execute(
                "CREATE TABLE Module(id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)"
            )
            connection.execute(
                "CREATE TABLE Function(id UBIGINT, moduleId UINTEGER, "
                "name VARCHAR, startOffset UBIGINT, size UBIGINT, "
                "srcfileId UINTEGER)"
            )
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries(moduleId UINTEGER, "
                "functionId UBIGINT, moduleRVA UBIGINT, processId UINTEGER, "
                'coreId UINTEGER, "event#0x76" BIGINT)'
            )
            connection.execute("INSERT INTO Process VALUES (100, ?), (200, ?)", [TARGET, OTHER])
            connection.execute(
                "INSERT INTO Module VALUES (1, 'c:\\capture\\defect_lab.exe', false)"
            )
            connection.execute("INSERT INTO Function VALUES (1, 1, 'main', 4096, 64, 0)")
            connection.execute(
                "INSERT INTO UnifiedSampleSeries VALUES "
                "(1, 1, 4096, 100, 0, 5), (1, 1, 4096, 200, 1, 7)"
            )
            uprof_db.set_variables(
                connection,
                {
                    "uprof_ibs_dump": "",
                    "uprof_ibs_parquet": "",
                    "uprof_symbols": "",
                    "uprof_aliases": "",
                    "uprof_pdbs": "",
                    "uprof_functions": "",
                    "uprof_event_column": "event#0x76",
                    "uprof_primary_measure": "samples",
                    "uprof_time_start": "0",
                    "uprof_time_end": "1000000",
                    "uprof_top": "20",
                    "uprof_pid": "",
                    "uprof_image": image,
                    "uprof_by": "process",
                    "uprof_module": "",
                    "uprof_function": "",
                    "uprof_line": "",
                    "uprof_instruction": "",
                },
            )
            uprof_db.run_script(connection, TOOLS_DIR / "views.sql")
            pids = sorted(
                int(row[0])
                for row in connection.execute("SELECT DISTINCT pid FROM v_scoped").fetchall()
            )
            with redirect_stderr(io.StringIO()):
                fields = uprof_query._scope_warning(connection, image)
        finally:
            connection.close()
        return pids, fields

    def test_file_name_pattern_and_full_path_each_select_the_process(self):
        for image in ("defect_lab.exe", "DEFECT_LAB.EXE", "%defect_lab%", "defect_%.exe", TARGET):
            with self.subTest(image):
                pids, fields = self.scope(image)
                self.assertEqual(pids, [100])
                self.assertEqual(fields, [])

    def test_a_name_that_matches_no_image_still_warns(self):
        pids, fields = self.scope("missing.exe")

        self.assertEqual(pids, [])
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0][0], "WARNING")
        self.assertIn(TARGET, fields[0][1])


if __name__ == "__main__":
    unittest.main()
