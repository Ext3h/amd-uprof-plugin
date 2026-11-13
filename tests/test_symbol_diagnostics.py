"""Symbolization diagnostics on stderr name each distinct cause once, for the view's scope only.

On a capture with many modules, per-module progress and warning lines repeated the same cause
once per module and on every symbolizing call, also for modules outside a --module scope. The
profile's stderr now drops lines of modules outside the scope, drops the progress lines of
modules that resolved no symbols, and prints each distinct cause once with the modules it
covers. A cache hit prints what the cold run printed.

Run from the repository root: python tests/run_tests.py test_symbol_diagnostics
"""

import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
import uprof_query

REPO = Path(__file__).resolve().parents[1]
CAUSE = (
    "image GUID unknown age unknown; debug info recorded: no; the image's debug directory "
    "records no PDB; pass profile --symbol-path <file|dir>"
)


def digest(text: str, scope: str | None = None) -> str:
    output = io.StringIO()
    stream = uprof_query._SymbolDiagnostics(output, scope)
    for chunk in (text[:17], text[17:]):  # lines may arrive split across writes
        stream.write(chunk)
    stream.flush()
    return output.getvalue()


def unresolved(name: str, cause: str = CAUSE) -> str:
    return (
        f"MODULE: c:/bin/{name}: 3 addresses in C:\\bin\\{name}\nPDB=none\n"
        "FOLDS: skipped (no PDB; no public symbols to enumerate)\n"
        f"WARNING: no symbols resolved for c:/bin/{name} (C:\\bin\\{name}): {cause}\n"
    )


RESOLVED = (
    "MODULE: c:/bin/app.exe: 24 addresses in C:\\bin\\app.exe\nPDB_MATCH=yes\n"
    "FOLDS: 0 shared starts of 5 in C:\\tmp\\publics.csv\n"
)


