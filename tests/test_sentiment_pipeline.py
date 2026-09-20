"""情绪模块 P3：公开 API、输出 schema、边界处理与可复现性。

合成数据为构造样本（SYNTHETIC FIXTURE），不是真实行情。
"""

from __future__ import annotations

import json
import math
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from macroboard.sentiment import (
    DEFAULT_OUTPUT_COLUMNS,
    SentimentConfig,
    SentimentQuality,
    analyze,
    codes,
    compute_sentiment,
    labels,
    output_columns,
    sentiment_snapshot,
)
from macroboard.sentiment.config import ComponentSpec, ScaleSpec, SentimentThresholds
from macroboard.sentiment.inputs import normalize_frame, profile_columns
from macroboard.sentiment.registry import (
    PROFILE_FAST,
    config_for_profile,
)


def crash_frame(size: int = 560) -> pd.DataFrame:
    """确定性构造样本：300 天震荡上行 + 60 天急跌（末端放量、跌停潮）。"""
    crash = min(60, size)
    rally = size - crash
    close = [
        3000.0 + 900.0 * i / rally if i < rally else 3900.0 - 900.0 * (i - rally + 1) / crash
        for i in range(size)
    ]
    turnover = [
        1.0 + 0.2 * math.sin(i / 30.0) if i < rally else 3.0 + 0.4 * math.sin((i - rally) / 5.0)
        for i in range(size)
    ]
    volume = [
        1e8 * (1.0 + 0.2 * math.sin(i / 20.0))
        if i < rally
        else 3.5e8 * (1.0 + 0.2 * math.sin((i - rally) / 3.0))
        for i in range(size)
    ]
    if size >= 3:
        volume[-3:] = [8.0e8, 8.5e8, 9.0e8]
    return pd.DataFrame(
        {
            'date': pd.bdate_range('2023-01-02', periods=size),
            'close': close,
            'volume': volume,
            'amount': [v * c for v, c in zip(volume, close, strict=True)],
            'turnover_rate': turnover,
            'margin_net_buy': [-1e8 if i < rally else -6e8 for i in range(size)],
            'up_count': [1800.0 if i < rally else 400.0 for i in range(size)],
            'down_count': [800.0 if i < rally else 2200.0 for i in range(size)],
            'limit_up_count': [20.0 if i < rally else 3.0 for i in range(size)],
            'limit_down_count': [2.0 if i < rally else 45.0 for i in range(size)],
            'pcr': [0.8 if i < rally else 1.5 for i in range(size)],
            'implied_volatility': [16.0 if i < rally else 32.0 for i in range(size)],
        }
    )


class SchemaTests(unittest.TestCase):
    def test_default_schema_matches_design_contract(self):
        frame = compute_sentiment(crash_frame(300))
        self.assertEqual(tuple(frame.columns), DEFAULT_OUTPUT_COLUMNS)
        self.assertEqual(len(DEFAULT_OUTPUT_COLUMNS), len(set(DEFAULT_OUTPUT_COLUMNS)))

    def test_spec_required_columns_are_present(self):
        required = (
            'date',
            'participation_index',
            'direction_index',
            'sentiment_index',
            'sentiment_pct_252',
            'sentiment_momentum_20d',
            'state',
            'panic_score',
            'panic_reasons',
            'reversal_watch',
            'reversal_reasons',
            'macd',
            'macd_signal',
            'macd_hist',
            'macd_hist_norm',
            'volume_ratio',
            'turnover_pct_252',
            'return_20d',
            'up_ratio',
            'limit_up_down_ratio',
            'pcr_reverse_pct',
            'iv_reverse_pct',
            'macd_volume_state',
            'macd_bullish_divergence',
        )
        for column in required:
            with self.subTest(column=column):
                self.assertIn(column, DEFAULT_OUTPUT_COLUMNS)

    def test_schema_follows_configuration(self):
        config = SentimentConfig(
            quality=SentimentQuality(
                components=(
                    ComponentSpec(name='turnover', source='turnover_rate', group='participation',
                                  scales=(ScaleSpec(63), ScaleSpec(252, 0.6))),
                    ComponentSpec(name='pcr', source='pcr', group='direction', sign='reverse',
                                  scales=(ScaleSpec(252),)),
                )
            )
        )
        columns = output_columns(config)
        self.assertIn('turnover_pct_63', columns)
        self.assertIn('turnover_pct_252', columns)
        self.assertIn('turnover_pct', columns)
        self.assertIn('pcr_reverse_pct', columns)
        self.assertNotIn('margin_net_buy_pct_252', columns)
        frame = compute_sentiment(crash_frame(300), config)
        self.assertEqual(tuple(frame.columns), columns)

    def test_label_columns_can_be_disabled(self):
        config = SentimentConfig(quality=SentimentQuality(include_labels=False))
        frame = compute_sentiment(crash_frame(300), config)
        self.assertFalse([c for c in frame.columns if c.endswith('_label')])
        self.assertNotIn('state_label', output_columns(config))

    def test_category_columns_use_codes_only(self):
        frame = compute_sentiment(crash_frame(300))
        self.assertTrue(set(frame['state'].unique()).issubset(set(codes.STATE_CODES)))
        self.assertTrue(set(frame['macd_volume_state'].unique()).issubset(set(codes.MACD_VOL_STATES)))
        self.assertTrue(set(frame['data_quality'].unique()).issubset(set(codes.DATA_QUALITY_CODES)))
        self.assertTrue(frame['state_label'].isin(labels.STATE_LABELS.values()).all())


