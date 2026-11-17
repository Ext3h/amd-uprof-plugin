"""A --pid or --image scope states its denominator and its share of the capture (#286).

Every share of a scoped view is a fraction of the scope, so a function at sample_share 0.40 of a
process holding 5 % of the capture is 2 % of the capture. The output names the scope as the
share base and gives the scope's sample count and its fraction of the unscoped capture, in the
JSON legend and in the text COLUMNS and READ lines.

Run from the repository root: python tests/run_tests.py test_scope_denominator
"""
# Spec: SPEC-02

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"

WORKLOAD = "c:\\app\\workload.exe"
OTHER = "c:\\app\\other.exe"
# The workload process holds 5 of the capture's 100 samples.
SAMPLES = {(4242, WORKLOAD): 5, (77, OTHER): 95}


def _meta():
    return {
        "translateCommand": "AMDuProfCLI.exe translate -i session --ascii ibsop-event-dump",
        "collection": {
            "schedule": {"elapsed": 10_000},
            "events": {
                "ibsEvents": {
                    "op": True,
                    "fetch": False,
                    "opInterval": 4_700_000,
                    "opCycleCount": True,
                }
            },
            "target": {"systemWide": True},
        },
        "auxiliary": {"ibsOpSampling": True},
    }


class ScopeDenominatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.session = Path(cls.tmp.name) / "scoped"
        cls.session.mkdir()
        (cls.session / "session.uprof").write_text(json.dumps(_meta()), encoding="utf-8")
        connection = duckdb.connect(str(cls.session / "cpu.db"))
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
            connection.execute("INSERT INTO Process VALUES (4242, ?), (77, ?)", [WORKLOAD, OTHER])
            connection.execute(
                "INSERT INTO Module VALUES (1, ?, false), (2, ?, false)", [WORKLOAD, OTHER]
            )
        finally:
            connection.close()
        rows = ["DeltaTick,CPU_Number,TID,PID,Offset,ModuleName,IbsTagToRetCtr"]
        tick = 0
        for (pid, module), count in SAMPLES.items():
            for _ in range(count):
                tick += 1_000_000
                rows.append(f"{tick},0,{pid},{pid},4096,{module},10")
        (cls.session / "IbsOpDump.csv").write_text(
            "\n".join(rows) + "\n", encoding="utf-8", newline="\n"
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def profile(self, *extra):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(self.session),
                "--by",
                "module",
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    @staticmethod
    def field(output, name):
        lines = [line for line in output.splitlines() if line.startswith(f"{name}:")]
        return lines[0] if lines else ""

    def test_a_pid_scope_states_its_samples_and_capture_fraction(self):
        legend = json.loads(self.profile("--pid", "4242", "--json"))["legend"]
        scope = legend["scope"]
        self.assertEqual(scope["filter"], "--pid 4242")
        self.assertEqual(scope["samples"], 5)
        self.assertEqual(scope["capture_samples"], 100)
        self.assertAlmostEqual(scope["sample_share"], 0.05)

        output = self.profile("--pid", "4242")
        self.assertIn("of the --pid 4242 scope", self.field(output, "COLUMNS"))
        read = self.field(output, "READ")
        self.assertIn("5 samples", read)
        self.assertIn("0.050 of the capture's 100 samples", read)

    def test_an_image_scope_names_the_image(self):
        legend = json.loads(self.profile("--image", "%workload.exe", "--json"))["legend"]
        self.assertEqual(legend["scope"]["filter"], "--image %workload.exe")
        self.assertEqual(legend["scope"]["samples"], 5)

    def test_an_unscoped_view_has_no_scope(self):
        legend = json.loads(self.profile("--json"))["legend"]
        self.assertNotIn("scope", legend)
        self.assertNotIn("of the --", self.field(self.profile(), "COLUMNS"))


if __name__ == "__main__":
    unittest.main()
