"""情绪模块 P2：指数合成、状态码、恐慌分、反转观察与量价状态。"""

from __future__ import annotations

import math
import unittest

import pandas as pd

from macroboard.sentiment import codes, labels, scoring
from macroboard.sentiment.config import (
    ComponentSpec,
    ScaleSpec,
    SentimentConfig,
    SentimentQuality,
    SentimentThresholds,
)
from macroboard.sentiment.features import compute_features
from macroboard.sentiment.inputs import normalize_frame
from macroboard.sentiment.pipeline import compute_sentiment


def series(values, index=None):
    return pd.Series([float(v) for v in values], index=index)


class StateTests(unittest.TestCase):
    def test_state_codes_follow_thresholds(self):
        participation = series([60, 40, 60.1, 60.1, 39.9, 39.9, 70])
        direction = series([60, 40, 60.1, 39.9, 39.9, 60.1, 50])
        thresholds = SentimentThresholds()
        state = scoring.classify_state(participation, direction, thresholds)
        self.assertEqual(
            list(state),
            [
                codes.STATE_NEUTRAL,
                codes.STATE_NEUTRAL,
                codes.STATE_GREED,
                codes.STATE_PANIC,
                codes.STATE_COLD,
                codes.STATE_THAW,
                codes.STATE_NEUTRAL,
            ],
        )
        for value in state:
            self.assertIn(value, codes.STATE_CODES)

    def test_missing_index_maps_to_unavailable(self):
        participation = series([70, math.nan])
        direction = series([70, 70])
        state = scoring.classify_state(participation, direction, SentimentThresholds())
        self.assertEqual(state.iloc[1], codes.STATE_UNAVAILABLE)

    def test_thresholds_are_configurable(self):
        thresholds = SentimentThresholds(high_participation=70, low_participation=30)
        participation = series([65])
        direction = series([65])
        self.assertEqual(
            scoring.classify_state(participation, direction, thresholds).iloc[0], codes.STATE_NEUTRAL
        )
        self.assertEqual(
            scoring.classify_state(series([75]), series([75]), thresholds).iloc[0], codes.STATE_GREED
        )

    def test_bands(self):
        values = series([70, 50, 30, math.nan])
        band = scoring.index_band(values, low=40, high=60)
        self.assertEqual(
            list(band),
            [codes.BAND_HIGH, codes.BAND_MID, codes.BAND_LOW, codes.BAND_UNAVAILABLE],
        )


class ComposeIndexTests(unittest.TestCase):
    @staticmethod
    def _specs() -> list[ComponentSpec]:
        return [
            ComponentSpec(name='a', source='amount', group='participation'),
            ComponentSpec(name='b', source='turnover_rate', group='participation'),
            ComponentSpec(name='c', source='pcr', group='participation'),
            ComponentSpec(name='d', source='implied_volatility', group='participation'),
        ]

    def test_equal_weight_average(self):
        values = {
            'a': series([80.0, 80.0]),
            'b': series([60.0, 60.0]),
            'c': series([40.0, 40.0]),
            'd': series([20.0, 20.0]),
        }
        result = scoring.compose_index(values, self._specs(), min_coverage=0.5)
        self.assertAlmostEqual(result.value.iloc[0], 50.0, places=12)
        self.assertAlmostEqual(result.coverage.iloc[0], 1.0, places=12)

    def test_missing_components_are_skipped_and_weight_rebalanced(self):
        values = {
            'a': series([80.0]),
            'b': series([60.0]),
            'c': series([math.nan]),
            'd': series([math.nan]),
        }
        result = scoring.compose_index(values, self._specs(), min_coverage=0.5)
        self.assertAlmostEqual(result.value.iloc[0], 70.0, places=12)
        self.assertAlmostEqual(result.coverage.iloc[0], 0.5, places=12)
        self.assertEqual(result.never_available, ('c', 'd'))
        self.assertEqual(result.missing_asof, ('c', 'd'))

    def test_coverage_below_minimum_is_nan(self):
        values = {
            'a': series([80.0]),
            'b': series([math.nan]),
            'c': series([math.nan]),
            'd': series([math.nan]),
        }
        result = scoring.compose_index(values, self._specs(), min_coverage=0.5)
        self.assertTrue(math.isnan(result.value.iloc[0]))
        self.assertAlmostEqual(result.coverage.iloc[0], 0.25, places=12)

    def test_values_are_clipped_to_0_100(self):
        values = {name: series([150.0]) for name in ('a', 'b', 'c')}
        values['d'] = series([-50.0])
        result = scoring.compose_index(values, self._specs(), min_coverage=0.5)
        self.assertLessEqual(float(result.value.iloc[0]), 100.0)
        self.assertGreaterEqual(float(result.value.iloc[0]), 0.0)

    def test_weighted_composition(self):
        specs = [
            ComponentSpec(name='a', source='amount', group='participation', weight=3.0),
            ComponentSpec(name='b', source='turnover_rate', group='participation', weight=1.0),
        ]
        values = {'a': series([80.0]), 'b': series([40.0])}
        result = scoring.compose_index(values, specs, min_coverage=0.5)
        self.assertAlmostEqual(result.value.iloc[0], 70.0, places=12)