class EdgeCaseTests(unittest.TestCase):
    def test_empty_frame_returns_empty_schema(self):
        empty = pd.DataFrame({'date': pd.Series(dtype='datetime64[ns]')})
        result = analyze(empty)
        self.assertEqual(len(result.frame), 0)
        self.assertEqual(tuple(result.frame.columns), DEFAULT_OUTPUT_COLUMNS)
        self.assertIsNone(result.as_of)
        snapshot = sentiment_snapshot(result.frame)
        self.assertIsNone(snapshot['as_of'])
        self.assertEqual(snapshot['state'], codes.STATE_UNAVAILABLE)

    def test_single_row_frame_is_unavailable_but_does_not_raise(self):
        frame = crash_frame(1)
        result = analyze(frame)
        self.assertEqual(len(result.frame), 1)
        last = result.frame.iloc[-1]
        self.assertEqual(last['state'], codes.STATE_UNAVAILABLE)
        self.assertEqual(last['data_quality'], codes.DQ_INSUFFICIENT)
        self.assertTrue(math.isnan(float(last['participation_index'])))
        self.assertFalse(bool(last['reversal_watch']))

    def test_missing_date_column_raises(self):
        with self.assertRaises(ValueError):
            analyze(crash_frame(10).drop(columns=['date']))

    def test_unparseable_date_raises(self):
        frame = crash_frame(10)
        frame['date'] = frame['date'].astype(object)
        frame.loc[3, 'date'] = 'oops'
        with self.assertRaises(ValueError):
            analyze(frame)

    def test_unsorted_and_duplicated_dates_are_normalised(self):
        frame = crash_frame(30)
        shuffled = pd.concat([frame.iloc[::-1], frame.iloc[[5]].assign(close=9999.0)], ignore_index=True)
        result = analyze(shuffled)
        self.assertEqual(len(result.frame), 30)
        self.assertTrue(result.frame['date'].is_monotonic_increasing)
        normalized = normalize_frame(shuffled)
        duplicated_row = normalized.frame.loc[normalized.frame['date'] == frame['date'].iloc[5]]
        self.assertAlmostEqual(float(duplicated_row['close'].iloc[0]), 9999.0, places=6)

    def test_duplicate_policy_raise(self):
        frame = crash_frame(30)
        duplicated = pd.concat([frame, frame.iloc[[5]]], ignore_index=True)
        config = SentimentConfig(quality=SentimentQuality(dup_policy='raise'))
        with self.assertRaises(ValueError):
            analyze(duplicated, config)

    def test_missing_columns_are_marked_unavailable(self):
        raw = crash_frame(300).drop(columns=['pcr', 'implied_volatility', 'margin_net_buy', 'limit_up_count'])
        result = analyze(raw)
        frame = result.frame
        last = frame.iloc[-1]
        self.assertTrue(math.isnan(float(last['pcr_reverse_pct'])))
        self.assertTrue(math.isnan(float(last['margin_net_buy_pct_252'])))
        self.assertTrue(math.isnan(float(last['limit_up_down_ratio_pct_252'])))
        self.assertAlmostEqual(float(last['direction_coverage']), 3 / 6, places=12)
        for column in ('pcr', 'implied_volatility', 'margin_net_buy', 'limit_up_count'):
            self.assertIn(column, last['unavailable_columns'])
        for component in ('pcr', 'iv', 'limit_up_down_ratio'):
            self.assertIn(component, last['direction_missing'])
        self.assertTrue(any('缺少列' in warning for warning in result.warnings))

    def test_all_nan_column_is_treated_as_unavailable(self):
        raw = crash_frame(300)
        raw['pcr'] = np.nan
        normalized = normalize_frame(raw)
        self.assertEqual(profile_columns(normalized).columns['pcr'].status, codes.STATUS_ALL_NAN)
        frame = analyze(raw).frame
        self.assertTrue(frame['pcr_reverse_pct'].isna().all())

    def test_invalid_values_do_not_leak_infinities(self):
        raw = crash_frame(300)
        raw.loc[10, 'close'] = -1.0
        raw.loc[11, 'volume'] = 0.0
        raw.loc[12, 'amount'] = np.inf
        raw.loc[13, 'turnover_rate'] = -2.0
        frame = analyze(raw).frame
        numeric = frame.select_dtypes(include=['float64', 'Float64', 'Int64'])
        values = numeric.to_numpy(dtype='float64', na_value=np.nan)
        self.assertFalse(bool(np.isinf(values).any()))

    def test_missing_column_policy_raise(self):
        config = SentimentConfig(quality=SentimentQuality(on_missing_column='raise'))
        with self.assertRaises(ValueError):
            analyze(crash_frame(30).drop(columns=['pcr']), config)


