"""What an elevated collect may do.

collect is the one command meant to run elevated. With administrator rights it must not load the
analysis stack (DuckDB, LLVM wrappers, the caches), must delete nothing, and its remedies are the
tool's own fixed text, also when IBS is unavailable and it fails with alternatives.

Each run happens in a fresh interpreter, so sys.modules shows exactly what collect imported.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import uprof_shell

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"
ANALYSIS_MODULES = (
    "duckdb",
    "uprof_cache",
    "uprof_context",
    "uprof_db",
    "uprof_dump",
    "uprof_functions",
    "uprof_query",
    "uprof_symbols",
)

PROBE = textwrap.dedent("""
    import json, os, shutil, subprocess, sys
    from pathlib import Path
    from unittest import mock

    sys.path.insert(0, sys.argv[1])
    out_dir, report = Path(sys.argv[2]), Path(sys.argv[3])
    import uprof, uprof_collect

    def fake_run_cli(_uprof, spec, _target, options):
        return subprocess.CompletedProcess(spec, 1, "", "ibs counters are not available")

    deletes = []

    def forbid(name):
        def record(*args, **kwargs):
            deletes.append(name)
            raise PermissionError(f"{name} called by an elevated collect")
        return record

    with mock.patch.object(uprof_collect, "is_admin", return_value=True), \\
            mock.patch.object(uprof_collect, "find_uprof", return_value="AMDuProfCLI"), \\
            mock.patch.object(uprof_collect, "require_uprof", return_value="AMDuProfCLI"), \\
            mock.patch.object(uprof_collect, "ibs_unavailable_reason",
                              return_value="IBS hidden for the test"), \\
            mock.patch.object(uprof_collect, "_run_cli", side_effect=fake_run_cli), \\
            mock.patch.object(uprof_collect, "_core_frequency",
                              return_value=(4_700_000_000, "4700 MHz", "test")), \\
            mock.patch.object(shutil, "rmtree", forbid("shutil.rmtree")), \\
            mock.patch.object(os, "remove", forbid("os.remove")), \\
            mock.patch.object(os, "unlink", forbid("os.unlink")), \\
            mock.patch.object(os, "rmdir", forbid("os.rmdir")), \\
            mock.patch.object(Path, "unlink", forbid("Path.unlink")), \\
            mock.patch.object(Path, "rmdir", forbid("Path.rmdir")):
        code = uprof.main(["collect", "--out-dir", str(out_dir), "--seconds", "1",
                           "--event", "ibs-op"])
    report.write_text(json.dumps({"code": code, "deletes": deletes,
                                  "modules": sorted(sys.modules)}), encoding="utf-8")
""")


class ElevatedCollectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="uprof-elevated-")
        root = Path(os.path.realpath(cls._tmp.name))
        probe, report = root / "probe.py", root / "report.json"
        probe.write_text(PROBE, encoding="utf-8")
        cls.out_dir = root / "out"
        cls.out_dir.mkdir()
        (cls.out_dir / "earlier.txt").write_text("keep", encoding="utf-8")
        cls.result = subprocess.run(
            [sys.executable, "-I", str(probe), str(TOOLS_DIR), str(cls.out_dir), str(report)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        cls.report = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else None

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.assertIsNotNone(self.report, self.result.stdout + self.result.stderr)
        self.assertEqual(self.report["code"], 2, self.result.stdout + self.result.stderr)

    def test_the_analysis_stack_is_not_loaded(self):
        loaded = set(self.report["modules"])
        self.assertEqual(sorted(loaded.intersection(ANALYSIS_MODULES)), [])

    def test_nothing_is_deleted(self):
        self.assertEqual(self.report["deletes"], [])
        self.assertEqual((self.out_dir / "earlier.txt").read_text(encoding="utf-8"), "keep")

    def test_remedies_are_fixed_text(self):
        # With the paths the run used replaced, stderr is exactly the tool's own text.
        out = uprof_shell.path_arg(str(self.out_dir), "<OUT_DIR>")[0]
        tool = uprof_shell.path_arg(str(TOOLS_DIR / "uprof.py"), "<UPROF_PY>")[0]
        text = self.result.stderr.replace(tool, "<UPROF_PY>").replace(out, "<OUT_DIR>")
        text = text.replace(str(self.out_dir), "<OUT_DIR>")
        call = "python <UPROF_PY> collect --out-dir <OUT_DIR> --seconds 1"
        self.assertEqual(
            text.splitlines()[:4],
            [
                (
                    "WARNING: <OUT_DIR> exists; uProf will create an AMDuProf-* subdirectory to "
                    "avoid the collision"
                ),
                "error: IBS unavailable: IBS hidden for the test",
                f"ALTERNATIVE: cycle capture (same target): {call} --event cycles",
                f"ALTERNATIVE: timer capture (system-wide): {call} --event timer",
            ],
        )


if __name__ == "__main__":
    unittest.main()
