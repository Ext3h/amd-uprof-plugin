"""Function-range derivation reads the image --image-path supplies, as symbolization does.

The capture names a module path that is not on this host; --image-path points at a directory
holding the captured build. The function rows must come from ranges derived from that image,
and the range cache must not serve a run made without the flag.

Run from the repository root: python tests/run_tests.py test_ranges_image_path
"""
# Spec: SPEC-56

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_cache
from test_function_region_profile import FIXTURE, READOBJ, SKIP, Session

TOOLS_DIR = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "amd-uprof" / "tools"


@unittest.skipUnless(READOBJ, SKIP)
class RangesImagePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="uprof-ranges-image-path-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.images = root / "images"
        self.images.mkdir()
        shutil.copyfile(FIXTURE, self.images / FIXTURE.name)
        absent = root / "elsewhere" / FIXTURE.name
        session = Session(root, "moved")
        session.module(1, absent, False).function(10, 1, "")
        session.sample(1, 10, 0x101B, 3).sample(1, 10, 0x1050, 2)
        self.session = session.write()

    def rows(self, *extra):
        result = subprocess.run(
            [
                sys.executable,
                str(TOOLS_DIR / "uprof.py"),
                "profile",
                "--session",
                str(self.session),
                "--by",
                "function",
                "--json",
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["rows"], result.stderr

    def test_ranges_come_from_the_image_path_image(self):
        rows, stderr = self.rows("--image-path", str(self.images))

        sources = {row["function"]: row["function_source"] for row in rows}
        self.assertEqual(sources.get("fixture_entry"), "export", stderr)
        self.assertNotIn("is not on this host", stderr)

    def test_without_the_flag_the_module_is_named_as_absent(self):
        rows, stderr = self.rows()

        self.assertEqual({row["function_source"] for row in rows}, {"none"})
        self.assertIn("is not on this host", stderr)


class RangesKeyTest(unittest.TestCase):
    def test_the_range_key_covers_the_image_path_image(self):
        with tempfile.TemporaryDirectory(prefix="uprof-ranges-key-") as directory:
            images = Path(directory)
            image = images / "fixture.exe"
            image.write_bytes(b"MZ-one")
            rows = [("fixture.exe", str(images / "absent" / "fixture.exe"), 1, 1)]
            with (
                mock.patch.object(uprof_cache.uprof_db, "fetch", return_value=(None, rows)),
                mock.patch.object(uprof_cache, "module_identity_rows", return_value=[]),
                mock.patch.object(uprof_cache, "tool_identities", return_value={"readobj": []}),
            ):
                without = uprof_cache.build_ranges_key(None, "symbols")
                first = uprof_cache.build_ranges_key(None, "symbols", [str(images)])
                image.write_bytes(b"MZ-other-build")
                second = uprof_cache.build_ranges_key(None, "symbols", [str(images)])

        self.assertNotEqual(without, first)
        self.assertNotEqual(first, second)

    def test_the_range_key_stats_a_remote_image_path_image(self):
        unc_image = "//host/share/fixture.exe"
        rows = [("fixture.exe", "//capture/share/fixture.exe", 1, 1)]
        stats = [
            mock.Mock(st_size=100, st_mtime_ns=100_000_000),
            mock.Mock(st_size=200, st_mtime_ns=200_000_000),
        ]
        with (
            mock.patch.object(uprof_cache.uprof_db, "fetch", return_value=(None, rows)),
            mock.patch.object(uprof_cache, "module_identity_rows", return_value=[]),
            mock.patch.object(uprof_cache, "tool_identities", return_value={"readobj": []}),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(Path, "stat", side_effect=stats),
        ):
            without = uprof_cache.build_ranges_key(None, "symbols")
            first = uprof_cache.build_ranges_key(None, "symbols", [unc_image])
            second = uprof_cache.build_ranges_key(None, "symbols", [unc_image])

        self.assertNotEqual(without, first)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
