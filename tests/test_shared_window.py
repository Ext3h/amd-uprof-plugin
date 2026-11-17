"""Instruction JSON: overlapping context windows are printed once and shared by their rows.

Run from the repository root: python tests/run_tests.py test_shared_window
"""
# Spec: SPEC-86, SPEC-88, SPEC-130

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
import uprof_context
from run_tests import HOST_SKIP

REPO = Path(__file__).resolve().parents[1]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"
sys.path.insert(0, str(REPO / "trials"))
import run_trials


def entry(rva, samples=1, **extra):
    return {
        "rva": rva,
        "instruction": f"nop{rva}",
        "samples": samples,
        "sample_share": 0.1,
        **extra,
    }


def context(runs, function="f", file="f.c", anchor="0x1000"):
    block = {"anchor": {"kind": "pdata", "rva": anchor, "distance": 0}, "build": {"verified": True}}
    if function is not None:
        block["function"] = function
    if file is not None:
        block["file"] = file
    block["runs"] = runs
    return block


def window_rvas(window):
    return [item["rva"] for run in window["runs"] for item in run["instructions"]]


class ShareWindowsTest(unittest.TestCase):
    def test_overlapping_windows_of_one_module_are_printed_once(self):
        first = context([{"line": 1, "instructions": [entry("0x1000"), entry("0x1002")]}])
        second = context(
            [
                {"line": 1, "instructions": [entry("0x1002")]},
                {"line": 2, "instructions": [entry("0x1004", 3)]},
            ]
        )

        contexts, windows = uprof_context.share_windows(["m.dll", "m.dll"], [first, second])

        self.assertEqual(len(windows), 1)
        self.assertEqual(window_rvas(windows[0]), ["0x1000", "0x1002", "0x1004"])
        self.assertEqual(windows[0]["module"], "m.dll")
        self.assertEqual(windows[0]["function"], "f")
        self.assertEqual(
            windows[0]["runs"],
            [
                {"line": 1, "instructions": [entry("0x1000"), entry("0x1002")]},
                {"line": 2, "instructions": [entry("0x1004", 3)]},
            ],
        )
        # Each row keeps its own anchor, build, function and file and names its window and the
        # range of it that its own decode covered, so nothing of the per-row block is lost.
        self.assertEqual(
            contexts[0],
            {
                "anchor": first["anchor"],
                "build": first["build"],
                "function": "f",
                "file": "f.c",
                "window": {"index": 0, "first": "0x1000", "last": "0x1002"},
            },
        )
        self.assertEqual(contexts[1]["window"], {"index": 0, "first": "0x1002", "last": "0x1004"})

    def test_disjoint_windows_and_other_modules_get_their_own_window(self):
        low = context([{"line": 1, "instructions": [entry("0x1000")]}])
        high = context([{"line": 9, "instructions": [entry("0x2000")]}], anchor="0x2000")
        other = context([{"line": 1, "instructions": [entry("0x1000")]}])

        contexts, windows = uprof_context.share_windows(
            ["m.dll", "m.dll", "n.dll"], [low, high, other]
        )

        self.assertEqual([window["module"] for window in windows], ["m.dll", "m.dll", "n.dll"])
        self.assertEqual([item["window"]["index"] for item in contexts], [0, 1, 2])

    def test_a_run_frame_differing_from_the_window_is_kept(self):
        inlined = context(
            [
                {"line": 1, "instructions": [entry("0x1000")]},
                {"function": "g", "file": "g.h", "line": 4, "instructions": [entry("0x1002")]},
                {"function": None, "file": None, "line": None, "instructions": [entry("0x1004")]},
            ]
        )
        later = context(
            [{"function": "g", "file": "g.h", "line": 4, "instructions": [entry("0x1002")]}],
            function="g",
            file="g.h",
        )

        _contexts, windows = uprof_context.share_windows(["m", "m"], [inlined, later])

        self.assertEqual(len(windows), 1)
        self.assertEqual(
            [{k: v for k, v in run.items() if k != "instructions"} for run in windows[0]["runs"]],
            [
                {"line": 1},
                {"function": "g", "file": "g.h", "line": 4},
                {"function": None, "file": None, "line": None},
            ],
        )

    def test_omitted_and_missing_contexts_pass_through(self):
        omitted = {"omitted": True, "reason": "row_cap"}

        contexts, windows = uprof_context.share_windows(["m", None], [omitted, None])

        self.assertEqual(contexts, [omitted, None])
        self.assertEqual(windows, [])

    def test_document_help_names_the_shared_window(self):
        contexts, windows = uprof_context.share_windows(
            ["m"], [context([{"line": 1, "instructions": [entry("0x1", ibs={"samples": 1})]}])]
        )

        fields = uprof_context.context_help({}, contexts, windows)["fields"]

        self.assertIn("window", fields)
        self.assertIn("windows[].runs[].instructions[].ibs", fields)
        self.assertNotIn("runs", fields)


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
@unittest.skipIf(F.tool_skip(), F.tool_skip() or "")
# The context reads the PDB that symbolization verified by GUID and age; that verification runs
# only on Windows, so elsewhere every lab_core.dll context omits with pdb_unverified.
@unittest.skipUnless(os.name == "nt", HOST_SKIP + "PDB verification runs only on Windows")
class SharedWindowCaptureTest(unittest.TestCase):
    """The default instruction document reaches the IBS cause evidence of the defect lab."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="uprof-shared-window-")
        cls.root = Path(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def document(self, capture: str, function: str) -> dict:
        folder = run_trials.committed_folder(f"defect_lab/{capture}", "HEAD")
        target = self.root / capture
        run_trials.extract("HEAD", folder, target / "capture")
        images = target / run_trials.IMAGES
        run_trials.extract("HEAD", f"{folder.rsplit('/', 1)[0]}/{run_trials.IMAGES}", images)
        result = subprocess.run(
            [
                sys.executable,
                str(UPROF),
                "profile",
                "--session",
                str(target / "capture"),
                "--by",
                "instruction",
                "--image",
                "%defect_lab.exe",
                "--function",
                function,
                "--module",
                "c:/capture/lab_core.dll",
                "--image-path",
                str(images),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def entries(self, document: dict) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for window in document["windows"]:
            for run in window["runs"]:
                for item in run["instructions"]:
                    self.assertNotIn(item["rva"], found, "a window entry is printed twice")
                    found[item["rva"]] = item
        return found

    def assert_rows_refer_to_windows(self, document: dict) -> None:
        for row in document["rows"]:
            block = row.get("context")
            if block and not block.get("omitted"):
                self.assertNotIn("runs", block)
                self.assertLess(block["window"]["index"], len(document["windows"]))

    def test_branch_mispredicts_are_in_the_shared_window(self):
        document = self.document("branch.ibs", "lab_branch")

        self.assertEqual(len(document["windows"]), 1)
        self.assert_rows_refer_to_windows(document)
        branch = self.entries(document)["0x1376"]
        self.assertEqual(branch["ibs"]["IbsOpBrnMisp"], {"set": 19, "of": 36})

    def test_chain_divide_carries_its_ibs_fields(self):
        document = self.document("chain.ibs", "lab_chain")

        self.assertEqual(len(document["windows"]), 1)
        self.assert_rows_refer_to_windows(document)
        divide = self.entries(document)["0x1676"]
        self.assertTrue(divide["instruction"].startswith("divq"))
        self.assertEqual(divide["ibs"]["IbsDcMiss"], {"set": 0, "of": 67})
        self.assertEqual(divide["ibs"]["IbsTagToRetCtr"]["sum"], 13601)


if __name__ == "__main__":
    unittest.main()
