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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F
import uprof
import uprof_context
import uprof_symbols

REPO = Path(__file__).resolve().parents[1]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"
TRIALS = REPO / "trials"
sys.path.insert(0, str(TRIALS))
import build_captures  # noqa: E402

CALL = re.compile(r"^\s+\d+\. `python <SKILL_DIR>/tools/uprof\.py (.+)`$")
FIELD = re.compile(r"^- \*\*(\w[\w ]*):\*\*\s*(.*)$")
PART = re.compile(r"^\s+- \*\*(\w+):\*\*\s*(.*)$")
ANSWER_PARTS = ("Location", "Proof", "Cost", "Confidence", "Saving")
NONE_WITH_REASON = re.compile(r"^none \(\S.*\)$")
# Concrete stand-ins for the catalogue's placeholders, so a call parses as a user would run it.
PLACEHOLDERS = {
    "<CAPTURE>": "capture",
    "<BEFORE>": "before",
    "<AFTER>": "after",
    "<SESSION>": "session",
    "<OUT_DIR>": "out",
    "<PDB_DIR>": "pdbs",
    "<IMAGE_DIR>": "bins",
    "<OWN_MODULE>": "c:/bins/app/fixture_full.exe",
    "<FUNCTION>": "fixture_entry",
    "<PID>": "3112",
    "<QPC1>": "1000",
    "<QPC2>": "2000",
    "<T1>": "0.5",
    "<T2>": "1.5",
}
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
        result[match.group(1)] = {"fields": fields, "parts": parts, "calls": calls, "body": body}
    return result


def conventions() -> str:
    text = (TRIALS / "scenarios.md").read_text(encoding="utf-8")
    return text.partition("\n## Conventions\n")[2].partition("\n## ")[0]


# Sessions with background load, where an own-code function call must be filtered first.
LOADED_SESSIONS = {"systemwide_loaded", "attached_inproc_loaded"}
# Steps (1-based) of scenarios on loaded sessions that ask for outside code on purpose, so an
# unfiltered --by function is the minimal call there.
UNFILTERED_STEPS: dict[str, set[int]] = {}
OPEN_MARK = re.compile(r"open \((D-\d{3})\)")


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
                self.assertTrue(
                    entry["fields"]["Answer"] or entry["parts"], f"{scn}: Answer is empty"
                )

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
                self.assertTrue(
                    "<CAPTURE>" in question or "<BEFORE>" in question,
                    f"{scn}: the question does not name the capture",
                )

    def test_minimal_calls_are_listed_or_declined(self):
        for scn, entry in self.scenarios.items():
            with self.subTest(scenario=scn):
                declined = entry["fields"]["Minimal calls"].startswith("none (")
                self.assertNotEqual(
                    declined, bool(entry["calls"]), f"{scn}: list calls or say none (reason)"
                )

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
        flat = " ".join(section.split())
        self.assertIn("a defect ceiling, not a target", flat)
        # The catalogue stays harness-neutral; how a harness's usage report adds up is the
        # runner's business.
        self.assertNotIn("cache-read", flat)

    def test_loaded_scenarios_run_on_their_loaded_sessions(self):
        expected = {
            "SCN-01": "systemwide_loaded",
            "SCN-09": "systemwide_loaded",
            "SCN-10": "systemwide_loaded",
            "SCN-20": "attached_inproc_loaded",
        }
        for scn, session in expected.items():
            with self.subTest(scenario=scn):
                self.assertEqual(
                    re.findall(r"`([^`]+)`", self.scenarios[scn]["fields"]["Session"]), [session]
                )

    def test_own_code_function_calls_on_loaded_sessions_are_filtered_first(self):
        # An unfiltered function view on a loaded capture shows other processes' code: a wasted
        # turn when the question is about the user's process.
        for scn, entry in self.scenarios.items():
            sessions = set(re.findall(r"`([^`]+)`", entry["fields"]["Session"]))
            if not sessions & LOADED_SESSIONS:
                continue
            for step, call in enumerate(entry["calls"], start=1):
                if "--by function" not in call or step in UNFILTERED_STEPS.get(scn, set()):
                    continue
                with self.subTest(scenario=scn, step=step):
                    self.assertRegex(call, r"--pid |--image ")

    def test_open_steps_name_a_decision_the_conventions_list(self):
        listed = set(OPEN_MARK.findall(conventions()))
        for scn, entry in self.scenarios.items():
            for decision in OPEN_MARK.findall(entry["body"]):
                with self.subTest(scenario=scn, decision=decision):
                    self.assertIn(decision, listed)

    def test_instruction_calls_ask_for_the_hot_row_only(self):
        for scn, entry in self.scenarios.items():
            for call in entry["calls"]:
                if "--by instruction" in call:
                    with self.subTest(scenario=scn, call=call):
                        self.assertIn("--top 1", call)

    def test_the_conventions_state_the_filter_first_rule(self):
        text = " ".join(conventions().split())
        self.assertIn("--pid", text)
        self.assertIn("before the function view", text)

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
                    "SELECT path, checksum, size FROM Module"
                ).fetchall()
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
        self.assert_quoted("SCN-09", self.shares("systemwide", "module", "module", "--pid", "3112"))

    @unittest.skipIf(F.READOBJ is None, "llvm-readobj is required; install LLVM")
    def test_images_pass_the_tools_build_identity_checks(self):
        for name, path, image, checksum, size in self.images():
            with self.subTest(session=name, module=path):
                self.assertIsNone(
                    uprof_symbols.image_build_mismatch(str(image), checksum, size, F.READOBJ)
                )
                headers = uprof_context.run([str(F.READOBJ), "--file-headers", str(image)])
                build = uprof_context.parse_image(headers.stdout).build
                verified = uprof_context.verify_build(build, checksum, size)
                self.assertTrue(verified.verified)
                self.assertEqual(verified.identity, "checksum")