class PanicTests(unittest.TestCase):
    """构造样本：用显式序列逐一验证 5 个条件。"""

    @staticmethod
    def _inputs(size: int = 6) -> dict[str, pd.Series]:
        return {
            'turnover_pct': series([10.0] * (size - 1) + [50.0]),
            'return_20d': series([0.0] * (size - 1) + [0.0]),
            'volume_ratio': series([1.0] * size),
            'macd_hist': series([0.5] * size),
            'down_ratio': series([0.5] * size),
        }

    def _panic(self, **overrides) -> scoring.PanicResult:
        inputs = self._inputs()
        inputs.update(overrides)
        return scoring.panic_metrics(**inputs, thresholds=SentimentThresholds())

    def test_no_condition_is_zero(self):
        result = self._panic()
        self.assertAlmostEqual(float(result.score.iloc[-1]), 0.0, places=12)
        self.assertEqual(int(result.hits.iloc[-1]), 0)
        self.assertAlmostEqual(float(result.coverage.iloc[-1]), 1.0, places=12)
        self.assertEqual(result.reasons.iloc[-1], ())

    def test_each_condition_alone_scores_100_after_renormalisation(self):
        cases = {
            codes.PANIC_TURNOVER_PCT_HIGH: {'turnover_pct': series([10.0] * 5 + [95.0])},
            codes.PANIC_RETURN_20D_LOW: {'return_20d': series([0.0] * 5 + [-0.12])},
            codes.PANIC_VOLUME_RATIO_HIGH: {'volume_ratio': series([1.0] * 5 + [2.0])},
            codes.PANIC_HIST_NEGATIVE_FALLING: {
                'macd_hist': series([0.5, 0.5, 0.5, 0.5, -1.0, -2.0])
            },
            codes.PANIC_DOWN_RATIO_HIGH: {'down_ratio': series([0.5] * 5 + [0.8])},
        }
        # 单条件场景需要屏蔽其他条件，故逐条把其余输入设为不可评估
        for expected_code, override in cases.items():
            with self.subTest(code=expected_code):
                inputs = {name: series([math.nan] * 6) for name in self._inputs()}
                inputs.update(override)
                result = scoring.panic_metrics(**inputs, thresholds=SentimentThresholds())
                self.assertEqual(result.reasons.iloc[-1], (expected_code,))
                self.assertAlmostEqual(float(result.coverage.iloc[-1]), 0.2, places=12)
                self.assertAlmostEqual(float(result.score.iloc[-1]), 100.0, places=12)

    def test_all_conditions_score_100_with_full_coverage(self):
        result = self._panic(
            turnover_pct=series([10.0] * 5 + [95.0]),
            return_20d=series([0.0] * 5 + [-0.12]),
            volume_ratio=series([1.0] * 5 + [2.0]),
            macd_hist=series([0.5, 0.5, 0.5, 0.5, -1.0, -2.0]),
            down_ratio=series([0.5] * 5 + [0.8]),
        )
        self.assertAlmostEqual(float(result.score.iloc[-1]), 100.0, places=12)
        self.assertEqual(int(result.hits.iloc[-1]), 5)
        self.assertAlmostEqual(float(result.coverage.iloc[-1]), 1.0, places=12)
        self.assertAlmostEqual(float(result.confidence.iloc[-1]), 1.0, places=12)
        self.assertEqual(result.reasons.iloc[-1], codes.PANIC_CONDITION_CODES)

    def test_renormalisation_uses_evaluable_conditions_only(self):
        inputs = {name: series([math.nan] * 6) for name in self._inputs()}
        inputs['turnover_pct'] = series([10.0] * 5 + [95.0])
        inputs['return_20d'] = series([0.0] * 5 + [-0.12])
        inputs['volume_ratio'] = series([1.0] * 5 + [2.0])
        inputs['down_ratio'] = series([0.5] * 6)
        result = scoring.panic_metrics(**inputs, thresholds=SentimentThresholds())
        self.assertAlmostEqual(float(result.coverage.iloc[-1]), 0.8, places=12)
        self.assertAlmostEqual(float(result.score.iloc[-1]), 75.0, places=12)
        self.assertEqual(int(result.hits.iloc[-1]), 3)
        self.assertEqual(result.missing.iloc[-1], (codes.PANIC_HIST_NEGATIVE_FALLING,))

    def test_no_evaluable_condition_is_unavailable(self):
        inputs = {name: series([math.nan] * 6) for name in self._inputs()}
        result = scoring.panic_metrics(**inputs, thresholds=SentimentThresholds())
        self.assertTrue(math.isnan(float(result.score.iloc[-1])))
        self.assertTrue(pd.isna(result.hits.iloc[-1]))
        self.assertAlmostEqual(float(result.coverage.iloc[-1]), 0.0, places=12)

    def test_confidence_combines_coverage_and_readiness(self):
        result = self._panic(readiness=series([0.5] * 6))
        self.assertAlmostEqual(float(result.confidence.iloc[-1]), 0.5, places=12)
        result_full = self._panic()
        self.assertAlmostEqual(float(result_full.confidence.iloc[-1]), 1.0, places=12)

    def test_thresholds_are_configurable(self):
        thresholds = SentimentThresholds(panic_return_20d=-0.15, panic_volume_ratio=1.2)
        result = scoring.panic_metrics(
            turnover_pct=series([10.0] * 6),
            return_20d=series([0.0] * 5 + [-0.12]),
            volume_ratio=series([1.0] * 5 + [1.3]),
            macd_hist=series([0.5] * 6),
            down_ratio=series([0.5] * 6),
            thresholds=thresholds,
        )
        self.assertEqual(result.reasons.iloc[-1], (codes.PANIC_VOLUME_RATIO_HIGH,))


