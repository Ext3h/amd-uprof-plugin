"""The IBS-to-cycles fallback of an elevated collect deletes nothing.

collect runs elevated in a directory an unelevated process of the same user can change. If the
failed IBS attempt were cleaned up, swapping --out-dir or its parent for a link while AMDuProfCLI
runs would turn that cleanup into an elevated delete of the link's target.
"""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_collect

SESSION = {
    "collection": {
        "events": {
            "pmcEvents": {
                "pmcEventList": [{"hwId": 118, "interval": 4_700_000, "isSampling": True}]
            }
        },
        "target": {"systemWide": True},
    },
    "auxiliary": {},
}


def _write_session(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.uprof").write_text(json.dumps(SESSION), encoding="utf-8")


class ElevatedFallbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="uprof-fallback-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.victim = self.base / "victim"
        for name in ("important_dir", "other_dir"):
            (self.victim / name).mkdir(parents=True)

    def link_dir(self, link: Path, target: Path) -> None:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)

    def collect(self, out: Path, during_ibs):
        """An elevated IBS collect into out whose first AMDuProfCLI run calls during_ibs and
        reports IBS unavailable; the cycle run writes its session where -o points."""
        outputs = []

        def fake_run_cli(_uprof, spec, _target_args, options):
            outputs.append(Path(options.out_dir))
            if len(outputs) == 1:
                during_ibs()
                return subprocess.CompletedProcess(spec, 1, "", "ibs counters are not available")
            _write_session(Path(options.out_dir) / "AMDuProf-cycles")
            return subprocess.CompletedProcess(spec, 0, "", "")

        stdout = io.StringIO()
        options = uprof_collect.CollectOptions(out_dir=str(out), seconds=1, event="ibs-op")
        with (
            mock.patch.object(uprof_collect, "is_admin", return_value=True),
            mock.patch.object(uprof_collect, "find_uprof", return_value="AMDuProfCLI"),
            mock.patch.object(uprof_collect, "require_uprof", return_value="AMDuProfCLI"),
            mock.patch.object(uprof_collect, "ibs_unavailable_reason", return_value="test"),
            mock.patch.object(uprof_collect, "_run_cli", side_effect=fake_run_cli),
            mock.patch.object(
                uprof_collect, "_core_frequency", return_value=(4_700_000_000, "4700 MHz", "test")
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            uprof_collect.collect(options)
        self.stdout = stdout.getvalue()
        session = next(
            line.split("=", 1)[1]
            for line in self.stdout.splitlines()
            if line.startswith("SESSION=")
        )
        return outputs, Path(session)

    def assert_victim_intact(self):
        for name in ("important_dir", "other_dir"):
            self.assertTrue((self.victim / name).is_dir(), name)

    def test_an_out_dir_swapped_for_a_link_loses_nothing_behind_the_link(self):
        out = self.base / "out"
        (out / "earlier").mkdir(parents=True)

        def swap():
            out.rename(self.base / "out-moved")
            self.link_dir(out, self.victim)

        self.collect(out, swap)
        self.assert_victim_intact()

    def test_a_parent_swapped_for_a_link_loses_nothing_behind_the_link(self):
        parent = self.base / "parent"
        out = parent / "important_dir"

        def swap():
            self.link_dir(parent, self.victim)

        self.collect(out, swap)
        self.assert_victim_intact()

    def test_the_failed_attempt_is_left_in_place(self):
        out = self.base / "out"
        out.mkdir()

        def partial():
            (out / "AMDuProf-ibs").mkdir()

        self.collect(out, partial)
        self.assertTrue((out / "AMDuProf-ibs").is_dir())

    def test_the_cycle_capture_goes_to_a_new_directory_and_ignores_a_decoy(self):
        out = self.base / "out"
        out.mkdir()

        def plant():
            _write_session(out / "AMDuProf-decoy")

        outputs, session = self.collect(out, plant)
        fallback = outputs[1]
        self.assertNotEqual(fallback, out)
        self.assertFalse(fallback.is_relative_to(out), fallback)
        self.assertEqual(session.parent.resolve(), fallback.resolve())
        self.assertTrue((out / "AMDuProf-decoy" / "session.uprof").is_file())

    def test_the_fallback_command_names_the_directory_it_captures_into(self):
        out = self.base / "out"
        out.mkdir()
        outputs, session = self.collect(out, lambda: None)
        commands = [line for line in self.stdout.splitlines() if line.startswith("COMMAND=")]
        self.assertEqual(len(commands), 2, commands)
        self.assertIn(str(outputs[1]), commands[1])
        self.assertEqual(session.parent.resolve(), outputs[1].resolve())


if __name__ == "__main__":
    unittest.main()
