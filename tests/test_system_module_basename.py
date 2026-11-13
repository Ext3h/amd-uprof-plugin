"""Verify isSystemModule fallback to basename for full-path dump module names.

Issue #281: isSystemModule and function joins have no basename fallback for full-path dump module names.
When a dump's ModuleName carries a directory differing from the catalog path (e.g. D:/mirror/ntdll.dll
vs C:/Windows/System32/ntdll.dll), the exact join fails; the basename fallback must supply the flag,
while ambiguous basenames across disagreeing catalog paths must yield NULL (#87).

Run from repository root: python tests/run_tests.py test_system_module_basename
"""

import json
from pathlib import Path
import tempfile
import unittest

import duckdb
import uprof_db

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


def _meta():
    return {
        "version": {"major": 5, "minor": 0, "build": 0},
        "collection": {
            "schedule": {"elapsed": 10_000},
            "events": {"ibsEvents": {"op": True, "fetch": False, "opInterval": 4_700_000,
                                     "opCycleCount": True}},
            "target": {"systemWide": True},
        },
        "auxiliary": {"ibsOpSampling": True},
    }


class SystemModuleBasenameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _create_session(self, modules, dump_modules):
        session = self.root / "session"
        session.mkdir()
        (session / "session.uprof").write_text(json.dumps(_meta()), encoding="utf-8")

        connection = duckdb.connect(str(session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT)")
            connection.execute("INSERT INTO Process VALUES (100, 'workload.exe')")
            for module_id, path, system in modules:
                connection.execute("INSERT INTO Module VALUES (?, ?, ?)", [module_id, path, system])
        finally:
            connection.close()

        dump_rows = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName,IbsTagToRetCtr"]
        tick = 0
        for mod in dump_modules:
            tick += 1_000_000_000
            dump_rows.append(f"{tick},0,1,100,4096,{mod},100")
        (session / "IbsOpDump.csv").write_text("\n".join(dump_rows) + "\n", encoding="utf-8", newline="\n")
        return session

    def run_query(self, session, sql_file="profile-module.sql"):
        variables = {
            "uprof_image": "%", "uprof_pid": "", "uprof_top": "0",
            "uprof_time_start": "0", "uprof_time_end": "9223372036854775807",
            "uprof_ibs_dump": str(session / "IbsOpDump.csv"),
            "uprof_event_column": "", "uprof_session_meta": str(session / "session.uprof"),
            "uprof_tag_available": "true", "uprof_symbols": "", "uprof_aliases": "",
            "uprof_pdbs": "", "uprof_sample_interval_s": "0.001",
            "uprof_functions": "",
        }
        with uprof_db.open_query(session / "cpu.db", [TOOLS_DIR / "views.sql"], variables) as connection:
            columns, rows = uprof_db.fetch(connection, TOOLS_DIR / sql_file)
        dicts = [dict(zip(columns, row)) for row in rows]
        return {d["module"]: d for d in dicts if "module" in d}

    def test_differing_directory_resolves_system_flag_via_basename(self):
        # Catalog has C:/Windows/System32/ntdll.dll (system=true)
        # Dump has D:/mirror/ntdll.dll
        modules = [(1, r"C:\Windows\System32\ntdll.dll", True)]
        dump_modules = [r"D:\mirror\ntdll.dll"]
        session = self._create_session(modules, dump_modules)
        rows = self.run_query(session, "profile-module.sql")
        row = rows.get("d:/mirror/ntdll.dll")
        self.assertIsNotNone(row)
        self.assertIs(row["isSystemModule"], True)

        fn_rows = self.run_query(session, "profile-function.sql")
        fn_row = fn_rows.get("d:/mirror/ntdll.dll")
        self.assertIsNotNone(fn_row)
        self.assertIs(fn_row["isSystemModule"], True)

        region_rows = self.run_query(session, "profile-region.sql")
        region_row = region_rows.get("d:/mirror/ntdll.dll")
        self.assertIsNotNone(region_row)
        self.assertIs(region_row["isSystemModule"], True)

    def test_bare_basename_resolves_system_flag(self):
        modules = [(1, r"C:\Windows\System32\ntdll.dll", True)]
        dump_modules = ["ntdll.dll"]
        session = self._create_session(modules, dump_modules)
        rows = self.run_query(session)
        row = rows.get("ntdll.dll")
        self.assertIsNotNone(row)
        self.assertIs(row["isSystemModule"], True)

    def test_ambiguous_basename_resolves_to_null_for_unmatched_path(self):
        # Catalog has conflicting system flags for ambiguous.dll
        modules = [
            (1, r"C:\App\ambiguous.dll", False),
            (2, r"C:\Windows\System32\ambiguous.dll", True),
        ]
        dump_modules = [
            r"C:\App\ambiguous.dll",
            r"C:\Windows\System32\ambiguous.dll",
            r"D:\mirror\ambiguous.dll",
        ]
        session = self._create_session(modules, dump_modules)
        rows = self.run_query(session)

        # Exact matches must preserve their distinct classifications
        self.assertIs(rows["c:/app/ambiguous.dll"]["isSystemModule"], False)
        self.assertIs(rows["c:/windows/system32/ambiguous.dll"]["isSystemModule"], True)
        # Ambiguous fallback must return NULL (#87)
        self.assertIsNone(rows["d:/mirror/ambiguous.dll"]["isSystemModule"])


if __name__ == "__main__":
    unittest.main()