class ReversalTests(unittest.TestCase):
    @staticmethod
    def _panic(score: float, coverage: float) -> scoring.PanicResult:
        index = pd.RangeIndex(8)
        return scoring.PanicResult(
            score=pd.Series([score] * 8, index=index, dtype='float64'),
            hits=pd.Series(pd.array([3] * 8, dtype='Int64'), index=index),
            coverage=pd.Series([coverage] * 8, index=index, dtype='float64'),
            confidence=pd.Series([coverage] * 8, index=index, dtype='float64'),
            reasons=pd.Series([()] * 8, index=index, dtype='object'),
            missing=pd.Series([()] * 8, index=index, dtype='object'),
        )

    @staticmethod
    def _inputs() -> dict[str, pd.Series]:
        return {
            # 末三日：-1.5 > -2.0 > -2.5，且都为负 -> 负柱连续 2 日缩短
            'macd_hist': series([-5.0, -4.0, -3.0, -2.0, -1.0, -2.5, -2.0, -1.5]),
            'volume': series([100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 30.0]),
            'direction_index': series([20.0, 20.0, 20.0, 21.0, 22.0, 23.0, 24.0, 30.0]),
        }

    def _reversal(self, *, panic: scoring.PanicResult | None = None, **overrides):
        inputs = self._inputs()
        inputs.update(overrides)
        return scoring.reversal_metrics(
            panic=panic if panic is not None else self._panic(80.0, 1.0),
            volume_ma=3,
            shrink=0.30,
            panic_score_threshold=60.0,
            coverage_min=0.60,
            rebound_lag=5,
            **inputs,
        )

    def test_all_conditions_fire(self):
        result, shrink_ratio = self._reversal()
        self.assertTrue(bool(result.watch.iloc[-1]))
        self.assertEqual(result.reasons.iloc[-1], codes.REV_CONDITION_CODES)
        self.assertIsNone(result.block_reason.iloc[-1])
        self.assertAlmostEqual(float(shrink_ratio.iloc[-1]), 0.3, places=12)

    def test_each_condition_is_required(self):
        cases = {
            codes.REV_PANIC_SCORE: {'panic': self._panic(50.0, 1.0)},
            codes.REV_HIST_SHRINKING: {'macd_hist': series([-5.0, -4.0, -3.0, -2.0, -1.0, -1.5, -2.0, -3.0])},
            codes.REV_DIRECTION_REBOUND: {'direction_index': series([30.0] * 8)},
        }
        for expected_missing, override in cases.items():
            with self.subTest(condition=expected_missing):
                result, _ = self._reversal(**override)
                self.assertFalse(bool(result.watch.iloc[-1]))
                self.assertNotIn(expected_missing, result.reasons.iloc[-1])
        # 缩量条件用成交量破坏
        result, _ = self._reversal(volume=series([1e6] * 8))
        self.assertFalse(bool(result.watch.iloc[-1]))
        self.assertNotIn(codes.REV_VOLUME_SHRUNK, result.reasons.iloc[-1])

    def test_volume_shrink_uses_strict_threshold(self):
        # 峰值 100：成交量 70 -> 缩量 30%，严格大于 30% 才算满足
        result_equal, ratio_equal = self._reversal(volume=series([100.0] * 7 + [70.0]))
        self.assertAlmostEqual(float(ratio_equal.iloc[-1]), 0.7, places=12)
        self.assertFalse(bool(result_equal.conditions[codes.REV_VOLUME_SHRUNK].iloc[-1]))
        result_below, ratio_below = self._reversal(volume=series([100.0] * 7 + [69.0]))
        self.assertLess(float(ratio_below.iloc[-1]), 0.7)
        self.assertTrue(bool(result_below.conditions[codes.REV_VOLUME_SHRUNK].iloc[-1]))

    def test_low_panic_coverage_blocks_the_signal(self):
        result, _ = self._reversal(panic=self._panic(80.0, 0.4))
        self.assertFalse(bool(result.watch.iloc[-1]))
        self.assertEqual(result.block_reason.iloc[-1], codes.REV_BLOCK_PANIC_COVERAGE_LOW)

    def test_coverage_at_the_boundary_is_allowed(self):
        result, _ = self._reversal(panic=self._panic(80.0, 0.60))
        self.assertTrue(bool(result.watch.iloc[-1]))
        self.assertIsNone(result.block_reason.iloc[-1])

    def test_missing_condition_data_blocks_with_reason(self):
        result, _ = self._reversal(macd_hist=series([math.nan] * 8))
        self.assertFalse(bool(result.watch.iloc[-1]))
        self.assertEqual(result.block_reason.iloc[-1], codes.REV_BLOCK_CONDITION_DATA_MISSING)
        self.assertIn(codes.REV_HIST_SHRINKING, result.missing.iloc[-1])

    def test_missing_panic_score_blocks_with_reason(self):
        panic = self._panic(80.0, 1.0)
        nan_series = pd.Series([math.nan] * 8, index=panic.score.index, dtype='float64')
        broken = scoring.PanicResult(
            score=nan_series,
            hits=panic.hits,
            coverage=panic.coverage,
            confidence=panic.confidence,
            reasons=panic.reasons,
            missing=panic.missing,
        )
        result, _ = self._reversal(panic=broken)
        self.assertFalse(bool(result.watch.iloc[-1]))
        self.assertEqual(result.block_reason.iloc[-1], codes.REV_BLOCK_PANIC_SCORE_MISSING)

    def test_watch_is_never_nullable(self):
        result, _ = self._reversal(macd_hist=series([math.nan] * 8))
        self.assertEqual(str(result.watch.dtype), 'bool')
        self.assertTrue(set(result.watch.unique()).issubset({True, False}))


