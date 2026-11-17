"""The top-row clause says whether row 1 beats row 2 and widens its interval for the selection.

The clause describes the maximum of the ranked rows, and a maximum picked from noisy shares is
biased up: with equal true shares a plain 95% interval on it covers the truth in only 65-84% of
runs, and a row at 23.4% beside one at 22.9% reads as a finding. The clause therefore uses a
Bonferroni z over the m ranked rows and states a separation test: a 95% Wilson interval of
k1 / (k1 + k2) that excludes one half separates row 1 from row 2 (issue #277).

Run from the repository root: python tests/run_tests.py test_top_row_separation
"""

import math
import unittest
from statistics import NormalDist

import duckdb
import uprof_query


def _wilson(count, total, z):
    share = count / total
    center = (share + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(share * (1 - share) / total + z * z / (4 * total * total))
    half /= 1 + z * z / total
    return f"{100 * max(0.0, center - half):.1f}-{100 * min(1.0, center + half):.1f}"


def _clause(samples):
    """The clause for module rows with these sample counts, ranked in the given order."""
    connection = duckdb.connect()
    try:
        total = sum(samples)
        values = ", ".join(
            f"('m{index}.dll', {count}, {total}, {index})"
            for index, count in enumerate(samples, start=1)
        )
        connection.execute(
            "CREATE TEMP VIEW v_module_rows AS SELECT * FROM (VALUES "
            f"{values}, ('<other modules>', 0, {total}, NULL)) "
            "AS t(module, samples, total_samples, rn)"
        )
        return uprof_query._top_row_precision(connection, "module")[0]
    finally:
        connection.close()


class TopRowSeparationTest(unittest.TestCase):
    def test_close_rows_are_not_separated(self):
        z = NormalDist().inv_cdf(1 - 0.05 / 4)
        self.assertEqual(
            _clause([100, 98]),
            f"top m1.dll 50.5% (95% CI {_wilson(100, 198, z)}%, n=198, row2 tied)",
        )

    def test_distant_rows_are_separated(self):
        z = NormalDist().inv_cdf(1 - 0.05 / 6)
        self.assertEqual(
            _clause([100, 20, 10]),
            f"top m1.dll 76.9% (95% CI {_wilson(100, 130, z)}%, n=130, row2 separated)",
        )

    def test_a_single_row_keeps_the_plain_interval(self):
        self.assertEqual(
            _clause([40]), f"top m1.dll 100.0% (95% CI {_wilson(40, 40, 1.96)}%, n=40)"
        )


if __name__ == "__main__":
    unittest.main()
