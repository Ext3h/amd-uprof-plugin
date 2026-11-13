"""A function or region view drops symbolizer advice for modules the uProf DB names fully.

The per-module "no symbols resolved" warning asks for --symbol-path/--image-path and a re-run.
When every in-scope sample of that module already has a uProf DB function name, a function or
region view would print the same rows after the re-run, so the line is dropped there; the cached
symbolization text keeps it for the line and instruction views, which need the image.
Run from the repository root: python tests/run_tests.py test_named_rows_warning
"""
# Spec: SPEC-67

import io
import unittest

import uprof_query

COVERAGE = {
    "rows": [
        {"module_name": "app.exe", "samples": 10, "named_samples": 10},
        {"module_name": "vendor.dll", "samples": 5, "named_samples": 2},
    ],
    "total": 15,
    "named": 12,
}


class NamedRowsWarningTest(unittest.TestCase):
    def test_only_fully_named_modules_are_quiet(self):
        self.assertEqual(uprof_query._fully_named_modules(COVERAGE), {"app.exe"})
        self.assertEqual(uprof_query._fully_named_modules(None), set())

    def test_the_filter_drops_only_those_modules_warning_lines(self):
        # The quiet module still counts as unresolved, so its progress line goes too.
        target = io.StringIO()
        stream = uprof_query._SymbolDiagnostics(target, None, quiet={"app.exe"})
        stream.write("MODULE: app.exe: 3 addresses in C:/app.exe\n")
        stream.write(
            "WARNING: no symbols resolved for app.exe (C:/app.exe): gone; "
            "pass --image-path\nWARNING: no symbols resolved for vendor.dll "
        )
        stream.write("(C:/vendor.dll): gone\n")
        stream.write("WARNING: no symbols resolved for app.exe.bak (C:/app.exe.bak): kept\n")
        stream.flush()
        self.assertEqual(
            target.getvalue(),
            "WARNING: no symbols resolved for vendor.dll (C:/vendor.dll): gone\n"
            "WARNING: no symbols resolved for app.exe.bak (C:/app.exe.bak): kept\n",
        )


if __name__ == "__main__":
    unittest.main()
