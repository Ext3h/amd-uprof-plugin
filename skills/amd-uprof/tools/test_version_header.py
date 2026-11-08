"""Every profile identifies the skill version and the copy that produced it.

Two installed copies can share a version and still differ, so the line carries both the version
from the nearest plugin.json and the skill directory. A loose copy has no manifest; it reports
the version built into the code instead. This is the output-side half of issue #28.

Run from this directory: python -m unittest test_version_header -v
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

import uprof_util

TOOLS_DIR = Path(__file__).resolve().parent
EVENT_COLUMN = "event#0x3000076"


class SkillIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_nearest_plugin_manifest_supplies_the_version(self):
        manifest = self.root / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"name": "amd-uprof", "version": "9.9.9"}),
                            encoding="utf-8")

        text = uprof_util.skill_identity(self.root)

        self.assertTrue(text.startswith("amd-uprof 9.9.9 (plugin.json "), text)
        self.assertIn(str(manifest.resolve()), text)
        self.assertIn(f"skill dir {self.root.resolve()}", text)

    def test_loose_copy_falls_back_to_the_built_in_version(self):
        text = uprof_util.skill_identity(self.root)

        self.assertTrue(text.startswith(f"amd-uprof {uprof_util._BUILTIN_VERSION} (built-in); "),
                        text)
        self.assertIn(f"skill dir {self.root.resolve()}", text)

    def test_malformed_manifest_falls_back_instead_of_failing(self):
        manifest = self.root / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir()
        manifest.write_text("{not json", encoding="utf-8")

        text = uprof_util.skill_identity(self.root)

        self.assertIn("(built-in)", text)


class VersionHeaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Path(self.tmp.name) / "session"
        self.session.mkdir()
        self._write_capture()

    def _write_capture(self):
        event = {"hwId": 118, "interval": 4_700_000, "isSampling": True, "user": True,
                 "os": False, "encodedEventId": 0x03000076}
        meta = {
            "collection": {
                "schedule": {"elapsed": 1000},
                "events": {"pmcEvents": {"pmcEventList": [event]}},
                "target": {"systemWide": True},
            },
            "auxiliary": {},
        }
        (self.session / "session.uprof").write_text(json.dumps(meta), encoding="utf-8")
        connection = duckdb.connect(str(self.session / "cpu.db"))
        try:
            connection.execute("CREATE TABLE Process (id UINTEGER, name VARCHAR)")
            connection.execute("CREATE TABLE Module (id UINTEGER, path VARCHAR)")
            connection.execute("CREATE TABLE Function (id UBIGINT, moduleId UINTEGER, "
                               "name VARCHAR)")
            connection.execute(
                "CREATE TABLE UnifiedSampleSeries (processId UINTEGER, threadId UINTEGER, "
                "moduleId UINTEGER, functionId UBIGINT, moduleRVA UBIGINT, coreId UINTEGER, "
                f'"{EVENT_COLUMN}" DOUBLE)')
            connection.execute(r"INSERT INTO Process VALUES (100, 'c:\workload.exe')")
            connection.execute(r"INSERT INTO Module VALUES (1, 'c:\workload.exe')")
            connection.execute("INSERT INTO Function VALUES (1, 1, 'main')")
            connection.execute(
                "INSERT INTO UnifiedSampleSeries VALUES (100, 1, 1, 1, 4096, 0, 5)")
        finally:
            connection.close()

    def profile(self, *extra):
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "uprof.py"), "profile",
             "--session", str(self.session), "--by", "process", *extra],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_markdown_and_json_carry_the_same_version_line(self):
        output = self.profile()
        line = next(line for line in output.splitlines() if line.startswith("VERSION: "))
        value = line.removeprefix("VERSION: ")
        document = json.loads(self.profile("--json"))

        self.assertEqual(document["VERSION"], value)
        self.assertTrue(value.startswith("amd-uprof "), value)
        self.assertIn("skill dir ", value)

    def test_version_identifies_this_installed_copy(self):
        # Running from the repository or a plugin install finds the manifest above the skill.
        value = json.loads(self.profile("--json"))["VERSION"]
        self.assertRegex(value, r"\((plugin\.json |built-in)")


if __name__ == "__main__":
    unittest.main()