class MacdVolumeTests(unittest.TestCase):
    def _state(self, *, macd, signal, ratio, close=None, hist=None, lookback=3, require_negative=False):
        size = len(macd)
        result = scoring.macd_volume_metrics(
            macd=series(macd),
            macd_signal=series(signal),
            volume_ratio=series(ratio),
            close=series(close if close is not None else [10.0] * size),
            macd_hist=series(hist if hist is not None else [0.1] * size),
            lookback=lookback,
            require_negative_hist=require_negative,
        )
        return result

    def test_golden_cross_surge(self):
        result = self._state(macd=[1.0, 2.0, 3.0], signal=[3.0, 2.0, 1.0], ratio=[1.0, 1.0, 2.0])
        self.assertEqual(result.state.iloc[-1], codes.MACD_VOL_GOLDEN_SURGE)
        self.assertTrue(bool(result.golden_cross.iloc[-1]))
        self.assertEqual(int(result.days_since_golden_cross.iloc[-1]), 0)

    def test_golden_cross_shrink(self):
        result = self._state(macd=[1.0, 2.0, 3.0], signal=[3.0, 2.0, 1.0], ratio=[1.0, 1.0, 0.5])
        self.assertEqual(result.state.iloc[-1], codes.MACD_VOL_GOLDEN_SHRINK)

    def test_dead_cross_surge_and_shrink(self):
        surge = self._state(macd=[3.0, 2.0, 1.0], signal=[1.0, 2.0, 3.0], ratio=[1.0, 1.0, 2.0])
        self.assertEqual(surge.state.iloc[-1], codes.MACD_VOL_DEAD_SURGE)
        shrink = self._state(macd=[3.0, 2.0, 1.0], signal=[1.0, 2.0, 3.0], ratio=[1.0, 1.0, 0.5])
        self.assertEqual(shrink.state.iloc[-1], codes.MACD_VOL_DEAD_SHRINK)

    def test_cross_with_unknown_volume_ratio(self):
        equal = self._state(macd=[1.0, 2.0, 3.0], signal=[3.0, 2.0, 1.0], ratio=[1.0, 1.0, 1.0])
        self.assertEqual(equal.state.iloc[-1], codes.MACD_VOL_GOLDEN_UNKNOWN)
        missing = self._state(macd=[1.0, 2.0, 3.0], signal=[3.0, 2.0, 1.0], ratio=[1.0, 1.0, math.nan])
        self.assertEqual(missing.state.iloc[-1], codes.MACD_VOL_GOLDEN_UNKNOWN)

    def test_no_cross_is_neutral(self):
        result = self._state(macd=[1.0, 1.5, 2.0], signal=[0.5, 0.5, 0.5], ratio=[1.0, 1.0, 1.0])
        self.assertEqual(result.state.iloc[-1], codes.MACD_VOL_NEUTRAL)
        self.assertFalse(bool(result.golden_cross.iloc[-1]))
        self.assertFalse(bool(result.dead_cross.iloc[-1]))

    def test_bullish_divergence(self):
        result = self._state(
            macd=[0.5, 0.4, 0.3, 0.25],
            signal=[0.6, 0.5, 0.4, 0.3],
            ratio=[1.0] * 4,
            close=[5.0, 4.0, 3.0, 2.0],
            hist=[-1.0, -2.0, -3.0, -2.5],
        )
        self.assertEqual(result.state.iloc[-1], codes.MACD_VOL_DIVERGENCE)
        self.assertTrue(bool(result.divergence.iloc[-1]))

    def test_no_divergence_when_hist_also_makes_new_low(self):
        result = self._state(
            macd=[0.5, 0.4, 0.3, 0.25],
            signal=[0.6, 0.5, 0.4, 0.3],
            ratio=[1.0] * 4,
            close=[5.0, 4.0, 3.0, 2.0],
            hist=[-1.0, -2.0, -3.0, -4.0],
        )
        self.assertFalse(bool(result.divergence.iloc[-1]))

    def test_cross_takes_precedence_over_divergence(self):
        result = self._state(
            macd=[0.1, 0.2, 0.5],
            signal=[0.6, 0.4, 0.3],
            ratio=[1.0, 1.0, 2.0],
            close=[5.0, 4.0, 3.0],
            hist=[-1.0, -2.0, -1.5],
            lookback=2,
        )
        self.assertTrue(bool(result.divergence.iloc[-1]))
        self.assertEqual(result.state.iloc[-1], codes.MACD_VOL_GOLDEN_SURGE)

    def test_missing_macd_is_unavailable(self):
        result = self._state(macd=[math.nan] * 3, signal=[math.nan] * 3, ratio=[1.0] * 3)
        self.assertEqual(result.state.iloc[-1], codes.MACD_VOL_UNAVAILABLE)
        self.assertTrue(pd.isna(result.divergence.iloc[-1]))


