"""A module or PDB path never reaches an LLVM tool as an option or a response file.

LLVM tools parse a positional argument that starts with '-' as an option and one that starts with
'@' as a response file to read options from. Paths come from the capture, so every path operand
is passed absolute, which can start with neither.
"""

import os
import types
import unittest
from pathlib import Path
from unittest import mock

import uprof_context
import uprof_functions
import uprof_symbols

HOSTILE = ("@x.rsp", "-o=evil")


class _Stop(BaseException):
    """Raised by the fake run once the command is recorded."""


class ToolOperandTest(unittest.TestCase):
    def capture(self, module, call):
        """The argv each hostile name produces when call runs it through module.run."""
        commands = []

        def fake_run(command, *args, **kwargs):
            commands.append([str(item) for item in command])
            raise _Stop()

        for name in HOSTILE:
            with self.subTest(name=name), mock.patch.object(module, "run", fake_run):
                commands.clear()
                with self.assertRaises(_Stop):
                    call(name)
                self.assertEqual(len(commands), 1)
                operands = commands[0][1:]
                self.assertFalse([item for item in operands if item.startswith("@")], operands)
                self.assertNotIn(name, operands)
                self.assertIn(os.path.abspath(name), operands)

    def test_readobj_imports(self):
        self.capture(
            uprof_context,
            lambda name: uprof_context.load_imports(name, None, readobj="llvm-readobj"),
        )

    def test_objdump_decode(self):
        build = types.SimpleNamespace(image_base=0x140000000)
        anchor = types.SimpleNamespace(rva=0x1000)
        self.capture(
            uprof_context,
            lambda name: uprof_context.decode_stream(
                name, build, anchor, 0x1010, 0x2000, objdump="llvm-objdump"
            ),
        )

    def test_readobj_function_ranges(self):
        self.capture(
            uprof_functions,
            lambda name: uprof_functions.derive_module_ranges("m", name, readobj="llvm-readobj"),
        )

    def test_readobj_build_identity(self):
        self.capture(
            uprof_symbols,
            lambda name: uprof_symbols.image_build_mismatch(name, 1, 1, Path("llvm-readobj")),
        )

    def test_readobj_debug_directory(self):
        self.capture(
            uprof_symbols, lambda name: uprof_symbols._readobj_info(name, Path("llvm-readobj"))
        )

    def test_pdbutil_signature(self):
        self.capture(
            uprof_symbols,
            lambda name: uprof_symbols._pdb_signature(Path("llvm-pdbutil"), Path(name)),
        )


if __name__ == "__main__":
    unittest.main()