class ReproducibilityTests(unittest.TestCase):
    def test_same_input_same_output(self):
        raw = crash_frame(320)
        first = compute_sentiment(raw)
        second = compute_sentiment(raw)
        pd.testing.assert_frame_equal(first, second)

    def test_row_order_does_not_matter(self):
        raw = crash_frame(300)
        permutation = [*range(len(raw) - 1, -1, -1)]
        shuffled = raw.iloc[permutation].reset_index(drop=True)
        pd.testing.assert_frame_equal(compute_sentiment(raw), compute_sentiment(shuffled))

    def test_prefix_invariance_across_whole_schema(self):
        raw = crash_frame(560)
        full = compute_sentiment(raw)
        # unavailable_columns 是"本次输入"的运行级标注（整列缺失才出现），不是逐日量
        columns = [c for c in full.columns if c != 'unavailable_columns']
        for prefix in (1, 100, 252, 400):
            part = compute_sentiment(raw.iloc[:prefix])
            with self.subTest(prefix=prefix):
                pd.testing.assert_frame_equal(
                    part[columns], full.iloc[:prefix][columns].reset_index(drop=True)
                )

    def test_pinned_values_for_synthetic_scenario(self):
        """固定输入 -> 固定输出（黄金值）。改动算法必须显式更新这些期望值。"""
        result = analyze(crash_frame(560))
        frame = result.frame
        self.assertEqual(result.as_of.strftime('%Y-%m-%d'), '2025-02-21')
        self.assertEqual(int(frame['participation_index'].first_valid_index()), 120)
        self.assertEqual(int(frame['sentiment_pct_252'].first_valid_index()), 240)
        self.assertTrue(pd.isna(frame['sentiment_pct_252'].iloc[239]))
        last = frame.iloc[-1]
        self.assertAlmostEqual(float(last['participation_index']), 72.81746031746032, places=9)
        self.assertAlmostEqual(float(last['direction_index']), 11.64021164021164, places=9)
        self.assertAlmostEqual(float(last['sentiment_index']), 42.228835978835974, places=9)
        self.assertAlmostEqual(float(last['panic_score']), 80.0, places=9)
        self.assertEqual(last['state'], codes.STATE_PANIC)
        self.assertEqual(last['macd_volume_state'], codes.MACD_VOL_DIVERGENCE)
        self.assertEqual(last['data_quality'], codes.DQ_OK)
        counts = frame['state'].value_counts().to_dict()
        self.assertEqual(counts.get(codes.STATE_UNAVAILABLE), 120)
        self.assertEqual(counts.get(codes.STATE_PANIC), 153)

    def test_config_round_trip_changes_nothing(self):
        raw = crash_frame(300)
        base = SentimentConfig()
        restored = SentimentConfig.from_dict(base.to_dict())
        pd.testing.assert_frame_equal(compute_sentiment(raw, base), compute_sentiment(raw, restored))


