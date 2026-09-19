"""页面数据组装：上涨家数占比、20日涨跌幅、状态聚合。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from macroboard import dashboard


class ViewTests(unittest.TestCase):
    def test_advance_ratio_includes_flat_in_denominator(self):
        rows = dashboard.advance_ratio_series(
            [(date(2026, 9, 18), 3937.0)],
            [(date(2026, 9, 18), 1109.0)],
            [(date(2026, 9, 18), 163.0)],
        )
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][1], 3937 / (3937 + 1109 + 163) * 100, places=9)

    def test_advance_ratio_skips_dates_without_all_three_counts(self):
        rows = dashboard.advance_ratio_series(
            [(date(2026, 9, 18), 3937.0), (date(2026, 9, 17), 2504.0)],
            [(date(2026, 9, 18), 1109.0)],
            [(date(2026, 9, 18), 163.0)],
        )
        self.assertEqual([day for day, _ in rows], [date(2026, 9, 18)])

    def test_rolling_change_uses_20_trading_days(self):
        start = date(2026, 1, 1)
        rows = [(start + timedelta(days=index), 100.0 + index) for index in range(21)]
        change = dashboard.rolling_change(rows, 20)
        self.assertAlmostEqual(change, (120.0 / 100.0 - 1) * 100, places=9)

    def test_rolling_change_needs_enough_history(self):
        rows = [(date(2026, 1, 1) + timedelta(days=index), 100.0) for index in range(5)]
        self.assertIsNone(dashboard.rolling_change(rows, 20))

    def test_rolling_change_series_matches_scalar(self):
        start = date(2026, 1, 1)
        rows = [(start + timedelta(days=index), 100.0 + index) for index in range(30)]
        series = dashboard.rolling_change_series(rows, 20)
        self.assertEqual(len(series), 10)
        self.assertAlmostEqual(series[-1][1], dashboard.rolling_change(rows, 20), places=9)
        self.assertEqual(series[0][0], rows[20][0])

    def test_format_change_units(self):
        self.assertEqual(dashboard.format_change('bp', -7.0), '-7.0 BP')
        self.assertEqual(dashboard.format_change('pct', 1.234), '+1.23%')
        self.assertEqual(dashboard.format_change('pp', 1.5), '+1.50 个百分点')

    def test_worse_status_priority(self):
        self.assertEqual(dashboard._worse_status('normal', 'stale'), 'stale')
        self.assertEqual(dashboard._worse_status('failed', 'unconfigured'), 'unconfigured')


if __name__ == '__main__':
    unittest.main()
