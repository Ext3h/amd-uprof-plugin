"""The defect_lab workload builds from its committed source and holds each class's code.

trials/defect_lab is a small freestanding program for the open-ended trial scenarios: one class
of defect per function in lab_core.dll. The test builds it from source with the same recipe the
timer fixture uses and checks what each class needs to reproduce: the class functions exist,
and each one's machine code holds the instructions that make its defect. It checks content, not
byte identity: the recipe does not make the images reproducible byte for byte.

Run from the repository root: python tests/run_tests.py test_defect_lab
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAB = REPO / "trials" / "defect_lab"
sys.path.insert(0, str(LAB))
import build

CLASSES = ("memory", "lock", "branch", "os", "vendor", "wait", "chain", "efficient")
IMAGES = ("defect_lab.exe", "lab_core.dll", "lab_vendor.dll", "lab_busy.exe")


def function_instructions(disassembly: str) -> dict[str, list[str]]:
    """Mnemonics per symbol of an llvm-objdump -d listing."""
    functions: dict[str, list[str]] = {}
    current = None
    for line in disassembly.splitlines():
        if match := re.match(r"^[0-9a-f]+ <([^>]+)>:$", line):
            current = match.group(1)
            functions[current] = []
        elif current and (match := re.match(r"^\s+[0-9a-f]+:\s+(\S+)", line)):
            functions[current].append(match.group(1))
    return functions


@unittest.skipIf(sys.platform != "win32", "host: defect_lab is a Windows program")
@unittest.skipIf(build.missing_tools(), build.missing_tools() or "")
class DefectLabBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="defect-lab-")
        cls.out = Path(cls.directory.name)
        build.build(cls.out)
        objdump = build.tool("llvm-objdump")
        listing = subprocess.run(
            [str(objdump), "-d", "--no-show-raw-insn", str(cls.out / "lab_core.obj")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        cls.functions = function_instructions(listing)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_every_image_and_pdb_is_built(self):
        for image in IMAGES:
            with self.subTest(image=image):
                self.assertTrue((self.out / image).is_file())
                self.assertTrue((self.out / image).with_suffix(".pdb").is_file())

    def test_every_class_has_its_function(self):
        for name in CLASSES:
            with self.subTest(defect_class=name):
                self.assertIn(f"lab_{name}", self.functions)

    def test_lock_takes_a_locked_compare_exchange_and_spins_with_pause(self):
        code = self.functions["lab_lock"]
        self.assertIn("lock", code)
        self.assertTrue(any(op.startswith("cmpxchg") for op in code))
        self.assertIn("pause", code)

    def test_chain_multiplies_and_divides(self):
        code = self.functions["lab_chain"]
        self.assertTrue(any(op.startswith("imul") for op in code))
        self.assertTrue(any(op.startswith("div") for op in code))

    def test_efficient_runs_vector_instructions(self):
        code = self.functions["lab_efficient"]
        self.assertIn("mulps", code)
        self.assertIn("addps", code)

    def test_branch_keeps_a_conditional_jump_and_no_conditional_move(self):
        code = self.functions["lab_branch"]
        self.assertTrue(any(re.fullmatch(r"j(?!mp)[a-z]+", op) for op in code))
        self.assertFalse([op for op in code if op.startswith("cmov")])

    def test_os_and_vendor_call_outside_code(self):
        self.assertIn("callq", self.functions["lab_os"])
        self.assertIn("callq", self.functions["lab_vendor"])

    def test_the_program_rejects_an_unknown_class(self):
        result = subprocess.run(
            [str(self.out / "defect_lab.exe"), "--class", "none"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: defect_lab --class", result.stdout)


if __name__ == "__main__":
    unittest.main()
