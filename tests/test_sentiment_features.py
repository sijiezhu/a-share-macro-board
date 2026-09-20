"""情绪模块 P1：分位、MACD、量比、分量注册表。

所有合成数据都是**构造样本**（SYNTHETIC FIXTURE），不是真实行情。
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from macroboard.sentiment import features
from macroboard.sentiment.config import (
    ComponentSpec,
    ScaleSpec,
    SentimentConfig,
    SentimentQuality,
    SentimentWindows,
)
from macroboard.sentiment.registry import (
    DEFAULT_COMPONENTS,
    PROFILE_MULTISCALE_SUGGESTED,
    SPEC_REQUIRED_COMPONENT_COLUMNS,
    emitted_columns,
)


def synthetic_frame(size: int = 320) -> pd.DataFrame:
    """确定性合成样本（正弦叠加趋势），不含随机数。"""
    dates = pd.bdate_range('2024-01-01', periods=size)
    close = [100.0 + 10.0 * math.sin(i / 17.0) + i * 0.05 for i in range(size)]
    volume = [1_000_000.0 * (1.0 + 0.4 * math.sin(i / 7.0)) for i in range(size)]
    return pd.DataFrame(
        {
            'date': dates,
            'close': close,
            'volume': volume,
            'amount': [v * c for v, c in zip(volume, close, strict=True)],
            'turnover_rate': [1.2 + 0.5 * math.sin(i / 11.0) for i in range(size)],
            'margin_net_buy': [2e8 * math.sin(i / 5.0) for i in range(size)],
            'up_count': [1500.0 + 800.0 * math.sin(i / 9.0) for i in range(size)],
            'down_count': [1500.0 - 800.0 * math.sin(i / 9.0) for i in range(size)],
            'limit_up_count': [max(0.0, 30.0 * math.sin(i / 13.0)) for i in range(size)],
            'limit_down_count': [max(0.0, -30.0 * math.sin(i / 13.0)) for i in range(size)],
            'pcr': [0.9 + 0.2 * math.sin(i / 8.0) for i in range(size)],
            'implied_volatility': [18.0 + 4.0 * math.cos(i / 15.0) for i in range(size)],
        }
    )


def naive_midrank(values: list[float], window: int, min_periods: int) -> list[float]:
    """朴素参考实现：严格早于当日，中位秩。"""
    out: list[float] = []
    for index, value in enumerate(values):
        sample = [v for v in values[max(0, index - window) : index] if not math.isnan(v)]
        if math.isnan(value) or len(sample) < min_periods:
            out.append(math.nan)
            continue
        lower = sum(1 for v in sample if v < value)
        equal = sum(1 for v in sample if v == value)
        out.append(100.0 * (lower + 0.5 * equal) / len(sample))
    return out


def naive_rank(values: list[float], window: int, min_periods: int) -> list[float]:
    """朴素参考实现：含当日，(<= x)/n。"""
    out: list[float] = []
    for index, value in enumerate(values):
        sample = [v for v in values[max(0, index - window + 1) : index + 1] if not math.isnan(v)]
        if math.isnan(value) or len(sample) < min_periods:
            out.append(math.nan)
            continue
        out.append(100.0 * sum(1 for v in sample if v <= value) / len(sample))
    return out


class MidrankPercentileTests(unittest.TestCase):
    def test_hand_table_matches_design_doc(self):
        series = pd.Series([50.0, 30.0, 40.0, 20.0, 10.0, 60.0, 35.0])
        pct, counts = features.midrank_percentile(series, window=5)
        expected = [math.nan] * 5 + [100.0, 60.0]
        for index, want in enumerate(expected):
            with self.subTest(index=index):
                if math.isnan(want):
                    self.assertTrue(math.isnan(pct.iloc[index]))
                else:
                    self.assertAlmostEqual(pct.iloc[index], want, places=12)
        self.assertEqual(list(counts.iloc[5:]), [5.0, 5.0])
        self.assertEqual(list(counts.iloc[:5]), [0.0, 1.0, 2.0, 3.0, 4.0])

    def test_ties_take_half_credit(self):
        series = pd.Series([1.0, 2.0, 2.0, 2.0, 3.0])
        pct, _ = features.midrank_percentile(series, window=5, min_periods=2)
        self.assertAlmostEqual(pct.iloc[3], 100.0 * (1 + 0.5 * 2) / 3, places=12)
        self.assertAlmostEqual(pct.iloc[4], 100.0, places=12)

    def test_constant_series_is_neutral_fifty(self):
        series = pd.Series([3.0] * 12)
        pct, _ = features.midrank_percentile(series, window=5, min_periods=2)
        self.assertAlmostEqual(pct.iloc[-1], 50.0, places=12)

    def test_requires_full_window_by_default(self):
        series = pd.Series([float(i) for i in range(10)])
        pct, _ = features.midrank_percentile(series, window=4)
        self.assertTrue(pct.iloc[:4].isna().all())
        self.assertFalse(pd.isna(pct.iloc[4]))

    def test_min_periods_override_relaxes_requirement(self):
        series = pd.Series([float(i) for i in range(10)])
        pct, _ = features.midrank_percentile(series, window=6, min_periods=3)
        self.assertTrue(pct.iloc[:3].isna().all())
        self.assertFalse(pd.isna(pct.iloc[3]))

    def test_matches_naive_reference_with_nans_and_ties(self):
        raw = [1.0, 2.0, 2.0, 5.0, 3.0, 3.0, math.nan, 4.0, 2.0, 6.0, 6.0, 5.0]
        series = pd.Series(raw * 4)
        pct, counts = features.midrank_percentile(series, window=9, min_periods=4)
        expected = naive_midrank(list(series), 9, 4)
        for index, want in enumerate(expected):
            with self.subTest(index=index):
                got = pct.iloc[index]
                if math.isnan(want):
                    self.assertTrue(math.isnan(got))
                else:
                    self.assertAlmostEqual(got, want, places=10)
        self.assertEqual(counts.iloc[-1], len([v for v in series.iloc[-10:-1] if not math.isnan(v)]))

    def test_no_lookahead_prefix_invariance(self):
        series = pd.Series([math.sin(i / 3.0) * 10 + i for i in range(60)])
        full, _ = features.midrank_percentile(series, window=7, min_periods=4)
        for prefix in (12, 25, 60):
            part, _ = features.midrank_percentile(series.iloc[:prefix], window=7, min_periods=4)
            pd.testing.assert_series_equal(part, full.iloc[:prefix], check_names=False)

    def test_future_values_do_not_change_past(self):
        base = [math.sin(i / 5.0) * 3 + i * 0.1 for i in range(50)]
        series = pd.Series(base)
        before, _ = features.midrank_percentile(series, window=10, min_periods=5)
        polluted = series.copy()
        polluted.iloc[35:] = polluted.iloc[35:] * 10.0
        after, _ = features.midrank_percentile(polluted, window=10, min_periods=5)
        pd.testing.assert_series_equal(before.iloc[:35], after.iloc[:35], check_names=False)

    def test_invalid_window_raises(self):
        series = pd.Series([1.0, 2.0, 3.0])
        with self.assertRaises(ValueError):
            features.midrank_percentile(series, window=1)
        with self.assertRaises(ValueError):
            features.midrank_percentile(series, window=3, min_periods=5)


class RankPercentileTests(unittest.TestCase):
    def test_hand_table_matches_design_doc(self):
        series = pd.Series([50.0, 30.0, 40.0, 20.0, 10.0, 60.0])
        pct, _ = features.rank_percentile(series, window=5, min_periods=3)
        expected = [math.nan, math.nan, 200.0 / 3, 25.0, 20.0, 100.0]
        for index, want in enumerate(expected):
            with self.subTest(index=index):
                if math.isnan(want):
                    self.assertTrue(math.isnan(pct.iloc[index]))
                else:
                    self.assertAlmostEqual(pct.iloc[index], want, places=10)

    def test_constant_series_is_max(self):
        series = pd.Series([3.0] * 12)
        pct, _ = features.rank_percentile(series, window=5, min_periods=2)
        self.assertAlmostEqual(pct.iloc[-1], 100.0, places=12)

    def test_matches_naive_reference(self):
        raw = [2.0, 1.0, 5.0, 5.0, 3.0, 3.0, math.nan, 4.0, 2.0, 6.0]
        series = pd.Series(raw * 3)
        pct, _ = features.rank_percentile(series, window=6, min_periods=3)
        expected = naive_rank(list(series), 6, 3)
        for index, want in enumerate(expected):
            with self.subTest(index=index):
                got = pct.iloc[index]
                if math.isnan(want):
                    self.assertTrue(math.isnan(got))
                else:
                    self.assertAlmostEqual(got, want, places=10)

    def test_dispatch_unknown_method_raises(self):
        with self.assertRaises(ValueError):
            features.rolling_percentile(pd.Series([1.0, 2.0, 3.0]), 3, None, 'nope')


class MacdTests(unittest.TestCase):
    def test_ema_uses_recursive_form(self):
        close = pd.Series([10.0, 11.0, 12.0, 11.0, 13.0, 12.0, 14.0, 15.0])
        out = features.ema_features(close, fast=2, slow=3, signal=2)
        alpha_fast = 2.0 / 3.0
        ema_fast = close.ewm(span=2, adjust=False).mean()
        manual = [close.iloc[0]]
        for value in close.iloc[1:]:
            manual.append(alpha_fast * value + (1 - alpha_fast) * manual[-1])
        self.assertAlmostEqual(ema_fast.iloc[0], manual[0], places=12)
        for index, want in enumerate(manual):
            with self.subTest(index=index):
                self.assertAlmostEqual(ema_fast.iloc[index], want, places=10)

        expected_hist = 2.0 * (out['macd'] - out['macd_signal'])
        pd.testing.assert_series_equal(out['macd_hist'], expected_hist, check_names=False)

    def test_adjust_false_differs_from_adjust_true(self):
        close = pd.Series([10.0, 12.0, 9.0, 15.0, 14.0, 18.0])
        recursive = close.ewm(span=3, adjust=False).mean()
        weighted = close.ewm(span=3, adjust=True).mean()
        self.assertGreater(float((recursive - weighted).abs().max()), 1e-6)

    def test_hist_norm_scales_with_close(self):
        close = pd.Series([100.0, 101.0, 102.0, 101.0, 100.0, 99.0])
        out = features.ema_features(close, fast=2, slow=3, signal=2)
        expected = out['macd_hist'] / close * 100.0
        pd.testing.assert_series_equal(out['macd_hist_norm'], expected, check_names=False)

    def test_ffill_state_masks_missing_rows_but_keeps_recursion(self):
        close = pd.Series([10.0, 11.0, 12.0, np.nan, 13.0, 14.0])
        out = features.ema_features(close, fast=2, slow=3, signal=2, policy='ffill_state')
        self.assertTrue(out.iloc[3].isna().all())
        self.assertFalse(out.iloc[4].isna().any())

    def test_nan_propagate_differs_from_ffill_state(self):
        close = pd.Series([10.0, 11.0, 12.0, np.nan, 13.0, 14.0])
        filled = features.ema_features(close, fast=2, slow=3, signal=2, policy='ffill_state')
        propagated = features.ema_features(close, fast=2, slow=3, signal=2, policy='nan_propagate')
        # 两种策略都不发布缺失行的数值；差异只体现在缺失之后的 EMA 递归状态。
        self.assertTrue(filled.iloc[3].isna().all())
        self.assertTrue(propagated.iloc[3].isna().all())
        self.assertGreater(float((filled['macd'] - propagated['macd']).abs().max()), 1e-9)

    def test_warmup_bars_masks_prefix(self):
        close = pd.Series([float(i) for i in range(20)])
        out = features.ema_features(close, fast=2, slow=3, signal=2, warmup_bars=5)
        self.assertTrue(out.iloc[:5].isna().all().all())
        self.assertFalse(out.iloc[5].isna().any())

    def test_invalid_policy_raises(self):
        with self.assertRaises(ValueError):
            features.ema_features(pd.Series([1.0, 2.0]), fast=2, slow=3, signal=2, policy='magic')


class RatioAndVolumeTests(unittest.TestCase):
    def test_volume_ratio_requires_full_window(self):
        volume = pd.Series([float(100 + i) for i in range(25)])
        ratio = features.volume_ratio(volume, 20)
        self.assertTrue(ratio.iloc[:19].isna().all())
        expected = volume.iloc[19] / volume.iloc[:20].mean()
        self.assertAlmostEqual(ratio.iloc[19], expected, places=12)

    def test_volume_ratio_zero_mean_is_nan(self):
        ratio = features.volume_ratio(pd.Series([0.0] * 25), 20)
        self.assertTrue(ratio.isna().all())

    def test_returns_guard_non_positive_base(self):
        close = pd.Series([0.0, 1.0, 2.0, 3.0])
        result = features.return_series(close, 1)
        self.assertTrue(math.isnan(result.iloc[1]))
        self.assertAlmostEqual(result.iloc[3], 0.5, places=12)

    def test_ratio_features_zero_division_guard(self):
        frame = pd.DataFrame(
            {
                'up_count': [0.0, 10.0],
                'down_count': [0.0, 30.0],
                'limit_up_count': [5.0, 5.0],
                'limit_down_count': [0.0, 0.0],
            }
        )
        out = features.ratio_features(frame)
        self.assertTrue(math.isnan(out['up_ratio'].iloc[0]))
        self.assertTrue(math.isnan(out['down_ratio'].iloc[0]))
        self.assertAlmostEqual(out['up_ratio'].iloc[1], 0.25, places=12)
        self.assertAlmostEqual(out['limit_up_down_ratio'].iloc[1], 5.0, places=12)


class BlendScaleTests(unittest.TestCase):
    @staticmethod
    def _component(scales: tuple[ScaleSpec, ...]) -> ComponentSpec:
        return ComponentSpec(name='turnover', source='turnover_rate', group='participation', scales=scales)

    def test_weighted_average_of_two_scales(self):
        index = pd.RangeIndex(3)
        component = self._component((ScaleSpec(63, 0.4), ScaleSpec(252, 0.6)))
        results = {
            63: features.ScaleResult(pd.Series([90.0, 90.0, 90.0], index=index), pd.Series([63.0] * 3, index=index)),
            252: features.ScaleResult(pd.Series([60.0] * 3, index=index), pd.Series([252.0] * 3, index=index)),
        }
        blended = features.blend_scales(results, component, min_coverage=0.5)
        for value in blended.value:
            self.assertAlmostEqual(value, 72.0, places=12)
        self.assertFalse(bool(blended.partial.any()))
        self.assertEqual(list(blended.count), [252.0, 252.0, 252.0])

    def test_missing_scale_is_renormalised_and_flagged(self):
        index = pd.RangeIndex(3)
        component = self._component((ScaleSpec(63, 0.4), ScaleSpec(252, 0.6)))
        results = {
            63: features.ScaleResult(pd.Series([math.nan, 90.0, math.nan], index=index), pd.Series([0.0, 63.0, 30.0], index=index)),
            252: features.ScaleResult(pd.Series([60.0, 60.0, 60.0], index=index), pd.Series([252.0] * 3, index=index)),
        }
        blended = features.blend_scales(results, component, min_coverage=0.5)
        self.assertAlmostEqual(blended.value.iloc[0], 60.0, places=12)
        self.assertTrue(bool(blended.partial.iloc[0]))
        self.assertFalse(bool(blended.partial.iloc[1]))

    def test_coverage_below_threshold_is_nan(self):
        index = pd.RangeIndex(2)
        component = self._component((ScaleSpec(63, 0.4), ScaleSpec(252, 0.6)))
        results = {
            63: features.ScaleResult(pd.Series([90.0, 90.0], index=index), pd.Series([63.0, 63.0], index=index)),
            252: features.ScaleResult(pd.Series([math.nan, math.nan], index=index), pd.Series([0.0, 0.0], index=index)),
        }
        blended = features.blend_scales(results, component, min_coverage=0.7)
        self.assertTrue(blended.value.isna().all())


class ComputeFeaturesTests(unittest.TestCase):
    def test_default_columns_match_spec_required_names(self):
        frame = synthetic_frame(80)
        bundle = features.compute_features(frame)
        count_columns = [name for name in bundle.percentiles.columns if name.endswith('_n')]
        value_columns = [name for name in bundle.percentiles.columns if not name.endswith('_n')]
        self.assertEqual(sorted(value_columns), sorted(SPEC_REQUIRED_COMPONENT_COLUMNS))
        self.assertEqual(len(count_columns), len(DEFAULT_COMPONENTS))

    def test_per_component_windows_are_independent(self):
        components = tuple(
            ComponentSpec(
                name=component.name,
                source=component.source,
                group=component.group,
                sign=component.sign,
                scales=(ScaleSpec(63),) if component.name == 'limit_up_down_ratio' else (ScaleSpec(252),),
                availability_lag=component.availability_lag,
            )
            for component in DEFAULT_COMPONENTS
        )
        config = SentimentConfig(quality=SentimentQuality(components=components))
        frame = synthetic_frame(200)
        bundle = features.compute_features(frame, config)
        self.assertIn('limit_up_down_ratio_pct_63', bundle.percentiles.columns)
        self.assertNotIn('limit_up_down_ratio_pct_252', bundle.percentiles.columns)
        self.assertIn('turnover_pct_252', bundle.percentiles.columns)
        self.assertNotIn('turnover_pct_63', bundle.percentiles.columns)
        derived = features.ratio_features(frame)['limit_up_down_ratio']
        manual, _ = features.midrank_percentile(derived, 63, 30)
        pd.testing.assert_series_equal(
            bundle.percentiles['limit_up_down_ratio_pct_63'], manual, check_names=False
        )

    def test_mode_a_min_periods_is_derived_from_window(self):
        components = tuple(
            ComponentSpec(
                name=component.name,
                source=component.source,
                group=component.group,
                sign=component.sign,
                scales=(ScaleSpec(126, method='rank_inclusive'),) if component.name == 'amount' else component.scales,
                availability_lag=component.availability_lag,
            )
            for component in DEFAULT_COMPONENTS
        )
        config = SentimentConfig(quality=SentimentQuality(components=components, percentile_method='rank_inclusive'))
        bundle = features.compute_features(synthetic_frame(180), config)
        self.assertEqual(bundle.meta['amount'].min_periods, (60,))
        other = features.compute_features(synthetic_frame(180))
        self.assertEqual(other.meta['amount'].min_periods, (120,))

    def test_multiscale_profile_emits_blend_column(self):
        config = SentimentConfig(quality=SentimentQuality(components=PROFILE_MULTISCALE_SUGGESTED))
        bundle = features.compute_features(synthetic_frame(320), config)
        self.assertIn('turnover_pct_63', bundle.percentiles.columns)
        self.assertIn('turnover_pct_252', bundle.percentiles.columns)
        self.assertIn('turnover_pct', bundle.percentiles.columns)
        self.assertIn('pcr_reverse_pct', bundle.percentiles.columns)
        self.assertNotIn('pcr_reverse_pct_252', bundle.percentiles.columns)
        self.assertIn('pcr_reverse_pct_n', bundle.percentiles.columns)
        for component in PROFILE_MULTISCALE_SUGGESTED:
            for column in emitted_columns(component):
                self.assertIn(column, bundle.percentiles.columns)

    def test_blend_value_matches_manual_average(self):
        config = SentimentConfig(quality=SentimentQuality(components=PROFILE_MULTISCALE_SUGGESTED))
        frame = synthetic_frame(320)
        bundle = features.compute_features(frame, config)
        mask = bundle.percentiles['turnover_pct_63'].notna() & bundle.percentiles['turnover_pct_252'].notna()
        manual = (
            0.4 * bundle.percentiles['turnover_pct_63'] + 0.6 * bundle.percentiles['turnover_pct_252']
        )
        pd.testing.assert_series_equal(
            bundle.percentiles.loc[mask, 'turnover_pct'],
            manual[mask],
            check_names=False,
        )

    def test_component_values_are_bounded(self):
        config = SentimentConfig(quality=SentimentQuality(components=PROFILE_MULTISCALE_SUGGESTED))
        bundle = features.compute_features(synthetic_frame(320), config)
        for name, series in bundle.values.items():
            valid = series.dropna()
            with self.subTest(component=name):
                self.assertGreaterEqual(float(valid.min()), 0.0)
                self.assertLessEqual(float(valid.max()), 100.0)

    def test_reverse_components_are_inverted(self):
        frame = synthetic_frame(320)
        bundle = features.compute_features(frame)
        pcr_pct, _ = features.midrank_percentile(frame['pcr'], 252, 120)
        expected = 100.0 - pcr_pct
        pd.testing.assert_series_equal(
            bundle.percentiles['pcr_reverse_pct'], expected, check_names=False
        )

    def test_prefix_invariance_of_full_feature_set(self):
        frame = synthetic_frame(300)
        full = features.compute_features(frame)
        part = features.compute_features(frame.iloc[:150])
        for column in full.percentiles.columns:
            with self.subTest(column=column):
                pd.testing.assert_series_equal(
                    part.percentiles[column], full.percentiles[column].iloc[:150], check_names=False
                )
        for column in full.base.columns:
            with self.subTest(column=column):
                pd.testing.assert_series_equal(
                    part.base[column], full.base[column].iloc[:150], check_names=False
                )

    def test_margin_net_buy_uses_availability_lag(self):
        frame = synthetic_frame(320)
        bundle = features.compute_features(frame)
        lagged, _ = features.midrank_percentile(frame['margin_net_buy'].shift(1), 252, 120)
        pd.testing.assert_series_equal(
            bundle.percentiles['margin_net_buy_pct_252'], lagged, check_names=False
        )

    def test_unknown_source_raises(self):
        component = ComponentSpec(name='mystery', source='not_a_source', group='direction')
        config = SentimentConfig(quality=SentimentQuality(components=(component,)))
        with self.assertRaises(ValueError):
            features.compute_features(synthetic_frame(30), config)

    def test_missing_optional_columns_are_marked_not_fatal(self):
        frame = synthetic_frame(300).drop(
            columns=['pcr', 'implied_volatility', 'margin_net_buy', 'limit_up_count', 'limit_down_count']
        )
        bundle = features.compute_features(frame)
        for column in ('pcr_reverse_pct', 'iv_reverse_pct', 'margin_net_buy_pct_252', 'limit_up_down_ratio_pct_252'):
            with self.subTest(column=column):
                self.assertTrue(bundle.percentiles[column].isna().all())
        self.assertTrue(bundle.percentiles['turnover_pct_252'].notna().any())

    def test_sentiment_window_default_is_252(self):
        self.assertEqual(SentimentWindows().sentiment_percentile_scales[0].window, 252)


if __name__ == '__main__':
    unittest.main()
