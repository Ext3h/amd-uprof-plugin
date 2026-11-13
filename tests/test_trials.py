"""The trial catalogue and its capture builder stay consistent with the current tools.

trials/scenarios.md lists, per scenario, the session it runs on and the minimal uprof.py calls;
every call must parse with the current CLI and every session must be one the builder writes.
The builder's captures must be the build their images are: the PE CheckSum and SizeOfImage it
records pass the same identity checks the tools apply before symbolizing or disassembling.

Run from the repository root: python tests/run_tests.py test_trials
"""

import contextlib
import io
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
import uprof
import uprof_context
import uprof_symbols

REPO = Path(__file__).resolve().parents[1]
TRIALS = REPO / "trials"
sys.path.insert(0, str(TRIALS))
import build_captures  # noqa: E402

CALL = re.compile(r"^\s+\d+\. `python <SKILL_DIR>/tools/uprof\.py (.+)`$")
FIELD = re.compile(r"^- \*\*(\w[\w ]*):\*\*\s*(.*)$")
PART = re.compile(r"^\s+- \*\*(\w+):\*\*\s*(.*)$")
ANSWER_PARTS = ("Location", "Proof", "Cost", "Confidence", "Saving")
NONE_WITH_REASON = re.compile(r"^none \(\S.*\)$")
# Concrete stand-ins for the catalogue's placeholders, so a call parses as a user would run it.
PLACEHOLDERS = {"<CAPTURE>": "capture", "<BEFORE>": "before", "<AFTER>": "after",
                "<SESSION>": "session", "<OUT_DIR>": "out", "<PDB_DIR>": "pdbs",
                "<IMAGE_DIR>": "bins", "<OWN_MODULE>": "c:/bins/app/fixture_full.exe",
                "<FUNCTION>": "fixture_entry", "<PID>": "3112", "<QPC1>": "1000",
                "<QPC2>": "2000", "<T1>": "0.5", "<T2>": "1.5"}
REQUIRED = ("Session", "Question", "Answer", "Minimal calls")


def scenarios() -> dict[str, dict]:
    text = (TRIALS / "scenarios.md").read_text(encoding="utf-8")
    result: dict[str, dict] = {}
    for block in text.split("\n## ")[1:]:
        title, _, body = block.partition("\n")
        match = re.match(r"(SCN-\d\d): ", title)
        if not match:
            continue
        fields: dict[str, str] = {}
        parts: dict[str, str] = {}
        calls: list[str] = []
        current = None
        for line in body.splitlines():
            if field := FIELD.match(line):
                current = field.group(1)
                fields[current] = field.group(2)
            elif current == "Answer" and (part := PART.match(line)):
                parts[part.group(1)] = part.group(2)
            elif call := CALL.match(line):
                calls.append(call.group(1))
        result[match.group(1)] = {"fields": fields, "parts": parts, "calls": calls}
    return result


def retired_ids() -> set[str]:
    text = (TRIALS / "scenarios.md").read_text(encoding="utf-8")
    match = re.search(r"Retired IDs: ([^.\n]*)\.", text)
    if not match or match.group(1).strip() == "none":
        return set()
    return {item.strip() for item in match.group(1).split(",")}


def argv(call: str) -> list[str]:
    for placeholder, value in PLACEHOLDERS.items():
        call = call.replace(placeholder, value)
    return shlex.split(call)


