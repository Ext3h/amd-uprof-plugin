"""Symbol names must come only from the build the capture recorded.

The capture records each module's PE CheckSum and SizeOfImage. An image on disk that disagrees
is a different build: its names would be attached to the captured build's addresses.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
from run_tests import HOST_SKIP

SKIP = F.tool_skip() or F.duckdb_skip() or F.pdb_option_skip()
NT_ONLY = HOST_SKIP + "PE symbolization of the production path runs only on Windows"
IMAGE = F.PE / "fixture_full.exe"


def _pe_identity(path: Path) -> tuple[int, int]:
    """The PE CheckSum and SizeOfImage from the optional header."""
    data = path.read_bytes()
    pe = int.from_bytes(data[0x3C:0x40], "little")
    optional = pe + 24
    size_of_image = int.from_bytes(data[optional + 56:optional + 60], "little")
    checksum = int.from_bytes(data[optional + 64:optional + 68], "little")
    return checksum, size_of_image


def _names(document: dict) -> set[str]:
    rows = document.get("rows") or document.get("data") or []
    names = set()
    for row in rows:
        for key in ("function", "function_name", "name"):
            value = row.get(key)
            if isinstance(value, str):
                names.add(value)
    return names


@unittest.skipIf(SKIP, SKIP or "")
@unittest.skipUnless(os.name == "nt", NT_ONLY)
class SymbolizedImageIdentityTest(unittest.TestCase):
    def profile(self, checksum, size):
        import duckdb

        with tempfile.TemporaryDirectory() as root:
            session = F.write_session(root, IMAGE, [F.PE_HELPER, F.PE_ENTRY])
            connection = duckdb.connect(str(session / "cpu.db"))
            try:
                connection.execute("ALTER TABLE Module ADD COLUMN checksum BIGINT")
                connection.execute("ALTER TABLE Module ADD COLUMN size UBIGINT")
                connection.execute("UPDATE Module SET checksum = ?, size = ?", [checksum, size])
            finally:
                connection.close()
            return F.profile_with_stderr(session, "line")

    def test_the_captured_build_is_symbolized(self):
        checksum, size = _pe_identity(IMAGE)
        document, _ = self.profile(checksum, size)
        self.assertIn("fixture_entry", _names(document))

    def assert_wrong_build(self, document, stderr):
        self.assertNotIn("fixture_entry", _names(document))
        self.assertNotIn("fixture_helper", _names(document))
        lines = [line for line in stderr.splitlines() if "different build" in line]
        self.assertEqual(len(lines), 1, stderr)
        self.assertIn(IMAGE.name, lines[0])
        # The cause names runnable alternatives.
        self.assertIn("--image-path", lines[0])
        self.assertRegex(lines[0], re.compile(r"--by module", re.IGNORECASE))

    def test_a_different_checksum_gives_no_names(self):
        checksum, size = _pe_identity(IMAGE)
        self.assert_wrong_build(*self.profile(checksum + 1, size))

    def test_a_different_size_of_image_without_a_checksum_gives_no_names(self):
        _, size = _pe_identity(IMAGE)
        self.assert_wrong_build(*self.profile(0, size + 0x1000))

    def test_missing_identity_evidence_keeps_the_names(self):
        document, _ = self.profile(0, 0)
        self.assertIn("fixture_entry", _names(document))


if __name__ == "__main__":
    unittest.main()
