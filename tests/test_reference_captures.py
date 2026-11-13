"""The committed defect_lab reference captures hold the full set and nothing that names a host.

trials/defect_lab/captures/<platform>/ holds one capture per defect class and mode, recorded on
the owner's reference machine and anonymized by trials/anonymize_capture.py, plus images/ with
the lab images and PDBs they were recorded with. The repository is public, so every string in
every file is checked: the database tables, session.uprof, the IBS dumps, the notes and the
strings of the images and PDBs.

Allowed paths: c:\\capture\\ (the anonymizer's rewrite), c:\\windows\\ (system modules and
drivers), and LLVM's install folder, which the compiler and linker record in every PDB.

Run from the repository root: python tests/run_tests.py test_reference_captures
"""

import json
import re
import sys
import unittest
from pathlib import Path

import debug_fixtures as F

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "trials"))
import anonymize_capture  # noqa: E402

CAPTURES = REPO / "trials" / "defect_lab" / "captures"
CLASSES = ("memory", "lock", "branch", "os", "vendor", "wait", "chain", "efficient")
MODES = ("timer", "timer-cg", "ibs", "ibs-cg")
IMAGES = ("defect_lab.exe", "lab_core.dll", "lab_vendor.dll")
DRIVE = re.compile(r"(?<![A-Za-z])[A-Za-z]:(?:\\+|/)")
ALLOWED_DRIVE = re.compile(r"c:\\(capture|windows)\\")
URL = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)
# The clang version string in every PDB names the LLVM source repository.
ALLOWED_URL = "https://github.com/llvm/llvm-project"
SERVER = re.compile(r"--symbol-server(?:=|\s+)(?!<VALUE>)\S+|srv\*", re.IGNORECASE)


def platforms() -> list[Path]:
    return sorted(path for path in CAPTURES.iterdir() if path.is_dir()) if CAPTURES.is_dir() else []


def table_strings(path: Path) -> set[str]:
    import duckdb

    connection = duckdb.connect(str(path), read_only=True)
    try:
        strings = set()
        for (table,) in connection.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall():
            for row in connection.execute(f'SELECT * FROM "{table}"').fetchall():
                strings.update(value for value in row if isinstance(value, str))
        return strings
    finally:
        connection.close()


def file_strings(path: Path) -> set[str]:
    if path.suffix == ".db":
        return table_strings(path)
    if path.suffix == ".uprof":
        return set(anonymize_capture.walk_strings(json.loads(path.read_text(encoding="utf-8-sig"))))
    if path.suffix.lower() in (".exe", ".dll", ".pdb"):
        return set(anonymize_capture.file_strings(path.read_bytes()))
    return set(path.read_text(encoding="utf-8").splitlines())


def host_strings(path: Path, strings: set[str]) -> list[str]:
    """The strings of one file that could name a host, a user or a symbol server."""
    found = []
    binary = path.suffix.lower() in (".exe", ".dll", ".pdb")
    if binary:
        found += anonymize_capture.host_paths(sorted(strings))
    for string in sorted(strings):
        if anonymize_capture.PROFILE_PATH.search(string) or EMAIL.search(string):
            found.append(string)
        if SERVER.search(string):
            found.append(string)
        found += [url for url in URL.findall(string) if url != ALLOWED_URL]
        if not binary:
            for match in DRIVE.finditer(string):
                head = string[match.start() : match.start() + 12].lower().replace("/", "\\")
                if not ALLOWED_DRIVE.match(head):
                    found.append(string)
                    break
    return found


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class ReferenceCaptureTest(unittest.TestCase):
    def test_every_class_and_mode_has_a_capture_with_its_images(self):
        self.assertTrue(platforms(), f"no platform folder under {CAPTURES}")
        for platform in platforms():
            with self.subTest(platform=platform.name):
                for name in CLASSES:
                    for mode in MODES:
                        folder = platform / f"{name}.{mode}"
                        for part in ("cpu.db", "session.uprof"):
                            self.assertTrue((folder / part).is_file(), f"{folder.name}/{part}")
                        if mode.startswith("ibs"):
                            self.assertTrue((folder / "IbsOpDump.csv").is_file(), folder.name)
                for image in IMAGES:
                    for name in (image, f"{Path(image).stem}.pdb"):
                        self.assertTrue((platform / "images" / name).is_file(), name)

    def test_no_file_names_a_host_a_user_or_a_symbol_server(self):
        self.assertTrue(platforms(), f"no platform folder under {CAPTURES}")
        for platform in platforms():
            for path in sorted(p for p in platform.rglob("*") if p.is_file()):
                with self.subTest(file=str(path.relative_to(platform))):
                    self.assertEqual(host_strings(path, file_strings(path)), [])

    def test_host_info_is_empty_and_thread_names_are_blank(self):
        import duckdb

        self.assertTrue(platforms(), f"no platform folder under {CAPTURES}")
        for platform in platforms():
            for path in sorted(platform.glob("*/cpu.db")):
                with self.subTest(capture=path.parent.name):
                    connection = duckdb.connect(str(path), read_only=True)
                    try:
                        tables = {
                            t
                            for (t,) in connection.execute(
                                "SELECT table_name FROM information_schema.tables"
                            ).fetchall()
                        }
                        if "HostInfo" in tables:
                            count = connection.execute("SELECT count(*) FROM HostInfo")
                            self.assertEqual(count.fetchone()[0], 0)
                        columns = {
                            c
                            for (c,) in connection.execute(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_name = 'ProcessThread'"
                            ).fetchall()
                        }
                        if "threadName" in columns:
                            named = connection.execute(
                                "SELECT count(*) FROM ProcessThread "
                                "WHERE coalesce(threadName, '') <> ''"
                            )
                            self.assertEqual(named.fetchone()[0], 0)
                    finally:
                        connection.close()


if __name__ == "__main__":
    unittest.main()
