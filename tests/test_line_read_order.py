"""The --by line READ text follows the line view's own order for every capture mode.

Issue #92: READ said "sample_share is the busy core-cycle share, so the sample order ranks where
cycles were spent" for every dump capture's line view, so an op-count or fetch share read as
cycles and the order it named was not the table's. The line view now carries the capture's
primary measure, and READ must name that order and never claim cycles for a dump.

Run from the repository root: python tests/run_tests.py test_line_read_order
"""

import argparse
import unittest
from pathlib import Path

import uprof_dump
import uprof_query
import uprof_session

CAVEAT = "a raw share cannot separate a cheap operation run often from an expensive one run rarely"
# time_share is time only at process, thread or phase scale (#278), so READ names it as the
# order, not as where time went.
SCALE = "time only at process, thread or phase scale"
CYCLE_READ = (
    "sample_share is the busy core-cycle share, so the sample order ranks where cycles were spent"
)
TAIL = "; the <other ...> rows are last, not ranked"


def _read(sampler, dump):
    args = argparse.Namespace(
        by="line",
        top=20,
        session=Path("."),
        process=None,
        pid=None,
        image=None,
        function=None,
        module=None,
        time_filter=None,
    )
    legend = uprof_query._legend(sampler, dump, False, False, args, False)
    return legend, uprof_query._read_text(legend, False, sampler=sampler)


class LineReadOrderTest(unittest.TestCase):
    def test_dump_line_read_names_the_primary_order(self):
        dump = uprof_dump.DumpInfo(path=Path("dump.csv"), source="beside")
        cases = {
            "op-count": (
                uprof_session.Sampler(ibs_dump_kind="ibsop-event-dump", interval_units="ops"),
                "sample_share",
                f"sample_share orders the rows; {CAVEAT}",
            ),
            "cycle": (
                uprof_session.Sampler(ibs_dump_kind="ibsop-event-dump", interval_units="cycles"),
                "sample_share",
                "sample_share orders the rows",
            ),
            "fetch": (
                uprof_session.Sampler(ibs_dump_kind="ibsfetch-event-dump", interval_units="fetch"),
                "time_share",
                f"{SCALE}; sample_share is not a per-op cost; {CAVEAT}",
            ),
        }
        for mode, (sampler, order, expected) in cases.items():
            with self.subTest(mode=mode):
                legend, read = _read(sampler, dump)
                self.assertEqual(legend["impact"]["order_by"][0], order)
                # READ names the line view's own primary order first.
                self.assertTrue(read.startswith(f"{order} orders the rows"), read)
                self.assertTrue(read.endswith(expected + TAIL), read)
                self.assertNotIn("cycles were spent", read)
                self.assertNotIn("where time went", read)

    def test_timer_line_read_names_the_sample_order(self):
        sampler = uprof_session.Sampler(pmc=True, timer=True, interval_units="ms", interval_ms=1.0)
        legend, read = _read(sampler, None)
        self.assertEqual(legend["impact"]["order_by"][0], "sample_share")
        self.assertTrue(read.startswith("sample_share orders the rows"), read)
        self.assertTrue(read.endswith(TAIL), read)

    def test_pmc_cycle_line_read_keeps_the_cycle_order(self):
        sampler = uprof_session.Sampler(pmc=True, pmc_cycle_shares=True)
        legend, read = _read(sampler, None)
        self.assertEqual(legend["impact"]["order_by"], ["sample_share", "samples"])
        self.assertEqual(read, CYCLE_READ + TAIL)


if __name__ == "__main__":
    unittest.main()
