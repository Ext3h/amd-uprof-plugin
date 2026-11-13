"""Verify hot_rva precision and skid attribution semantics across sampling modes.

Issue #287: hot_rva is called "the precise attribution" for PMC, timer and fetch samples.
Only IBS OP carries a precise instruction address; PMC and timer samples skid, and fetch
samples carry a fetch-block address.

Run from repository root: python tests/run_tests.py test_hot_rva_attribution
"""

import argparse
from pathlib import Path
import unittest

import uprof_query
import uprof_session


class HotRvaAttributionTest(unittest.TestCase):
    def setUp(self):
        self.samplers = {
            "ibsop": uprof_session.Sampler(
                ibs_dump_kind="ibsop-event-dump", interval_units="cycles"
            ),
            "ibsfetch": uprof_session.Sampler(
                ibs_dump_kind="ibsfetch-event-dump", interval_units="fetch"
            ),
            "timer": uprof_session.Sampler(timer=True, interval_ms=1.0),
            "pmc": uprof_session.Sampler(pmc=True, pmc_cycle_shares=True),
        }

    def _make_legend_and_fields(self, sampler, by):
        args = argparse.Namespace(
            by=by,
            top=20,
            session=Path("."),
            process=None,
            pid=None,
            image=None,
            sort=None,
            filter=None,
            show_synthetic=False,
            caller=None,
            time_filter=None,
            time_unit="s",
        )
        legend = uprof_query._legend(
            sampler,
            None,
            False,
            False,
            args,
            False,
            None,
            False,
            None,
            {},
            None,
            None,
        )
        fields = dict(
            uprof_query._legend_fields(
                legend, False, sampler.timer, sampler=sampler
            )
        )
        return legend, fields

    def test_columns_hot_rva_semantics_per_mode(self):
        for by, container in (
            ("function", "range"),
            ("module", "module"),
            ("region", "page"),
        ):
            with self.subTest(by=by, mode="ibsop"):
                legend, fields = self._make_legend_and_fields(
                    self.samplers["ibsop"], by
                )
                col = legend["columns"]["hot_rva"]
                self.assertIn(f"the precise attribution inside the {container}", col)
                self.assertNotIn("skid", col)
                self.assertIn(f"the precise attribution inside the {container}", fields["COLUMNS"])

            with self.subTest(by=by, mode="ibsfetch"):
                legend, fields = self._make_legend_and_fields(
                    self.samplers["ibsfetch"], by
                )
                col = legend["columns"]["hot_rva"]
                self.assertIn(
                    f"the precise attribution inside the {container} (fetch samples carry a fetch-block address)",
                    col,
                )
                self.assertIn("fetch samples carry a fetch-block address", fields["COLUMNS"])

            with self.subTest(by=by, mode="timer"):
                legend, fields = self._make_legend_and_fields(
                    self.samplers["timer"], by
                )
                col = legend["columns"]["hot_rva"]
                self.assertIn(
                    f"the precise attribution inside the {container} (timer samples skid)",
                    col,
                )
                self.assertIn("timer samples skid", fields["COLUMNS"])

            with self.subTest(by=by, mode="pmc"):
                legend, fields = self._make_legend_and_fields(
                    self.samplers["pmc"], by
                )
                col = legend["columns"]["hot_rva"]
                self.assertIn(
                    f"the precise attribution inside the {container} (PMC samples skid)",
                    col,
                )
                self.assertIn("PMC samples skid", fields["COLUMNS"])

    def test_region_impact_estimate_semantics_per_mode(self):
        with self.subTest(mode="ibsop"):
            legend, _ = self._make_legend_and_fields(self.samplers["ibsop"], "region")
            estimate = legend["impact"]["estimate"]
            self.assertIn(
                "hot_rva is the highest-sample RVA inside the page and the precise attribution",
                estimate,
            )
            self.assertNotIn("skid", estimate)

        with self.subTest(mode="ibsfetch"):
            legend, _ = self._make_legend_and_fields(self.samplers["ibsfetch"], "region")
            estimate = legend["impact"]["estimate"]
            self.assertIn(
                "hot_rva is the highest-sample RVA inside the page and the precise attribution (fetch samples carry a fetch-block address)",
                estimate,
            )

        with self.subTest(mode="timer"):
            legend, _ = self._make_legend_and_fields(self.samplers["timer"], "region")
            estimate = legend["impact"]["estimate"]
            self.assertIn(
                "hot_rva is the highest-sample RVA inside the page and the precise attribution (timer samples skid)",
                estimate,
            )

        with self.subTest(mode="pmc"):
            legend, _ = self._make_legend_and_fields(self.samplers["pmc"], "region")
            estimate = legend["impact"]["estimate"]
            self.assertIn(
                "hot_rva is the highest-sample RVA inside the page and the precise attribution (PMC samples skid)",
                estimate,
            )

    def test_region_read_semantics_per_mode(self):
        with self.subTest(mode="ibsop"):
            _, fields = self._make_legend_and_fields(self.samplers["ibsop"], "region")
            read = fields["READ"]
            self.assertIn("hot_rva is the precise attribution inside the page", read)
            self.assertNotIn("skid", read)

        with self.subTest(mode="ibsfetch"):
            _, fields = self._make_legend_and_fields(self.samplers["ibsfetch"], "region")
            read = fields["READ"]
            self.assertIn(
                "hot_rva is the precise attribution inside the page (fetch samples carry a fetch-block address)",
                read,
            )

        with self.subTest(mode="timer"):
            _, fields = self._make_legend_and_fields(self.samplers["timer"], "region")
            read = fields["READ"]
            self.assertIn(
                "hot_rva is the precise attribution inside the page (timer samples skid)",
                read,
            )

        with self.subTest(mode="pmc"):
            _, fields = self._make_legend_and_fields(self.samplers["pmc"], "region")
            read = fields["READ"]
            self.assertIn(
                "hot_rva is the precise attribution inside the page (PMC samples skid)",
                read,
            )


if __name__ == "__main__":
    unittest.main()
