"""A llvm-readobj failure in the build-identity check stays with its module (#416).

The check reads each image's PE headers before symbolization and range derivation. A timeout or
a tool that cannot run there becomes that module's evidence like every other tool failure in a
module, so the other modules are still reported and profile exits 0.

Run from the repository root: python tests/run_tests.py test_readobj_timeout
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_functions
import uprof_query
import uprof_symbols
from uprof_native import ToolTimeout

TIMEOUT = "llvm-readobj.exe timed out after 300s"


def build_check(image, *_args):
    """A hanging readobj on hang.dll, another build for other.dll."""
    if Path(image).name == "hang.dll":
        raise ToolTimeout(TIMEOUT)
    return "the image is a different build than the capture recorded: CheckSum 0x2, capture 0x1"


class SymbolizationTest(unittest.TestCase):
    def test_a_readobj_timeout_fails_only_its_module(self):
        with tempfile.TemporaryDirectory(prefix="uprof-readobj-timeout-") as tmp:
            root = Path(tmp)
            modules = [(name, str(root / name), ["0x1000"]) for name in ("hang.dll", "other.dll")]
            stream = io.StringIO()
            with (
                mock.patch.object(
                    uprof_symbols, "resolve_image", side_effect=lambda name, path, *_: (path, "")
                ),
                mock.patch.object(uprof_symbols, "require_symbolizer", return_value="sym"),
                mock.patch.object(uprof_symbols, "find_sibling", return_value=Path("readobj")),
                mock.patch.object(uprof_symbols, "image_build_mismatch", side_effect=build_check),
            ):
                outcomes = uprof_symbols._symbolize_tasks(
                    modules,
                    root,
                    stream,
                    None,
                    (),
                    (),
                    (),
                    (),
                    identities={"hang.dll": (1, 0), "other.dll": (1, 0)},
                )

        by_name = {outcome.module_name: outcome for outcome in outcomes}
        self.assertEqual(by_name["hang.dll"].evidence.outcome, "failed")
        self.assertIn(TIMEOUT, by_name["hang.dll"].evidence.note)
        self.assertEqual(by_name["other.dll"].evidence.outcome, "wrong_build")


class RangeDerivationTest(unittest.TestCase):
    def test_a_readobj_timeout_derives_no_range_for_its_module_only(self):
        with tempfile.TemporaryDirectory(prefix="uprof-readobj-timeout-") as tmp:
            images = {name: Path(tmp) / name for name in ("hang.dll", "ok.dll")}
            for image in images.values():
                image.write_bytes(b"MZ")
            rows = [(name, str(path), 1, 2) for name, path in images.items()]
            stream = io.StringIO()
            connection = mock.Mock()
            connection.execute.return_value.fetchall.return_value = []
            derived = uprof_functions.RangeDerivation(ranges=[], reason="no function range")
            with (
                mock.patch.object(uprof_query.uprof_db, "fetch", return_value=([], rows)),
                mock.patch.object(
                    uprof_query,
                    "_module_identities",
                    return_value={"hang.dll": (1, 0), "ok.dll": (1, 0)},
                ),
                mock.patch.object(
                    uprof_query.uprof_context, "find_readobj", return_value="readobj"
                ),
                mock.patch.object(
                    uprof_symbols,
                    "image_build_mismatch",
                    side_effect=lambda image, *_: (
                        build_check(image) if Path(image).name == "hang.dll" else None
                    ),
                ),
                mock.patch.object(uprof_query, "_pool_workers", return_value=None),
                mock.patch.object(
                    uprof_functions, "derive_module_ranges", return_value=derived
                ) as derive,
            ):
                _path, info = uprof_query._derive_scope_ranges(connection, None, stream)

        absent = info["absent_by_module"]
        self.assertIn(TIMEOUT, absent["hang.dll"])
        self.assertIn("WARNING: no function range derived for hang.dll", stream.getvalue())
        # The other module is still derived.
        self.assertEqual(derive.call_count, 1)
        self.assertEqual(absent["ok.dll"], "no function range")


if __name__ == "__main__":
    unittest.main()
