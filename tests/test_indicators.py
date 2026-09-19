"""股债利差与百分位的口径测试。"""

from __future__ import annotations

import unittest
from datetime import date

from macroboard import indicators


class SpreadTests(unittest.TestCase):
    def test_pe_12_5_and_yield_2pct_gives_6_points_and_600bp(self):
        spread = indicators.equity_bond_spread(12.5, 2.0)
        self.assertIsNotNone(spread)
        self.assertAlmostEqual(spread, 0.06, places=12)
        self.assertAlmostEqual(indicators.to_percentage_points(spread), 6.0, places=9)
        self.assertAlmostEqual(indicators.to_bp(spread), 600.0, places=6)

    def test_spec_example_with_decimal_yield(self):
        # 规格书示例：PE=12.5、国债收益率=0.02（小数）
        spread = indicators.equity_bond_spread(12.5, 0.02 * 100)
        self.assertAlmostEqual(indicators.to_percentage_points(spread), 6.0, places=9)

    def test_invalid_pe_yields_none(self):
        for pe in (0, -3.2, None):
            with self.subTest(pe=pe):
                self.assertIsNone(indicators.equity_bond_spread(pe, 2.0))

    def test_missing_yield_yields_none(self):
        self.assertIsNone(indicators.equity_bond_spread(12.5, None))

    def test_build_spread_series_requires_same_date_without_forward_fill(self):
        pe = [(date(2024, 1, 2), 12.0), (date(2024, 1, 4), 13.0)]
        bond = [(date(2024, 1, 3), 2.0), (date(2024, 1, 4), 2.2)]
        series = indicators.build_spread_series(pe, bond)
        self.assertEqual([day for day, _ in series], [date(2024, 1, 4)])
        self.assertAlmostEqual(series[0][1], 1 / 13.0 - 0.022, places=12)

    def test_series_drops_invalid_pe_dates(self):
        pe = [(date(2024, 1, 2), 0.0), (date(2024, 1, 3), 10.0)]
        bond = [(date(2024, 1, 2), 2.0), (date(2024, 1, 3), 2.0)]
        series = indicators.build_spread_series(pe, bond)
        self.assertEqual([day for day, _ in series], [date(2024, 1, 3)])


class PercentileTests(unittest.TestCase):
    def test_percentile_handles_duplicates(self):
        history = [1.0, 2.0, 2.0, 2.0, 3.0]
        # 小于 2 的 1 个，等于 2 的 3 个 → (1 + 1.5) / 5 = 50%
        self.assertAlmostEqual(indicators.percentile_rank(2.0, history), 50.0, places=9)
        # 小于 1 的 0 个，等于 1 的 1 个 → (0 + 0.5) / 5 = 10%
        self.assertAlmostEqual(indicators.percentile_rank(1.0, history), 10.0, places=9)
        # 小于 3 的 4 个，等于 3 的 1 个 → (4 + 0.5) / 5 = 90%
        self.assertAlmostEqual(indicators.percentile_rank(3.0, history), 90.0, places=9)

    def test_percentile_uses_unrounded_values(self):
        history = [0.06001, 0.06002, 0.06003]
        self.assertAlmostEqual(indicators.percentile_rank(0.060015, history), 100 / 3, places=9)

    def test_insufficient_sample_hides_percentile(self):
        series = [(date(2026, 1, 1), 0.03), (date(2026, 1, 2), 0.031)]
        stats = indicators.percentile_stats(series, min_samples=252)
        self.assertIsNotNone(stats)
        self.assertTrue(stats.insufficient)
        self.assertIsNone(stats.percentile)
        self.assertEqual(stats.sample_size, 1)

    def test_full_window_label_requires_history_before_window_start(self):
        short_history = [(date(2025, 1, 1), 0.02), (date(2026, 6, 1), 0.021)]
        partial = indicators.percentile_stats(short_history, min_samples=1)
        self.assertIsNotNone(partial)
        self.assertFalse(partial.window_full)
        self.assertEqual(partial.window_label, '可用历史百分位')

        long_history = [(date(2020, 1, 1), 0.01), (date(2026, 6, 1), 0.03)]
        full = indicators.percentile_stats(long_history, min_samples=1)
        self.assertIsNotNone(full)
        self.assertTrue(full.window_full)
        self.assertIn('5年百分位', full.window_label)
        self.assertEqual(full.sample_size, 0)

    def test_percentile_excludes_current_and_future_dates(self):
        series = [
            (date(2024, 1, 1), 0.01),
            (date(2024, 1, 2), 0.02),
            (date(2024, 1, 3), 0.03),
        ]
        stats = indicators.percentile_stats(series, min_samples=1)
        self.assertEqual(stats.sample_size, 2)
        self.assertEqual(stats.sample_end, date(2024, 1, 2))


def _month(index: int) -> tuple[date, float]:
    year = 2019 + index // 12
    month = index % 12 + 1
    return (date(year, month, 1), 0.02 + index / 10000.0)


class ChangeTests(unittest.TestCase):
    def test_change_units(self):
        self.assertAlmostEqual(indicators.change_in_bp(4.94, 5.01), -7.000000000000001, places=6)
        self.assertAlmostEqual(indicators.change_in_pct(6.6984, 6.7101), -0.1744, places=3)
        self.assertAlmostEqual(indicators.change_in_pp(45.0, 43.5), 1.5, places=9)

    def test_change_with_missing_previous(self):
        self.assertIsNone(indicators.change_in_bp(5.0, None))
        self.assertIsNone(indicators.change_in_pct(5.0, 0))


if __name__ == '__main__':
    unittest.main()
