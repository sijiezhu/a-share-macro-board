"""曲线粒度测试：一个视图内统一分辨率、只保留真实观测、近密远疏可选。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from macroboard import charts

END = date(2026, 9, 18)


def daily_series(days: int) -> list[tuple[date, float]]:
    start = END - timedelta(days=days - 1)
    return [(start + timedelta(days=index), 100.0 + index) for index in range(days)]


class AutoResolutionTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(charts.auto_resolution(250), charts.RESOLUTION_DAILY)
        self.assertEqual(charts.auto_resolution(365), charts.RESOLUTION_DAILY)
        self.assertEqual(charts.auto_resolution(750), charts.RESOLUTION_WEEKLY)
        self.assertEqual(charts.auto_resolution(1095), charts.RESOLUTION_WEEKLY)
        self.assertEqual(charts.auto_resolution(1250), charts.RESOLUTION_MONTHLY)

    def test_effective_resolution_resolves_auto(self):
        one_year = daily_series(365)
        five_years = daily_series(1250)
        self.assertEqual(
            charts.effective_resolution(charts.RESOLUTION_AUTO, one_year), charts.RESOLUTION_DAILY
        )
        self.assertEqual(
            charts.effective_resolution(charts.RESOLUTION_AUTO, five_years), charts.RESOLUTION_MONTHLY
        )
        self.assertEqual(
            charts.effective_resolution(charts.RESOLUTION_WEEKLY, one_year), charts.RESOLUTION_WEEKLY
        )

    def test_span_days(self):
        self.assertEqual(charts.span_days(daily_series(10)), 9)
        self.assertEqual(charts.span_days([]), 0)
        self.assertEqual(charts.span_days([(END, 1.0)]), 0)


class ResampleTests(unittest.TestCase):
    def test_daily_and_raw_keep_every_observation(self):
        series = daily_series(300)
        self.assertEqual(charts.resample(series, charts.RESOLUTION_DAILY), series)
        self.assertEqual(charts.resample(series, charts.RESOLUTION_RAW), series)

    def test_weekly_and_monthly_reduce_with_real_values(self):
        series = daily_series(800)
        weekly = charts.resample(series, charts.RESOLUTION_WEEKLY)
        monthly = charts.resample(series, charts.RESOLUTION_MONTHLY)
        self.assertLess(len(weekly), len(series))
        self.assertLess(len(monthly), len(weekly))
        source = dict(series)
        for sampled in (weekly, monthly):
            days = [day for day, _ in sampled]
            self.assertEqual(days, sorted(days))
            self.assertEqual(len(days), len(set(days)))
            for day, value in sampled:
                self.assertEqual(value, source[day])

    def test_weekly_keeps_last_observation_of_each_week(self):
        series = [
            (date(2026, 9, 7), 1.0),  # 周一
            (date(2026, 9, 11), 2.0),  # 周五
            (date(2026, 9, 14), 3.0),  # 下周一
        ]
        self.assertEqual(
            charts.resample(series, charts.RESOLUTION_WEEKLY),
            [(date(2026, 9, 11), 2.0), (date(2026, 9, 14), 3.0)],
        )

    def test_tiered_keeps_recent_daily_and_thins_older(self):
        series = daily_series(1200)
        tiered = charts.resample(series, charts.RESOLUTION_TIERED, now=END)
        recent_input = [row for row in series if (END - row[0]).days <= charts.CHART_RECENT_FULL_DAYS]
        recent_sampled = [row for row in tiered if (END - row[0]).days <= charts.CHART_RECENT_FULL_DAYS]
        self.assertEqual(len(recent_sampled), len(recent_input))
        self.assertLess(len(tiered), len(series) / 3)
        self.assertGreater(len(tiered), 90)

    def test_unknown_resolution_raises(self):
        with self.assertRaises(ValueError):
            charts.resample(daily_series(10), '不存在的粒度')

    def test_empty_series(self):
        self.assertEqual(charts.resample([], charts.RESOLUTION_WEEKLY), [])

    def test_tiered_boundaries_positions(self):
        boundaries = charts.tiered_boundaries(END)
        self.assertEqual(len(boundaries), 3)
        self.assertEqual(boundaries[0][0], END - timedelta(days=charts.CHART_RECENT_FULL_DAYS))
        self.assertIn('日线→周线', boundaries[0][1])


class SliceTests(unittest.TestCase):
    def test_slice_last_year(self):
        series = daily_series(1000)
        sliced = charts.slice_range(series, 365, now=END)
        self.assertEqual(len(sliced), 366)
        self.assertEqual(sliced[-1], series[-1])

    def test_slice_all_when_days_none(self):
        series = daily_series(10)
        self.assertEqual(charts.slice_range(series, None, now=END), series)

    def test_slice_empty(self):
        self.assertEqual(charts.slice_range([], 365, now=END), [])


class FormatTests(unittest.TestCase):
    def test_latest_text_units(self):
        rate = charts.ChartSpec('us10y', '美国10年期国债收益率', '%', '#fff', 2)
        fx = charts.ChartSpec('usdcny', '汇率', 'CNY/USD', '#fff', 4)
        gold = charts.ChartSpec('xauusd', '黄金', 'USD/金衡盎司', '#fff', 2)
        self.assertEqual(charts.latest_text([(END, 5.01)], rate), '5.01% · 2026-09-18')
        self.assertEqual(charts.latest_text([(END, 6.6984)], fx), '6.6984 · 2026-09-18')
        self.assertIn('4,348.15', charts.latest_text([(END, 4348.15)], gold))
        self.assertEqual(charts.latest_text([], rate), '未接通')

    def test_chart_specs_cover_six_indicators(self):
        keys = [spec.key for spec in charts.CHART_SPECS]
        self.assertEqual(keys, ['us10y', 'cn10y', 'usdcny', 'xauusd', 'adv_ratio', 'hs300_20d'])


if __name__ == '__main__':
    unittest.main()
