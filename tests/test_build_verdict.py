"""Symbolization, range derivation and the instruction context share one build verdict.

The on-disk image is compared with the CheckSum and SizeOfImage the capture recorded. A nonzero
CheckSum pair decides; without one, a nonzero SizeOfImage pair decides; with neither the build
is unverified, not mismatched. Every consumer must reach the same verdict for the same evidence.

Run from the repository root: python tests/run_tests.py test_build_verdict
"""
# Spec: SPEC-90, SPEC-91

import itertools
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import uprof_context
import uprof_symbols

HEADERS = """\
ImageOptionalHeader {
  Magic: 0x20B
  ImageBase: 0x140000000
  SizeOfImage: {size}
  CheckSum: 0x{checksum:X}
}
ImageFileHeader {
  TimeDateStamp: 2026-11-07 17:21:58 (0x6AEF5E36)
}
"""


def context_verdict(checksum, size, capture_checksum, capture_size):
    build = uprof_context.parse_image(
        HEADERS.replace("{size}", str(size)).replace("{checksum:X}", f"{checksum:X}")
    ).build
    try:
        identity = uprof_context.verify_build(build, capture_checksum, capture_size)
    except uprof_context.ContextOmission as exc:
        return "mismatch" if exc.reason == "build_mismatch" else exc.reason
    return identity.identity


def symbols_verdict(checksum, size, capture_checksum, capture_size):
    text = HEADERS.replace("{size}", str(size)).replace("{checksum:X}", f"{checksum:X}")
    completed = subprocess.CompletedProcess([], 0, stdout=text, stderr="")
    with mock.patch.object(uprof_symbols, "run", return_value=completed):
        mismatch = uprof_symbols.image_build_mismatch(
            "image.dll", capture_checksum, capture_size, Path("llvm-readobj")
        )
    return "mismatch" if mismatch else "match_or_unverified"


class SharedBuildVerdictTest(unittest.TestCase):
    def test_every_evidence_combination_gets_one_verdict(self):
        for checksum, size, capture_checksum, capture_size in itertools.product(
            (0, 0x1234), (8192,), (None, 0, 0x1234, 0x9999), (None, 0, 8192, 8193)
        ):
            with self.subTest(
                checksum=checksum,
                size=size,
                capture_checksum=capture_checksum,
                capture_size=capture_size,
            ):
                context = context_verdict(checksum, size, capture_checksum, capture_size)
                symbols = symbols_verdict(checksum, size, capture_checksum, capture_size)
                self.assertEqual(context == "mismatch", symbols == "mismatch", context)

    def test_a_size_mismatch_without_a_checksum_omits_the_context(self):
        self.assertEqual(context_verdict(0, 8192, 0, 8193), "mismatch")
        self.assertEqual(symbols_verdict(0, 8192, 0, 8193), "mismatch")


if __name__ == "__main__":
    unittest.main()
