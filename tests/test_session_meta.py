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

PMC_EVENT = {"hwId": 118, "interval": 250000, "isSampling": True}


class SessionMetaShapeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="uprof-meta-test-")
        self.addCleanup(self._tmp.cleanup)
        self.session = Path(self._tmp.name) / "session"
        self.session.mkdir()

    def _write_meta(self, content):
        path = self.session / "session.uprof"
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text, encoding="utf-8")

    def _assert_raises(self, text, call, *args, **kwargs):
        with self.assertRaises(uprof_util.UprofError) as cm:
            call(*args, **kwargs)
        self.assertIn(text, str(cm.exception))

    def test_top_level_array_raises_uprof_error(self):
        self._write_meta([{"collection": {"target": {"systemWide": True}}}])
        self._assert_raises("not a JSON object", uprof_session.read_meta, self.session)

    def test_top_level_primitive_raises_uprof_error(self):
        self._write_meta("42")
        self._assert_raises("not a JSON object", uprof_session.read_meta, self.session)

    def test_corrupt_file_reads_as_empty_object(self):
        self._write_meta("{not json")
        self.assertEqual(uprof_session.read_meta(self.session), {})

    def test_events_null_derives_sampler_without_traceback(self):
        self._write_meta({"collection": {"events": None, "target": {"systemWide": "true"}}})
        sampler = uprof_session.derive_sampler(self.session)
        self.assertIn("no PMC or IBS event recorded", sampler.metric)

    def test_events_null_verify_collect_raises_uprof_error(self):
        self._write_meta({"collection": {"events": None, "target": {"systemWide": "true"}}})
        self._assert_raises(
            "no PMC event recorded",
            uprof_session.verify_collect,
            self.session,
            "cycles",
            250000,
            "cycles",
            True,
            [],
        )

    def test_events_null_collect_summary_succeeds(self):
        meta = {"collection": {"events": None, "target": {"systemWide": "true"}}}
        self.assertIn("hwId=None", uprof_session.collect_summary(meta, "cycles"))
        self.assertIn("ibsOp=None", uprof_session.collect_summary(meta, "ibsop"))

    def test_events_not_an_object_raises_uprof_error(self):
        self._write_meta({"collection": {"events": 5}})
        self._assert_raises("collection.events", uprof_session.derive_sampler, self.session)

    def test_system_wide_with_target_not_an_object_raises_uprof_error(self):
        self._write_meta({"collection": {"target": "yes"}})
        self._assert_raises("collection.target", uprof_session.system_wide, self.session)

    def test_non_numeric_encoded_id_raises_uprof_error(self):
        self._write_meta(
            {
                "collection": {
                    "events": {
                        "pmcEvents": {"pmcEventList": [dict(PMC_EVENT, encodedEventId=0x03000076)]}
                    }
                },
                "auxiliary": {
                    "eventColumnIds": [
                        {"events": [{"name": "CYCLES_NOT_IN_HALT", "encodedId": "not_numeric"}]}
                    ]
                },
            }
        )
        self._assert_raises("encodedId", uprof_session.derive_sampler, self.session)

    def test_non_numeric_encoded_event_id_raises_uprof_error(self):
        self._write_meta(
            {
                "collection": {
                    "events": {
                        "pmcEvents": {"pmcEventList": [dict(PMC_EVENT, encodedEventId="xyz")]}
                    }
                }
            }
        )
        self._assert_raises("encodedEventId", uprof_session.derive_sampler, self.session)

    def test_non_numeric_timer_encoded_id_raises_uprof_error(self):
        meta = {
            "collection": {"events": {"timerInterval": 1}},
            "auxiliary": {
                "eventColumnIds": [
                    {
                        "events": [
                            {
                                "name": uprof_session.TIMER_EVENT_NAME,
                                "hwEventId": uprof_session.TIMER_HW_EVENT_ID,
                                "encodedId": 1.5,
                            }
                        ]
                    }
                ]
            },
        }
        self._assert_raises("encodedId", uprof_session._timer_sampler, meta)

    def test_non_numeric_process_id_raises_uprof_error(self):
        self._write_meta(
            {
                "collection": {
                    "events": {"pmcEvents": {"pmcEventList": [PMC_EVENT]}},
                    "target": {"systemWide": "false", "attachedProcesses": [{"processId": "x"}]},
                }
            }
        )
        self._assert_raises(
            "processId",
            uprof_session.verify_collect,
            self.session,
            "cycles",
            250000,
            "cycles",
            False,
            [100],
        )

    def test_attached_processes_not_a_list_raises_uprof_error(self):
        self._write_meta(
            {
                "collection": {
                    "events": {"pmcEvents": {"pmcEventList": [PMC_EVENT]}},
                    "target": {"systemWide": "false", "attachedProcesses": {"processId": 100}},
                }
            }
        )
        self._assert_raises(
            "attachedProcesses",
            uprof_session.verify_collect,
            self.session,
            "cycles",
            250000,
            "cycles",
            False,
            [100],
        )

    def test_non_numeric_anchor_start_raises_uprof_error(self):
        self._write_meta({"collection": {"schedule": {"start": "invalid_start_time"}}})
        self._assert_raises(
            "schedule.start", uprof_session.anchor_report, self.session, qpc=1000, freq=10_000_000
        )

    def test_collect_command_not_text_raises_uprof_error(self):
        meta = {"collectCommand": ["-e", "event=timer"]}
        self._assert_raises("collectCommand", uprof_session._event_from_command, meta)

    def test_collect_command_text_names_the_event(self):
        meta = {"collectCommand": "AMDuProfCLI collect -e event=cycles,interval=1 x.exe"}
        self.assertEqual(uprof_session._event_from_command(meta), "cycles")
        self.assertIsNone(uprof_session._event_from_command({}))

    def test_meta_int_reads_decimal_and_explicit_hex(self):
        meta_int = uprof_session.meta_int
        self.assertEqual(meta_int(42, "x"), 42)
        self.assertEqual(meta_int("42", "x"), 42)
        self.assertEqual(meta_int("010", "x"), 10)
        self.assertEqual(meta_int("0x10", "x"), 16)
        self.assertEqual(meta_int("0X1f", "x"), 31)

    def test_meta_int_rejects_floats_booleans_and_other_text(self):
        for value in (True, False, 1.0, "1.5", "1e3", "", "0x", "-0x1", None, [1]):
            with self.subTest(value=value):
                self._assert_raises("non-integer x", uprof_session.meta_int, value, "x")

    def test_error_message_truncates_long_values(self):
        with self.assertRaises(uprof_util.UprofError) as cm:
            uprof_session.meta_int("y" * 500, "x")
        self.assertLess(len(str(cm.exception)), 200)

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
