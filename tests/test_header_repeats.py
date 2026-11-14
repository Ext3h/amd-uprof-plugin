"""A header states each fact once: no phrase repeats across the header lines of one output.

Every profile call prints its header, so a fact stated twice is paid twice in every call (#389).
The column meaning lives in COLUMNS or METRIC, the time_share estimator in TIME_SHARE, the
tag-to-retire definition in METRIC; READ, WEIGHTS and SANITY add only what those lines do not
say. A phrase is six words after paths, placeholders and READ's top-row clause are dropped;
NEXT and VERSION carry paths and runnable calls and are not compared.

The process and module views run everywhere. The function view runs on Windows with LLVM and
skips elsewhere, with a reason that UPROF_REQUIRE_TOOLS=1 turns into a failure.

Run from the repository root: python tests/run_tests.py test_header_repeats
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import debug_fixtures as F

REPO = Path(__file__).resolve().parents[1]
UPROF = REPO / "plugin" / "skills" / "amd-uprof" / "tools" / "uprof.py"
HEADER = re.compile(r"^([A-Z][A-Z_]+): (.*)$")
SKIPPED = ("NEXT", "VERSION")
PHRASE = 6
# One synthetic capture per sampler: IBS OP cycle and op count, system-wide IBS, PMC, timer.
SESSIONS = ("attached", "opcount", "pmc", "systemwide", "timer")


def repeats(output: str) -> list[str]:
    """Phrases that occur in two places of the header, each with the fields it occurs in."""
    words: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = HEADER.match(line)
        if not match or match.group(1) in SKIPPED or match.group(2).startswith("'"):
            continue
        # READ's last clause names the top row, which other fields may name too: row data.
        value = re.sub(r"; top row .*$", "", match.group(2))
        text = re.sub(r"\S*[\\/]\S*|<[A-Z_]+>", " ", value.lower())
        words += [(match.group(1), word) for word in text.split()]
    first: dict[tuple[str, ...], tuple[int, str]] = {}
    found: dict[str, None] = {}
    for index in range(len(words) - PHRASE + 1):
        phrase = tuple(word for _field, word in words[index : index + PHRASE])
        if phrase in first and index - first[phrase][0] >= PHRASE:
            found[f"{first[phrase][1]} -> {words[index][0]}: {' '.join(phrase)}"] = None
        first.setdefault(phrase, (index, words[index][0]))
    return list(found)


@unittest.skipIf(F.duckdb_skip(), F.duckdb_skip() or "")
class HeaderRepeatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "trials"))
        import build_captures

        cls.tmp = tempfile.TemporaryDirectory()
        cls.sessions = build_captures.build(Path(cls.tmp.name) / "c", list(SESSIONS))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_profile(self, session: str, *args) -> str:
        result = subprocess.run(
            [sys.executable, str(UPROF), "profile", "--session", str(self.sessions[session])]
            + list(args),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def check(self, views):
        for session in SESSIONS:
            for view in views:
                with self.subTest(session=session, view=view):
                    self.assertEqual(repeats(self.run_profile(session, "--by", view)), [])

    def test_the_rollup_headers_state_each_fact_once(self):
        self.check(["process", "module"])

    @unittest.skipIf(os.name != "nt", "host: the symbolizing fixtures run on Windows")
    @unittest.skipIf(F.READOBJ is None, "missing llvm-readobj; install LLVM")
    def test_the_function_header_states_each_fact_once(self):
        self.check(["function"])


if __name__ == "__main__":
    unittest.main()
