"""translate refuses a time filter whose end is not after its start, as profile does.

Run from the repository root: python tests/run_tests.py test_translate_time_filter
"""
# Spec: SPEC-38

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_native
from uprof_util import UprofError


class TranslateTimeFilterOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.session = root / "session"
        self.session.mkdir()
        (self.session / "session.uprof").write_bytes(b"session")

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_empty_or_reversed_window_is_refused_before_amduprofcli_runs(self):
        for index, value in enumerate(("20:10", "5:5")):
            with self.subTest(value=value):
                run = mock.Mock(side_effect=AssertionError("AMDuProfCLI ran"))
                with (
                    mock.patch.object(
                        uprof_native, "require_uprof", return_value=Path("AMDuProfCLI")
                    ),
                    mock.patch.object(uprof_native, "run_watched", run),
                    self.assertRaisesRegex(
                        UprofError, rf"--time-filter needs T1 < T2; got {value}"
                    ),
                ):
                    out = self.session.parent / f"out{index}"
                    uprof_native.translate(self.session, out, time_filter=value)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
