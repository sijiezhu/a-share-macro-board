"""情绪模块 P1：输入规范化与字段可用性画像。"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from macroboard.sentiment import codes
from macroboard.sentiment.config import SentimentConfig, SentimentQuality
from macroboard.sentiment.inputs import (
    SUPPORTED_NUMERIC_COLUMNS,
    normalize_frame,
    profile_columns,
)


def base_frame(size: int = 6) -> pd.DataFrame:
    dates = pd.bdate_range('2024-01-01', periods=size)
    return pd.DataFrame(
        {
            'date': dates,
            'close': [100.0 + i for i in range(size)],
            'volume': [1_000_000.0 + i for i in range(size)],
            'amount': [1e9 + i for i in range(size)],
            'turnover_rate': [1.0 + 0.1 * i for i in range(size)],
            'margin_net_buy': [-1e8 + i for i in range(size)],
            'up_count': [2000.0 + i for i in range(size)],
            'down_count': [1000.0 + i for i in range(size)],
            'limit_up_count': [20.0] * size,
            'limit_down_count': [3.0] * size,
            'pcr': [0.9 + 0.01 * i for i in range(size)],
            'implied_volatility': [18.0 + 0.1 * i for i in range(size)],
        }
    )


class NormalizeTests(unittest.TestCase):
    def test_sorts_ascending_and_keeps_unique_dates(self):
        frame = base_frame(4).iloc[[3, 0, 2, 1]].reset_index(drop=True)
        normalized = normalize_frame(frame)
        self.assertTrue(normalized.frame['date'].is_monotonic_increasing)
        self.assertEqual(normalized.frame['date'].nunique(), 4)

    def test_duplicate_dates_keep_last_by_default(self):
        frame = base_frame(3)
        duplicated = pd.concat([frame, frame.iloc[[1]].assign(close=999.0)], ignore_index=True)
        normalized = normalize_frame(duplicated)
        self.assertEqual(len(normalized.frame), 3)
        middle = normalized.frame.loc[normalized.frame['date'] == frame['date'].iloc[1], 'close']
        self.assertAlmostEqual(float(middle.iloc[0]), 999.0, places=12)
        self.assertTrue(any('重复' in warning for warning in normalized.warnings))

    def test_duplicate_dates_can_raise(self):
        frame = base_frame(3)
        duplicated = pd.concat([frame, frame.iloc[[1]]], ignore_index=True)
        config = SentimentConfig(quality=SentimentQuality(dup_policy='raise'))
        with self.assertRaises(ValueError):
            normalize_frame(duplicated, config)

    def test_duplicate_policy_first(self):
        frame = base_frame(3)
        duplicated = pd.concat([frame, frame.iloc[[1]].assign(close=999.0)], ignore_index=True)
        config = SentimentConfig(quality=SentimentQuality(dup_policy='first'))
        normalized = normalize_frame(duplicated, config)
        middle = normalized.frame.loc[normalized.frame['date'] == frame['date'].iloc[1], 'close']
        self.assertAlmostEqual(float(middle.iloc[0]), 101.0, places=12)

    def test_missing_date_column_raises(self):
        with self.assertRaises(ValueError):
            normalize_frame(base_frame(3).drop(columns=['date']))

    def test_unparseable_date_raises(self):
        frame = base_frame(3)
        frame['date'] = frame['date'].astype(object)
        frame.loc[1, 'date'] = 'not-a-date'
        with self.assertRaises(ValueError):
            normalize_frame(frame)

    def test_accepts_iso_string_dates(self):
        frame = base_frame(3)
        frame['date'] = frame['date'].dt.strftime('%Y-%m-%d')
        normalized = normalize_frame(frame)
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(normalized.frame['date']))

    def test_missing_columns_are_marked_by_default(self):
        frame = base_frame(5).drop(columns=['pcr', 'implied_volatility'])
        normalized = normalize_frame(frame)
        self.assertEqual(set(normalized.missing_columns), {'pcr', 'implied_volatility'})
        self.assertTrue(normalized.frame['pcr'].isna().all())
        report = profile_columns(normalized)
        self.assertEqual(report.columns['pcr'].status, codes.STATUS_MISSING)
        self.assertIn('pcr', report.unavailable)

    def test_missing_columns_can_raise(self):
        frame = base_frame(5).drop(columns=['pcr'])
        config = SentimentConfig(quality=SentimentQuality(on_missing_column='raise'))
        with self.assertRaises(ValueError):
            normalize_frame(frame, config)

    def test_invalid_values_become_missing(self):
        frame = base_frame(5)
        frame.loc[0, 'close'] = -1.0
        frame.loc[1, 'volume'] = -5.0
        frame.loc[2, 'pcr'] = 0.0
        frame.loc[3, 'implied_volatility'] = 0.0
        frame.loc[4, 'up_count'] = -3.0
        normalized = normalize_frame(frame)
        for row, column in ((0, 'close'), (1, 'volume'), (2, 'pcr'), (3, 'implied_volatility'), (4, 'up_count')):
            with self.subTest(column=column):
                self.assertTrue(math.isnan(normalized.frame.loc[row, column]))
        self.assertTrue(any('非法值' in warning for warning in normalized.warnings))
        report = profile_columns(normalized)
        self.assertEqual(report.columns['close'].status, codes.STATUS_INVALID_VALUES)

    def test_infinities_become_missing(self):
        frame = base_frame(4)
        frame.loc[1, 'amount'] = np.inf
        frame.loc[2, 'close'] = -np.inf
        normalized = normalize_frame(frame)
        self.assertTrue(math.isnan(normalized.frame.loc[1, 'amount']))
        self.assertTrue(math.isnan(normalized.frame.loc[2, 'close']))

    def test_zero_volume_is_missing_by_default(self):
        frame = base_frame(4)
        frame.loc[1, 'volume'] = 0.0
        normalized = normalize_frame(frame)
        self.assertTrue(math.isnan(normalized.frame.loc[1, 'volume']))

    def test_zero_volume_can_be_kept(self):
        frame = base_frame(4)
        frame.loc[1, 'volume'] = 0.0
        config = SentimentConfig(quality=SentimentQuality(zero_volume_policy='keep'))
        normalized = normalize_frame(frame, config)
        self.assertEqual(normalized.frame.loc[1, 'volume'], 0.0)

    def test_zero_amount_is_kept(self):
        frame = base_frame(4)
        frame.loc[2, 'amount'] = 0.0
        normalized = normalize_frame(frame)
        self.assertEqual(normalized.frame.loc[2, 'amount'], 0.0)

    def test_margin_net_buy_may_be_negative(self):
        frame = base_frame(4)
        frame['margin_net_buy'] = [-5e8, -1e8, 0.0, 3e8]
        normalized = normalize_frame(frame)
        self.assertFalse(bool(normalized.frame['margin_net_buy'].isna().any()))

    def test_no_forward_fill_by_default(self):
        frame = base_frame(5)
        frame.loc[2, 'turnover_rate'] = np.nan
        normalized = normalize_frame(frame)
        self.assertTrue(math.isnan(normalized.frame.loc[2, 'turnover_rate']))

    def test_forward_fill_only_for_whitelist(self):
        frame = base_frame(5)
        frame.loc[2, 'turnover_rate'] = np.nan
        frame.loc[2, 'close'] = np.nan
        config = SentimentConfig(
            quality=SentimentQuality(
                missing_policy='ffill',
                ffill_limit=2,
                ffill_whitelist=('turnover_rate',),
            )
        )
        normalized = normalize_frame(frame, config)
        self.assertAlmostEqual(float(normalized.frame.loc[2, 'turnover_rate']), 1.1, places=12)
        self.assertTrue(math.isnan(normalized.frame.loc[2, 'close']))

    def test_winsorize_is_causal(self):
        size = 40
        frame = base_frame(size)
        frame.loc[30, 'turnover_rate'] = 500.0
        config = SentimentConfig(
            quality=SentimentQuality(winsorize_enabled=True, winsorize_window=10, winsorize_upper_q=0.9)
        )
        normalized = normalize_frame(frame, config)
        prefix = normalize_frame(frame.iloc[:20], config)
        pd.testing.assert_series_equal(
            prefix.frame['turnover_rate'],
            normalized.frame['turnover_rate'].iloc[:20],
            check_names=False,
        )
        self.assertLess(float(normalized.frame.loc[30, 'turnover_rate']), 500.0)

    def test_all_supported_columns_are_present_after_normalisation(self):
        frame = base_frame(3).drop(columns=['limit_up_count'])
        normalized = normalize_frame(frame)
        for column in SUPPORTED_NUMERIC_COLUMNS:
            with self.subTest(column=column):
                self.assertIn(column, normalized.frame.columns)


class ProfileTests(unittest.TestCase):
    def test_statuses(self):
        frame = base_frame(6)
        frame['pcr'] = np.nan
        normalized = normalize_frame(frame)
        report = profile_columns(normalized)
        self.assertEqual(report.columns['pcr'].status, codes.STATUS_ALL_NAN)
        self.assertEqual(report.columns['close'].status, codes.STATUS_OK)
        self.assertEqual(report.columns['close'].valid_count, 6)
        self.assertEqual(report.columns['close'].dropped_count, 0)

    def test_insufficient_history_status(self):
        normalized = normalize_frame(base_frame(6))
        report = profile_columns(normalized, min_samples=10)
        self.assertEqual(report.columns['close'].status, codes.STATUS_INSUFFICIENT_HISTORY)

    def test_first_and_last_valid_dates(self):
        frame = base_frame(6)
        frame.loc[0, 'close'] = np.nan
        frame.loc[5, 'close'] = np.nan
        normalized = normalize_frame(frame)
        status = profile_columns(normalized).columns['close']
        self.assertEqual(status.valid_count, 4)
        self.assertEqual(status.first_valid, frame['date'].iloc[1])
        self.assertEqual(status.last_valid, frame['date'].iloc[4])

    def test_coverage_counts_usable_columns(self):
        frame = base_frame(6).drop(columns=['pcr', 'implied_volatility'])
        normalized = normalize_frame(frame)
        report = profile_columns(normalized)
        self.assertAlmostEqual(report.coverage(('close', 'pcr')), 0.5, places=12)
        self.assertAlmostEqual(report.coverage(('close', 'volume')), 1.0, places=12)
        self.assertAlmostEqual(report.coverage(()), 0.0, places=12)


if __name__ == '__main__':
    unittest.main()