class CatalogueTest(unittest.TestCase):
    def setUp(self):
        self.scenarios = scenarios()

    def test_ids_are_unique_and_ordered(self):
        ids = list(self.scenarios)
        self.assertGreater(len(ids), 0)
        self.assertEqual(ids, sorted(set(ids)))

    def test_every_id_is_in_use_or_retired_never_both(self):
        # A retired ID is never reused, so every number up to the highest is accounted for.
        ids, retired = set(self.scenarios), retired_ids()
        self.assertFalse(ids & retired, "a retired ID was reused")
        highest = max(int(scn[4:]) for scn in ids | retired)
        self.assertEqual(ids | retired, {f"SCN-{n:02d}" for n in range(1, highest + 1)})

    def test_every_scenario_has_its_fields(self):
        for scn, entry in self.scenarios.items():
            with self.subTest(scenario=scn):
                for name in REQUIRED:
                    self.assertIn(name, entry["fields"], f"{scn} lacks {name}")
                for name in ("Session", "Question"):
                    self.assertTrue(entry["fields"][name], f"{scn}: {name} is empty")
                self.assertTrue(entry["fields"]["Answer"] or entry["parts"],
                                f"{scn}: Answer is empty")

    def test_answers_on_a_session_have_every_contract_part(self):
        for scn, entry in self.scenarios.items():
            fields = entry["fields"]
            if fields["Session"].startswith("none") or "examples/" in fields["Answer"]:
                continue
            with self.subTest(scenario=scn):
                for part in ANSWER_PARTS:
                    self.assertTrue(entry["parts"].get(part), f"{scn}: Answer lacks {part}")

    def test_none_always_names_its_reason(self):
        for scn, entry in self.scenarios.items():
            for name in ("Session", "Minimal calls"):
                value = entry["fields"][name]
                with self.subTest(scenario=scn, field=name):
                    if value.startswith("none"):
                        self.assertRegex(value, NONE_WITH_REASON)

    def test_sessions_are_ones_the_builder_writes(self):
        for scn, entry in self.scenarios.items():
            session = entry["fields"]["Session"]
            with self.subTest(scenario=scn):
                if session.startswith("none ("):
                    continue
                names = re.findall(r"`([^`]+)`", session)
                self.assertTrue(names, f"{scn}: Session names no builder session")
                self.assertLessEqual(set(names), set(build_captures.SESSIONS))
                question = entry["fields"]["Question"]
                self.assertTrue("<CAPTURE>" in question or "<BEFORE>" in question,
                                f"{scn}: the question does not name the capture")

    def test_minimal_calls_are_listed_or_declined(self):
        for scn, entry in self.scenarios.items():
            with self.subTest(scenario=scn):
                declined = entry["fields"]["Minimal calls"].startswith("none (")
                self.assertNotEqual(declined, bool(entry["calls"]),
                                    f"{scn}: list calls or say none (reason)")

    def test_pass_criteria_cap_the_final_turn_context(self):
        # A run that reaches the context ceiling is defective: the ceiling counts the model's
        # input at its final turn, harness system prompt included. A harness without usage
        # reports records it unmeasured.
        text = (TRIALS / "scenarios.md").read_text(encoding="utf-8")
        section = text.partition("\n## Pass criteria\n")[2].partition("\n## ")[0]
        self.assertTrue(section, "the catalogue has no Pass criteria section")
        self.assertIn("fewer than 50,000 tokens", section)
        self.assertIn("system prompt", section)
        self.assertIn("not measured", section)

    def test_every_call_parses_with_the_current_cli(self):
        parser = uprof.build_parser()
        for scn, entry in self.scenarios.items():
            for call in entry["calls"]:
                with self.subTest(scenario=scn, call=call):
                    stderr = io.StringIO()
                    try:
                        with contextlib.redirect_stderr(stderr):
                            parser.parse_args(argv(call))
                    except SystemExit:
                        self.fail(f"{scn}: {call}: {stderr.getvalue().strip()}")


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class BuilderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import duckdb

        cls.directory = tempfile.TemporaryDirectory(prefix="uprof-trials-")
        cls.root = Path(cls.directory.name) / "captures"
        cls.sessions = build_captures.build(cls.root)
        cls.modules = {}
        for name, session in cls.sessions.items():
            connection = duckdb.connect(str(session / "cpu.db"), read_only=True)
            try:
                cls.modules[name] = connection.execute(
                    "SELECT path, checksum, size FROM Module").fetchall()
            finally:
                connection.close()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def images(self):
        """(session, recorded path, image on disk, checksum, size) for every image built."""
        timer_image = self.root / "bins" / "timer" / "heap_churn.exe"
        for name, rows in self.modules.items():
            for path, checksum, size in rows:
                image = Path(path)
                if name == "timer" and image.name == timer_image.name:
                    image = timer_image
                elif self.root not in image.parents or "absent" in image.parts:
                    continue
                yield name, path, image, checksum, size

    def test_every_session_is_written(self):
        self.assertEqual(set(self.sessions), set(build_captures.SESSIONS))
        for session in self.sessions.values():
            self.assertTrue((session / "session.uprof").is_file())
            self.assertTrue((session / "cpu.db").is_file())

    def test_recorded_images_exist(self):
        found = list(self.images())
        self.assertGreater(len(found), 0)
        for name, path, image, _checksum, _size in found:
            with self.subTest(session=name, module=path):
                self.assertTrue(image.is_file())

    def test_recorded_identity_is_read_from_the_image(self):
        for name, path, image, checksum, size in self.images():
            with self.subTest(session=name, module=path):
                self.assertEqual((checksum, size), build_captures.pe_identity(image))
                self.assertNotEqual(checksum, 0, "a zero CheckSum would skip the check")

    def shares(self, session: str, by: str, key: str, *extra: str) -> dict[str, float]:
        document = F.profile(self.sessions[session], by, extra)
        return {Path(str(row[key])).name: row["sample_share"] for row in document["rows"]}

    def assert_quoted(self, scn: str, shares: dict[str, float]):
        answer = " ".join(scenarios()[scn]["parts"].values())
        for name, share in shares.items():
            with self.subTest(scenario=scn, row=name):
                self.assertIn(f"{share:.3f}", answer, f"{scn} does not quote {name}'s share")

    def test_scn02_quotes_the_module_shares_the_tools_print(self):
        self.assert_quoted("SCN-02", self.shares("attached", "module", "module"))

    def test_scn09_quotes_the_process_and_scoped_module_shares(self):
        self.assert_quoted("SCN-09", self.shares("systemwide", "process", "process"))
        self.assert_quoted("SCN-09", self.shares("systemwide", "module", "module",
                                                 "--pid", "3112"))

    @unittest.skipIf(F.READOBJ is None, "llvm-readobj is required; install LLVM")
    def test_images_pass_the_tools_build_identity_checks(self):
        for name, path, image, checksum, size in self.images():
            with self.subTest(session=name, module=path):
                self.assertIsNone(uprof_symbols.image_build_mismatch(
                    str(image), checksum, size, F.READOBJ))
                headers = uprof_context.run([str(F.READOBJ), "--file-headers", str(image)])
                build = uprof_context.parse_image(headers.stdout).build
                verified = uprof_context.verify_build(build, checksum, size)
                self.assertTrue(verified.verified)
                self.assertEqual(verified.identity, "checksum")


if __name__ == "__main__":
    unittest.main()