class ConfigBehaviourTests(unittest.TestCase):
    def test_project_constant_switches_window_without_code_change(self):
        """改 macroboard/config.py 一行常量，pipeline 默认行为随动。"""
        from macroboard import config as project_config

        raw = crash_frame(120)
        self.assertEqual(int(compute_sentiment(raw)['participation_index'].notna().sum()), 0)
        with mock.patch.object(project_config, 'SENTIMENT_PERCENTILE_WINDOW', 21):
            fast = compute_sentiment(raw)
        self.assertGreater(int(fast['participation_index'].notna().sum()), 80)
        # min_periods=10：样本刚够一半分量（换手率+成交额）就先出指数，覆盖率会标 0.5
        self.assertEqual(int(fast['participation_index'].first_valid_index()), 10)
        self.assertAlmostEqual(float(fast['participation_coverage'].iloc[10]), 0.5, places=12)

    def test_fast_profile_needs_only_one_month_of_history(self):
        """21 天窗口：一个月数据即可出数；默认 252 窗口在同一输入下毫无输出。"""
        raw = crash_frame(120)
        fast = compute_sentiment(raw, config_for_profile(PROFILE_FAST, percentile_window=21))
        self.assertGreater(int(fast['participation_index'].notna().sum()), 80)
        default = compute_sentiment(raw)
        self.assertEqual(int(default['participation_index'].notna().sum()), 0)

    def test_thresholds_change_state(self):
        raw = crash_frame(560)
        default = compute_sentiment(raw).iloc[-1]
        strict = compute_sentiment(
            raw, SentimentConfig(thresholds=SentimentThresholds(high_participation=90, low_direction=5))
        ).iloc[-1]
        self.assertEqual(default['state'], codes.STATE_PANIC)
        self.assertNotEqual(strict['state'], codes.STATE_PANIC)

    def test_index_missing_policy(self):
        raw = crash_frame(300).drop(columns=['turnover_rate', 'amount', 'volume', 'margin_net_buy'])
        strict = compute_sentiment(raw, SentimentConfig()).iloc[-1]
        lenient = compute_sentiment(
            raw, SentimentConfig(quality=SentimentQuality(index_missing='renormalize'))
        ).iloc[-1]
        self.assertTrue(math.isnan(float(strict['participation_index'])))
        self.assertEqual(strict['state'], codes.STATE_UNAVAILABLE)
        self.assertFalse(math.isnan(float(lenient['sentiment_index'])))
        self.assertAlmostEqual(
            float(lenient['sentiment_index']), float(lenient['direction_index']), places=9
        )

    def test_invalid_config_raises(self):
        with self.assertRaises(ValueError):
            analyze(crash_frame(30), SentimentConfig(
                thresholds=SentimentThresholds(low_participation=80, high_participation=60)
            ))

    def test_bounds_hold_on_full_output(self):
        frame = compute_sentiment(crash_frame(560))
        for column in ('participation_index', 'direction_index', 'sentiment_index', 'panic_score'):
            valid = frame[column].dropna()
            with self.subTest(column=column):
                self.assertGreaterEqual(float(valid.min()), 0.0)
                self.assertLessEqual(float(valid.max()), 100.0)
        for column in ('turnover_pct_252', 'return_20d_pct_252', 'pcr_reverse_pct', 'iv_reverse_pct'):
            valid = frame[column].dropna()
            with self.subTest(column=column):
                self.assertGreaterEqual(float(valid.min()), 0.0)
                self.assertLessEqual(float(valid.max()), 100.0)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_json_friendly_and_has_labels(self):
        result = analyze(crash_frame(560))
        snapshot = sentiment_snapshot(result.frame, result.config)
        self.assertEqual(snapshot['as_of'], '2025-02-21')
        self.assertEqual(snapshot['state'], codes.STATE_PANIC)
        self.assertEqual(snapshot['state_label'], '恐慌抛售（放量急跌型冰点）')
        self.assertIn('component_scales', snapshot)
        self.assertEqual(snapshot['percentile_method'], 'midrank_exclusive')
        json.dumps(snapshot, ensure_ascii=False)

    def test_snapshot_never_contains_advice_fields(self):
        snapshot = sentiment_snapshot(compute_sentiment(crash_frame(560)))
        forbidden = ('suggestion', 'target_price', 'position', 'signal_buy')
        for key in snapshot:
            with self.subTest(key=key):
                self.assertNotIn(key, forbidden)

    def test_result_as_dict_is_serialisable(self):
        result = analyze(crash_frame(300))
        payload = result.as_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn('midrank_exclusive', text)
        self.assertEqual(payload['rows'], 300)
        self.assertEqual(payload['as_of'], '2024-02-23')

    def test_snapshot_of_empty_frame(self):
        snapshot = sentiment_snapshot(pd.DataFrame())
        self.assertEqual(snapshot['rows'], 0)
        self.assertEqual(snapshot['state'], codes.STATE_UNAVAILABLE)


if __name__ == '__main__':
    unittest.main()