OWN_IMAGE_NAME = "fixture_full.exe"
# What the minimal calls of one scenario may print together (stdout and stderr), in characters.
# A run must end below 50,000 tokens of context: 21,500 go to the harness and prompt, about
# 1,200 to SKILL.md after #118's cut, 300 per call, and 5,000 stay as margin; at 2.0 characters
# per token, the low end measured on tool output, that leaves about 44,000 characters.
CALL_OUTPUT_CEILING = 44_000


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
@unittest.skipIf(F.READOBJ is None, "llvm-readobj is required; install LLVM")
class ContextCeilingTest(unittest.TestCase):
    """Each scenario's minimal calls print little enough to stay below the context ceiling."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="uprof-trials-ceiling-")
        cls.root = Path(cls.directory.name) / "captures"
        cls.sessions = build_captures.build(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_call(self, call: str) -> str:
        result = subprocess.run(
            [sys.executable, str(UPROF), *shlex.split(call)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, f"{call}\n{result.stderr}")
        return result.stdout + result.stderr

    def own_module(self, session: Path) -> str:
        document = F.profile(session, "function", ("--top", "all"))
        return next(
            row["module"] for row in document["rows"] if str(row["module"]).endswith(OWN_IMAGE_NAME)
        )

    def concrete(self, call: str, names: list[str]) -> str:
        # Forward slashes: the calls are split the way a POSIX shell splits them.
        capture = self.sessions[names[0]]
        values = {
            "<CAPTURE>": capture.as_posix(),
            "<PDB_DIR>": (self.root / "bins" / "pdbs").as_posix(),
            "<IMAGE_DIR>": (self.root / "bins" / "timer").as_posix(),
            "<PID>": "3112",
            "<FUNCTION>": "fixture_entry",
            "<QPC1>": "12000000",
            "<QPC2>": "15000000",
            "<T1>": "0.2",
            "<T2>": "0.5",
        }
        if len(names) > 1:
            values.update(
                {
                    "<BEFORE>": self.sessions[names[0]].as_posix(),
                    "<AFTER>": self.sessions[names[1]].as_posix(),
                }
            )
        if "<OWN_MODULE>" in call:
            values["<OWN_MODULE>"] = self.own_module(capture)
        for placeholder, value in values.items():
            call = call.replace(placeholder, shlex.quote(value) if " " in value else value)
        return call

    def test_minimal_calls_print_less_than_the_ceiling(self):
        for scn, entry in scenarios().items():
            names = re.findall(r"`([^`]+)`", entry["fields"]["Session"])
            if entry["fields"]["Session"].startswith("none") or not entry["calls"]:
                continue
            with self.subTest(scenario=scn):
                printed = sum(
                    len(self.run_call(self.concrete(call, names))) for call in entry["calls"]
                )
                self.assertLess(printed, CALL_OUTPUT_CEILING)


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class LoadedSessionTest(unittest.TestCase):
    """Loaded sessions put the user's rows where a real system puts them: below the top rows."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="uprof-trials-loaded-")
        cls.sessions = build_captures.build(
            Path(cls.directory.name) / "captures", ["systemwide_loaded", "attached_inproc_loaded"]
        )

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def rows(self, session: str, by: str, *extra: str) -> list[dict]:
        return F.profile(self.sessions[session], by, extra)["rows"]

    def test_systemwide_loaded_hides_the_target_below_busier_processes(self):
        rows = self.rows("systemwide_loaded", "process", "--top", "all")
        processes = [row for row in rows if not str(row["process"]).startswith("<")]
        self.assertGreater(len(processes), 20)
        target = [row for row in processes if row["process"] == build_captures.PROCESS]
        self.assertEqual(
            sorted(row["pid"] for row in target), [build_captures.PID, build_captures.SECOND_PID]
        )
        own = next(row for row in target if row["pid"] == build_captures.PID)
        self.assertGreaterEqual(own["sample_share"], 0.02)
        self.assertLessEqual(own["sample_share"], 0.10)
        self.assertNotEqual(processes[0]["process"], build_captures.PROCESS)
        modules = [
            row
            for row in self.rows("systemwide_loaded", "module", "--top", "all")
            if not str(row["module"]).startswith("<")
        ]
        self.assertGreater(len(modules), 20)

    @unittest.skipIf(F.READOBJ is None, "llvm-readobj is required; install LLVM")
    def test_only_the_process_filter_brings_the_own_function_into_the_top_rows(self):
        def own(rows):
            return [row for row in rows if str(row["module"]).endswith(OWN_IMAGE_NAME)]

        self.assertEqual(own(self.rows("systemwide_loaded", "function")), [])
        filtered = self.rows("systemwide_loaded", "function", "--pid", str(build_captures.PID))
        self.assertIn("fixture_entry", [row["function"] for row in own(filtered)])

    def test_the_image_filter_merges_the_unrelated_instance_and_the_pid_does_not(self):
        def total(*extra):
            return sum(
                row["samples"]
                for row in self.rows("systemwide_loaded", "module", "--top", "all", *extra)
            )

        by_pid = total("--pid", str(build_captures.PID))
        second = total("--pid", str(build_captures.SECOND_PID))
        self.assertGreater(second, 0)
        self.assertEqual(total("--image", build_captures.PROCESS), by_pid + second)

    @unittest.skipIf(F.READOBJ is None, "llvm-readobj is required; install LLVM")
    def test_inproc_load_hides_the_own_functions_even_under_the_process_filter(self):
        processes = [
            row
            for row in self.rows("attached_inproc_loaded", "process", "--top", "all")
            if not str(row["process"]).startswith("<")
        ]
        self.assertEqual([row["pid"] for row in processes], [build_captures.PID])
        modules = [
            row
            for row in self.rows("attached_inproc_loaded", "module", "--top", "all")
            if not str(row["module"]).startswith("<")
        ]
        self.assertGreater(len(modules), 20)
        rows = self.rows("attached_inproc_loaded", "function", "--pid", str(build_captures.PID))
        named = [row for row in rows if not str(row["function"]).startswith("<other")]
        self.assertGreaterEqual(len(named), 20)
        self.assertFalse([row for row in rows if str(row["module"]).endswith(OWN_IMAGE_NAME)])
        everything = self.rows(
            "attached_inproc_loaded", "function", "--pid", str(build_captures.PID), "--top", "all"
        )
        self.assertTrue([row for row in everything if str(row["module"]).endswith(OWN_IMAGE_NAME)])


if __name__ == "__main__":
    unittest.main()
