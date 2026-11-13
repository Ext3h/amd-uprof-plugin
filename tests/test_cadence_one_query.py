"""translate --ascii and profile report one ACHIEVED cadence from one SQL query (#301).

Both commands summarize the same dump: per-context sample counts and median gaps. They must
print the same ACHIEVED text for the same translated session, so a fix to the query reaches
both. The dump holds three CPUs with different cadences; one row names a module and a process
that cpu.db does not record.

Run from the repository root: python tests/run_tests.py test_cadence_one_query
"""
# Spec: SPEC-24

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import duckdb

import uprof_native

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
DUMP = (
    "DeltaTick,CPU_Number,TID,PID,Offset,ModuleName\n"
    "0,0,1,100,4096,c:\\capture\\app.exe\n"
    "1000000,0,1,100,4096,c:\\capture\\app.exe\n"
    "2000000,0,1,100,4096,c:\\capture\\app.exe\n"
    "3000000,0,1,100,4096,c:\\capture\\app.exe\n"
    "500000,1,2,100,4096,c:\\capture\\app.exe\n"
    "4500000,1,2,100,4096,c:\\capture\\app.exe\n"
    "2500000,2,9,4,8192,c:\\windows\\system32\\ntoskrnl.exe\n"
)


class CadenceOneQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(os.path.realpath(self.tmp.name))
        self.source = root / "capture"
        (self.source / "cpu").mkdir(parents=True)
        (self.source / "cpu" / "raw.prd").write_bytes(b"raw")
        meta = {
            "translateCommand": "AMDuProfCLI.exe translate -i session --ascii ibsop-event-dump",
            "collection": {
                "schedule": {"elapsed": 5},
                "events": {
                    "ibsEvents": {
                        "op": {"interval": 4_700},
                        "opInterval": 4_700,
                        "opCycleCount": False,
                    }
                },
                "target": {"systemWide": True},
            },
            "auxiliary": {"ibsOpSampling": True},
        }
        (self.source / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        self.cpu_db = root / "made" / "cpu.db"
        self.cpu_db.parent.mkdir()
        connection = duckdb.connect(str(self.cpu_db))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute(
                "CREATE TABLE Module (id UINTEGER, path VARCHAR, isSystemModule BOOLEAN)"
            )
            connection.execute(
                "CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, name VARCHAR)"
            )
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries (moduleId UINTEGER, "
                "functionId UBIGINT, moduleRVA UBIGINT)"
            )
            connection.execute("INSERT INTO Process VALUES (100, 'c:\\capture\\app.exe')")
            connection.execute("INSERT INTO Module VALUES (1, 'c:\\capture\\app.exe', false)")
            connection.execute("INSERT INTO Function VALUES (10, 1, 'main')")
            connection.execute("INSERT INTO UnifiedSampleSeries VALUES (1, 10, 4096)")
        finally:
            connection.close()
        self.out = root / "translated"

    def fake_translate(self, command, **_kwargs):
        shutil.copy2(self.cpu_db, self.out / "cpu.db")
        (self.out / "IbsOpDump.csv").write_text(DUMP, encoding="utf-8", newline="\n")
        return subprocess.CompletedProcess(command, 0, "Translation finished\n", "")

    @staticmethod
    def achieved(text):
        return next(
            line for line in text.splitlines() if line.startswith("ACHIEVED: ")
        ).removeprefix("ACHIEVED: ")

    def test_translate_and_profile_print_the_same_achieved_text(self):
        stream = io.StringIO()
        with (
            mock.patch.object(uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")),
            mock.patch.object(uprof_native, "run_watched", side_effect=self.fake_translate),
            redirect_stdout(stream),
        ):
            uprof_native.translate(self.source, self.out, ascii_kind="ibsop-event-dump")
        translated = self.achieved(stream.getvalue())

        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(self.out),
                "--by",
                "process",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(translated, self.achieved(result.stdout))


if __name__ == "__main__":
    unittest.main()
