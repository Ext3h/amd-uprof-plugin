"""The ACHIEVED cadence summary over synthetic per-context statistics.

Run from the repository root: python tests/run_tests.py test_achieved_cadence
"""
# Spec: SPEC-24

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import uprof_collect
import uprof_native
import uprof_query
import uprof_session


def cycle_sampler(**overrides):
    settings = {"interval": 4_700_000, "interval_units": "cycles"}
    settings.update(overrides)
    return uprof_session.Sampler(**settings)


class AchievedCadenceTest(unittest.TestCase):
    """Each test supplies context stats directly; no capture or database is needed."""

    def achieved(self, contexts, *, window_s, cpus=None, sampler=None, label="CPU"):
        return uprof_session.achieved_summary(
            sum(context.samples for context in contexts),
            window_s,
            len(contexts) if cpus is None else cpus,
            sampler or cycle_sampler(),
            contexts=contexts,
            context_label=label,
            frequency_hz=4_700_000_000,
        )

    def test_eight_busy_and_eight_idle_cpus_do_not_warn(self):
        # The capture average is 62.56/s per CPU, below 10% of the 1,000/s ideal, because the
        # idle CPUs share the denominator; the busiest CPU still delivers the 1 ms cadence.
        contexts = ([uprof_session.ContextCadence(1000, 1_000_000)] * 8
                    + [uprof_session.ContextCadence(1, 1_000_000_000)] * 8)
        text = self.achieved(contexts, window_s=8.0)
        self.assertNotIn("WARN", text)
        self.assertIn("62.56/s per CPU", text)
        self.assertIn("busiest CPU 1,000/s", text)
        self.assertIn("8 active CPUs", text)

    def test_all_contexts_at_one_percent_warn(self):
        contexts = [uprof_session.ContextCadence(100)] * 8
        text = self.achieved(contexts, window_s=10.0)
        self.assertIn("WARN", text)
        self.assertIn("busiest CPU 10.00/s", text)

    def test_intermittent_thread_with_correct_gaps_does_not_warn(self):
        # One thread, 500 samples over 100 s: 5/s overall, but every running gap is 1 ms.
        contexts = [uprof_session.ContextCadence(500, 1_000_000)]
        text = self.achieved(contexts, window_s=100.0, cpus=1, label="thread")
        self.assertNotIn("WARN", text)
        self.assertIn("5.00/s per CPU", text)
        self.assertIn("busiest thread 1,000/s", text)
        self.assertIn("1 active thread", text)

    def test_pmc_contexts_use_the_busiest_core_rate(self):
        # PMC carries no per-sample timestamps, so the busiest core's samples/s is the cadence.
        sampler = cycle_sampler(pmc=True, event_column="event#0x3000076")
        contexts = [uprof_session.ContextCadence(10), uprof_session.ContextCadence(2)]
        text = self.achieved(contexts, window_s=10.0, sampler=sampler, label="core")
        self.assertIn("WARN", text)
        self.assertIn("busiest core 1.00/s", text)

    def test_dump_context_without_a_running_gap_warns(self):
        sampler = cycle_sampler(ibs_dump_kind="ibsop-event-dump")
        contexts = [uprof_session.ContextCadence(5, None)]
        text = self.achieved(contexts, window_s=1.0, sampler=sampler)
        self.assertIn("WARN", text)

    def test_no_contexts_never_warns_on_the_diluted_average(self):
        text = self.achieved([], window_s=8.0, cpus=16)
        self.assertNotIn("WARN", text)


class OneFrequencyReadTest(unittest.TestCase):
    """The run-gap limit and the ACHIEVED text convert the cycle interval at one frequency.

    The Linux fallback reads the current MHz, which can change between two reads; a median gap
    filtered at one frequency and judged at another could then warn about a gap the filter kept.
    """

    def test_profile_reads_the_core_frequency_once_for_gaps_and_text(self):
        sampler = cycle_sampler(ibs_dump_kind="ibsop-event-dump")
        args = argparse.Namespace(session=Path("session"))
        reads = [(4_700_000_000, "4700 MHz", "first"), (2_200_000_000, "2200 MHz", "second")]
        limits = []

        def contexts(_connection, limit_ns):
            limits.append(limit_ns)
            return [uprof_session.ContextCadence(5, 1_000_000)]

        with mock.patch.object(uprof_collect, "_core_frequency", side_effect=reads) as read, \
                mock.patch.object(uprof_session, "read_meta", return_value={}), \
                mock.patch.object(uprof_session, "system_wide", return_value=True), \
                mock.patch.object(uprof_query, "_capture_scan", return_value=(5, 1, None)), \
                mock.patch.object(uprof_query, "_dump_contexts", side_effect=contexts):
            (_name, text), _samples, _kernel = uprof_query._achieved_field(
                None, True, args, sampler, None)

        self.assertEqual(read.call_count, 1)
        self.assertEqual(limits, [10 * 4_700_000 / 4.7e9 * 1e9])
        self.assertIn("4700 MHz", text)


class DumpCadenceTest(unittest.TestCase):
    def test_same_tid_in_different_processes_is_two_contexts(self):
        # Both rows sit on CPU 0, so only the (PID, TID) key can separate the two timelines.
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "IbsOpDump.csv"
            dump.write_text(
                "DeltaTick,CPU_Number,TID,PID,Offset,ModuleName\n"
                "0,0,4242,100,4096,workload.exe\n"
                "1000000000,0,4242,100,4096,workload.exe\n"
                "1500000000,0,4242,200,8192,other.exe\n"
                "2500000000,0,4242,200,8192,other.exe\n",
                encoding="utf-8", newline="\n")

            _, _, contexts = uprof_native.dump_cadence(dump, ("PID", "TID"))

        self.assertEqual(sorted(context.samples for context in contexts), [2, 2])


if __name__ == "__main__":
    unittest.main()