class SymbolDiagnosticsTest(unittest.TestCase):
    def test_a_shared_cause_is_printed_once_with_its_modules(self):
        text = digest(unresolved("a.dll") + unresolved("b.dll") + unresolved("c.dll", "gone"))
        self.assertEqual(
            text.splitlines(),
            [
                f"WARNING: no symbols resolved for 2 modules (a.dll, b.dll): {CAUSE}",
                "WARNING: no symbols resolved for c:/bin/c.dll (C:\\bin\\c.dll): gone",
            ],
        )

    def test_past_five_modules_the_rest_is_counted(self):
        text = digest("".join(unresolved(f"m{n}.dll") for n in range(7)))
        self.assertEqual(
            text.splitlines(),
            [
                "WARNING: no symbols resolved for 7 modules (m0.dll, m1.dll, m2.dll, m3.dll, "
                f"m4.dll and 2 more): {CAUSE}"
            ],
        )

    def test_a_resolved_module_keeps_its_progress_lines(self):
        text = digest(RESOLVED + unresolved("a.dll"))
        self.assertEqual(
            text,
            RESOLVED + f"WARNING: no symbols resolved for c:/bin/a.dll (C:\\bin\\a.dll): {CAUSE}\n",
        )

    def test_lines_of_modules_outside_the_scope_are_dropped(self):
        ranges = (
            "FUNCTION_RANGES: c:/bin/a.dll: 3 ranges from C:\\bin\\a.dll\n"
            "FUNCTION_RANGES: c:/bin/app.exe: 4 ranges from C:\\bin\\app.exe\n"
        )
        text = digest(unresolved("a.dll") + RESOLVED + ranges, scope="C:\\BIN\\app.exe")
        self.assertEqual(
            text, RESOLVED + "FUNCTION_RANGES: c:/bin/app.exe: 4 ranges from C:\\bin\\app.exe\n"
        )

    def test_range_lines_are_summarized(self):
        text = digest(
            "".join(
                f"FUNCTION_RANGES: c:/bin/m{n}.dll: {n + 1} ranges from C:\\bin\\m{n}.dll\n"
                for n in range(3)
            )
        )
        self.assertEqual(
            text, "FUNCTION_RANGES: 6 ranges from 3 module images (m0.dll 1, m1.dll 2, m2.dll 3)\n"
        )

    def test_missing_ranges_share_their_reason(self):
        text = digest(
            "".join(
                f"WARNING: no function range derived for c:/bin/m{n}.dll "
                f"(C:\\bin\\m{n}.dll): no .pdata\n"
                for n in range(2)
            )
        )
        self.assertEqual(
            text, "WARNING: no function range derived for 2 modules (m0.dll, m1.dll): no .pdata\n"
        )

    def test_a_path_with_a_parenthesis_keeps_its_owner(self):
        path = "C:\\Program Files (x86)\\vendor\\app.exe"
        name = "c:/program files (x86)/vendor/app.exe"
        warning = f"WARNING: no symbols resolved for {name} ({path}): {CAUSE}"
        ranges = f"WARNING: no function range derived for {name} ({path}): no .pdata"
        block = (
            f"MODULE: {name}: 3 addresses in {path}\nPDB=none\n"
            "FOLDS: skipped (no PDB; no public symbols to enumerate)\n"
            f"{warning}\n{ranges}\n"
        )
        self.assertEqual(digest(block, scope=path).splitlines(), [warning, ranges])
        other = block.replace("vendor", "other")
        text = digest(block + other)
        self.assertIn("for 2 modules (app.exe, app.exe):", text)
        self.assertNotIn("MODULE: ", text)

    def test_a_quieted_module_still_drops_its_progress_lines(self):
        output = io.StringIO()
        stream = uprof_query._SymbolDiagnostics(output, None, quiet={"c:/bin/a.dll"})
        stream.write(unresolved("a.dll") + unresolved("b.dll"))
        stream.flush()
        self.assertEqual(
            output.getvalue().splitlines(),
            [f"WARNING: no symbols resolved for c:/bin/b.dll (C:\\bin\\b.dll): {CAUSE}"],
        )

    def test_other_lines_pass_through_in_order(self):
        text = digest("CACHE: hit\n" + unresolved("a.dll") + "NOTE: done")
        self.assertEqual(
            text.splitlines(),
            [
                "CACHE: hit",
                f"WARNING: no symbols resolved for c:/bin/a.dll (C:\\bin\\a.dll): {CAUSE}",
                "NOTE: done",
            ],
        )


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
@unittest.skipIf(F.READOBJ is None, "llvm-readobj is required; install LLVM")
# Off Windows the PE stand-ins take the DWARF path and print no "no symbols resolved" cause.
@unittest.skipIf(os.name != "nt", "host: the PE symbolization path runs on Windows only")
class ProfileStderrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "trials"))
        import build_captures

        cls.directory = tempfile.TemporaryDirectory(prefix="uprof-diagnostics-")
        root = Path(cls.directory.name) / "captures"
        cls.session = build_captures.build(root, ["systemwide"])["systemwide"]

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def stderr(self, by: str, *extra: str) -> tuple[dict, str]:
        return F.profile_with_stderr(self.session, by, extra)

    def test_the_function_view_prints_a_shared_cause_once(self):
        _document, stderr = self.stderr("function")
        warnings = [
            line
            for line in stderr.splitlines()
            if line.startswith("WARNING: no symbols resolved for")
        ]
        shared = [line for line in warnings if re.search(r"for \d+ modules \(", line)]
        self.assertEqual(len(shared), 1, stderr)
        self.assertIn("amdvlk64.dll", shared[0])
        self.assertIn("node.exe", shared[0])
        self.assertNotIn(
            "MODULE: ",
            "\n".join(
                line for line in stderr.splitlines() if "amdvlk64" in line or "node.exe" in line
            ),
        )

    def test_a_module_scoped_view_names_no_other_module(self):
        document, _ = self.stderr("function")
        # The driver stand-in exports fixture_entry too; scope to the own module's row.
        module = next(
            row["module"]
            for row in document["rows"]
            if row["function"] == "fixture_entry" and row["module"].endswith("fixture_full.exe")
        )
        _document, stderr = self.stderr("line", "--function", "fixture_entry", "--module", module)
        for other in ("amdvlk64.dll", "ntdll.dll", "node.exe", "vendorlib.dll"):
            self.assertNotIn(other, stderr)
        self.assertIn(f"MODULE: {module}: ", stderr)

    def test_a_cache_hit_prints_what_the_cold_run_printed(self):
        _document, first = self.stderr("function", "--pid", "4242")
        _document, second = self.stderr("function", "--pid", "4242")
        # Only a cold run creates and cleans an artifact folder; FOLDS names a file inside it.
        strip = re.compile(r"^(FOLDS:|ARTIFACTS=|ARTIFACTS_CLEANED=).*\n", re.M)
        self.assertEqual(strip.sub("", first), strip.sub("", second))


if __name__ == "__main__":
    unittest.main()
