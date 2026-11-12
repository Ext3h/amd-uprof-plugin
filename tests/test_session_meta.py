"""Malformed session.uprof shapes must raise UprofError instead of crashing with a traceback.

Run from repository root: python tests/run_tests.py test_session_meta
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof
import uprof_session
import uprof_util


class SessionMetaShapeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="uprof-meta-test-")
        self.addCleanup(self._tmp.cleanup)
        self.session = Path(self._tmp.name) / "session"
        self.session.mkdir()

    def _write_meta(self, content):
        path = self.session / "session.uprof"
        if isinstance(content, (dict, list)):
            path.write_text(json.dumps(content), encoding="utf-8")
        else:
            path.write_text(str(content), encoding="utf-8")

    def test_top_level_array_raises_uprof_error(self):
        self._write_meta([{"collection": {"target": {"systemWide": True}}}])
        with self.assertRaises(uprof_util.UprofError) as cm:
            uprof_session.read_meta(self.session)
        self.assertIn("not a JSON object", str(cm.exception))

    def test_top_level_primitive_raises_uprof_error(self):
        self._write_meta("42")
        with self.assertRaises(uprof_util.UprofError) as cm:
            uprof_session.read_meta(self.session)
        self.assertIn("not a JSON object", str(cm.exception))

    def test_events_null_derives_sampler_without_traceback(self):
        self._write_meta({
            "collection": {"events": None, "target": {"systemWide": "true"}}
        })
        sampler = uprof_session.derive_sampler(self.session)
        self.assertIn("no PMC or IBS event recorded", sampler.metric)

    def test_events_null_verify_collect_raises_uprof_error(self):
        self._write_meta({
            "collection": {"events": None, "target": {"systemWide": "true"}}
        })
        with self.assertRaises(uprof_util.UprofError) as cm:
            uprof_session.verify_collect(self.session, "cycles", 250000, "cycles", True, [])
        self.assertIn("no PMC event recorded", str(cm.exception))

    def test_events_null_collect_summary_succeeds(self):
        meta = {"collection": {"events": None, "target": {"systemWide": "true"}}}
        summary_pmc = uprof_session.collect_summary(meta, "cycles")
        self.assertIn("hwId=None", summary_pmc)
        summary_ibs = uprof_session.collect_summary(meta, "ibsop")
        self.assertIn("ibsOp=None", summary_ibs)

    def test_non_numeric_encoded_id_raises_uprof_error(self):
        self._write_meta({
            "collection": {
                "events": {
                    "pmcEvents": {
                        "pmcEventList": [{"hwId": 118, "interval": 250000, "isSampling": True,
                                          "encodedEventId": 0x03000076}]
                    }
                }
            },
            "auxiliary": {
                "eventColumnIds": [{
                    "events": [{"name": "CYCLES_NOT_IN_HALT", "encodedId": "not_numeric"}]
                }]
            }
        })
        with self.assertRaises(uprof_util.UprofError) as cm:
            uprof_session.derive_sampler(self.session)
        self.assertIn("encodedId", str(cm.exception))

    def test_non_numeric_encoded_event_id_raises_uprof_error(self):
        self._write_meta({
            "collection": {
                "events": {
                    "pmcEvents": {
                        "pmcEventList": [{"hwId": 118, "interval": 250000, "isSampling": True,
                                          "encodedEventId": "xyz"}]
                    }
                }
            }
        })
        with self.assertRaises(uprof_util.UprofError) as cm:
            uprof_session.derive_sampler(self.session)
        self.assertIn("encodedEventId", str(cm.exception))

    def test_non_numeric_process_id_raises_uprof_error(self):
        self._write_meta({
            "collection": {
                "events": {
                    "pmcEvents": {
                        "pmcEventList": [{"hwId": 118, "interval": 250000, "isSampling": True}]
                    }
                },
                "target": {
                    "systemWide": "false",
                    "attachedProcesses": [{"processId": "not-a-pid"}]
                }
            }
        })
        with self.assertRaises(uprof_util.UprofError) as cm:
            uprof_session.verify_collect(self.session, "cycles", 250000, "cycles", False, [100])
        self.assertIn("processId", str(cm.exception))

    def test_non_numeric_anchor_start_raises_uprof_error(self):
        self._write_meta({
            "collection": {
                "schedule": {"start": "invalid_start_time"}
            }
        })
        with self.assertRaises(uprof_util.UprofError) as cm:
            uprof_session.anchor_report(self.session, qpc=1000, freq=10_000_000)
        self.assertIn("schedule.start", str(cm.exception))

    def test_cli_main_handles_malformed_meta_without_traceback(self):
        self._write_meta([1, 2, 3])
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = uprof.main(["anchor", "--session", str(self.session), "--qpc", "1000"])
        self.assertEqual(code, 2)
        self.assertIn("error:", stderr.getvalue())
        self.assertIn("not a JSON object", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