class EndToEndTests(unittest.TestCase):
    """A股结构场景：高换手 + 急跌必须是恐慌抛售，而不是贪婪。"""

    @staticmethod
    def _crash_frame(size: int = 420) -> pd.DataFrame:
        rally = size - 60
        close = [3000.0 + 900.0 * i / rally for i in range(rally)]
        close += [3900.0 - 900.0 * (i + 1) / 60 for i in range(60)]
        turnover = [1.0 + 0.2 * math.sin(i / 30.0) for i in range(rally)]
        turnover += [3.0 + 0.4 * math.sin(i / 5.0) for i in range(60)]
        volume = [1e8 * (1.0 + 0.2 * math.sin(i / 20.0)) for i in range(rally)]
        volume += [3.5e8 * (1.0 + 0.2 * math.sin(i / 3.0)) for i in range(60)]
        volume[-3:] = [8.0e8, 8.5e8, 9.0e8]  # 末端放量：量比 > 1.5
        up = [1800.0] * rally + [400.0] * 60
        down = [800.0] * rally + [2200.0] * 60
        return pd.DataFrame(
            {
                'date': pd.bdate_range('2023-01-02', periods=size),
                'close': close,
                'volume': volume,
                'amount': [v * c for v, c in zip(volume, close, strict=True)],
                'turnover_rate': turnover,
                'margin_net_buy': [-1e8] * rally + [-6e8] * 60,
                'up_count': up,
                'down_count': down,
                'limit_up_count': [20.0] * rally + [3.0] * 60,
                'limit_down_count': [2.0] * rally + [45.0] * 60,
                'pcr': [0.8] * rally + [1.5] * 60,
                'implied_volatility': [16.0] * rally + [32.0] * 60,
            }
        )

    def test_crash_scenario_is_panic_not_greed(self):
        frame = normalize_frame(self._crash_frame()).frame
        bundle = compute_features(frame)
        scored = scoring.score_all(frame, bundle=bundle).frame
        last = scored.iloc[-1]
        self.assertGreater(float(last['participation_index']), 60.0)
        self.assertLess(float(last['direction_index']), 40.0)
        self.assertEqual(last['state'], codes.STATE_PANIC)
        self.assertGreater(float(last['panic_score']), 60.0)
        self.assertIn(codes.PANIC_TURNOVER_PCT_HIGH, last['panic_reasons'])
        self.assertIn(codes.PANIC_RETURN_20D_LOW, last['panic_reasons'])

    def test_state_column_contains_only_codes(self):
        frame = normalize_frame(self._crash_frame()).frame
        scored = scoring.score_all(frame).frame
        self.assertTrue(set(scored['state'].unique()).issubset(set(codes.STATE_CODES)))
        text = labels.label_series(scored['state'], labels.STATE_LABELS)
        self.assertTrue(text.notna().all())

    def test_warmup_rows_are_marked_unavailable(self):
        frame = normalize_frame(self._crash_frame(400)).frame
        scored = scoring.score_all(frame).frame
        self.assertEqual(scored['state'].iloc[0], codes.STATE_UNAVAILABLE)
        self.assertEqual(scored['data_quality'].iloc[0], codes.DQ_INSUFFICIENT)
        self.assertTrue(math.isnan(float(scored['participation_index'].iloc[0])))
        self.assertFalse(bool(scored['reversal_watch'].iloc[0]))

    def test_index_columns_never_exceed_bounds(self):
        frame = normalize_frame(self._crash_frame()).frame
        scored = scoring.score_all(frame).frame
        for column in ('participation_index', 'direction_index', 'sentiment_index', 'panic_score'):
            valid = scored[column].dropna()
            with self.subTest(column=column):
                self.assertGreaterEqual(float(valid.min()), 0.0)
                self.assertLessEqual(float(valid.max()), 100.0)
        for column in ('panic_coverage', 'panic_confidence', 'participation_coverage', 'direction_coverage'):
            valid = scored[column].dropna()
            with self.subTest(column=column):
                self.assertGreaterEqual(float(valid.min()), 0.0)
                self.assertLessEqual(float(valid.max()), 1.0)

    def test_prefix_invariance_of_scoring(self):
        frame = normalize_frame(self._crash_frame(400)).frame
        full = scoring.score_all(frame).frame
        part = scoring.score_all(frame.iloc[:320]).frame
        numeric = ('participation_index', 'direction_index', 'sentiment_index', 'panic_score', 'volume_shrink_ratio')
        for column in numeric:
            with self.subTest(column=column):
                pd.testing.assert_series_equal(part[column], full[column].iloc[:320], check_names=False)
        for column in ('state', 'macd_volume_state', 'reversal_watch'):
            with self.subTest(column=column):
                pd.testing.assert_series_equal(part[column], full[column].iloc[:320], check_names=False)

    def test_labels_available_for_every_macd_state(self):
        frame = normalize_frame(self._crash_frame()).frame
        scored = scoring.score_all(frame).frame
        codes_in_data = set(scored['macd_volume_state'].unique())
        for code in codes_in_data:
            self.assertIn(code, labels.MACD_VOL_STATE_LABELS)

    def test_nested_warmup_boundaries(self):
        """min_periods=120：分量分位第 121 行起有值，嵌套的综合分位第 241 行起有值。"""
        frame = compute_sentiment(self._crash_frame(560))
        self.assertEqual(int(frame['participation_index'].first_valid_index()), 120)
        self.assertEqual(int(frame['volume_ratio_pct_252'].first_valid_index()), 139)
        self.assertTrue(math.isnan(float(frame['sentiment_pct_252'].iloc[239])))
        self.assertFalse(math.isnan(float(frame['sentiment_pct_252'].iloc[240])))

    def test_index_starts_with_partial_coverage_and_says_so(self):
        """样本刚够时先出指数、但覆盖率与缺失分量必须可见（不假装指标齐全）。"""
        frame = normalize_frame(self._crash_frame(560)).frame
        scored = scoring.score_all(frame).frame
        self.assertAlmostEqual(float(scored['participation_coverage'].iloc[120]), 0.5, places=12)
        self.assertIn('volume_ratio', scored['participation_missing'].iloc[120])
        self.assertAlmostEqual(float(scored['participation_coverage'].iloc[139]), 1.0, places=12)

    def test_missing_optional_columns_still_scores(self):
        raw = self._crash_frame().drop(columns=['pcr', 'implied_volatility', 'margin_net_buy'])
        frame = normalize_frame(raw).frame
        scored = scoring.score_all(frame).frame
        self.assertFalse(math.isnan(float(scored['direction_index'].iloc[-1])))
        self.assertEqual(scored['data_quality'].iloc[-1], codes.DQ_PARTIAL)
        self.assertIn('pcr', scored['direction_missing'].iloc[-1])

    def test_custom_config_changes_behaviour(self):
        frame = normalize_frame(self._crash_frame()).frame
        quality = SentimentQuality(
            components=(
                ComponentSpec(name='turnover', source='turnover_rate', group='participation',
                              scales=(ScaleSpec(252),)),
                ComponentSpec(name='pcr', source='pcr', group='direction', sign='reverse',
                              scales=(ScaleSpec(252),)),
            )
        )
        config = SentimentConfig(
            quality=quality,
            thresholds=SentimentThresholds(min_panic_coverage_for_reversal=0.5),
        )
        scored = scoring.score_all(frame, config).frame
        self.assertIn('turnover_pct_252', compute_features(frame, config).percentiles.columns)
        self.assertFalse(bool(scored['reversal_watch'].isna().any()))


if __name__ == '__main__':
    unittest.main()
