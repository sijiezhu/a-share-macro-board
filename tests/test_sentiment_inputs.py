"""情绪模块 P1：输入规范化与字段可用性画像。"""

from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from macroboard.sentiment import codes
from macroboard.sentiment.config import SentimentConfig, SentimentQuality, derive_min_periods
from macroboard.sentiment.inputs import (
    MARGIN_COMPLETENESS_COLUMNS,
    POSITIVE_COLUMNS,
    SUPPORTED_NUMERIC_COLUMNS,
    margin_completeness_warning,
    normalize_frame,
    profile_columns,
)


def base_frame(size: int = 6) -> pd.DataFrame:
    dates = pd.bdate_range('2024-01-01', periods=size)
    return pd.DataFrame(
        {
            'date': dates,
            'close': [100.0 + i for i in range(size)],
            'sh_close': [3000.0 + i for i in range(size)],
            'volume': [1_000_000.0 + i for i in range(size)],
            'amount': [1e9 + i for i in range(size)],
            'turnover_rate': [1.0 + 0.1 * i for i in range(size)],
            'margin_net_buy': [-1e8 + i for i in range(size)],
            'margin_turnover': [5e8 + i for i in range(size)],
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
        frame = base_frame(5).drop(columns=['pcr', 'implied_volatility', 'sh_close'])
        normalized = normalize_frame(frame)
        self.assertEqual(set(normalized.missing_columns), {'pcr', 'implied_volatility', 'sh_close'})
        self.assertTrue(normalized.frame['pcr'].isna().all())
        report = profile_columns(normalized)
        self.assertEqual(report.columns['pcr'].status, codes.STATUS_MISSING)
        self.assertIn('pcr', report.unavailable)

    def test_missing_columns_can_raise(self):
        frame = base_frame(5).drop(columns=['pcr'])
        config = SentimentConfig(quality=SentimentQuality(on_missing_column='raise'))
        with self.assertRaises(ValueError):
            normalize_frame(frame, config)

    def test_sh_close_is_a_positive_column(self):
        """点位必须为正：负值与 0 一律置为缺失（与 close 同一规则）。"""
        frame = base_frame(4)
        frame.loc[0, 'sh_close'] = -1.0
        frame.loc[1, 'sh_close'] = 0.0
        normalized = normalize_frame(frame)
        self.assertTrue(math.isnan(normalized.frame.loc[0, 'sh_close']))
        self.assertTrue(math.isnan(normalized.frame.loc[1, 'sh_close']))
        self.assertFalse(math.isnan(normalized.frame.loc[2, 'sh_close']))
        self.assertIn('sh_close', SUPPORTED_NUMERIC_COLUMNS)
        self.assertIn('sh_close', POSITIVE_COLUMNS)

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

    def test_margin_turnover_must_be_non_negative(self):
        frame = base_frame(4)
        frame['margin_turnover'] = [5e8, -1.0, 0.0, 7e8]
        normalized = normalize_frame(frame)
        self.assertFalse(math.isnan(float(normalized.frame.loc[0, 'margin_turnover'])))
        self.assertTrue(math.isnan(float(normalized.frame.loc[1, 'margin_turnover'])))
        self.assertEqual(float(normalized.frame.loc[2, 'margin_turnover']), 0.0)
        self.assertEqual(normalized.dropped_counts['margin_turnover'], 1)

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


def margin_frame(size: int = 30, *, amount: float = 1.0e12, margin: float = 1.7e11) -> pd.DataFrame:
    """两融完整性校验用的确定性样本：比值恒定的平稳序列（合成样本，非真实行情）。

    成交额与两融交易额都是常数 -> 比值恒定，任何一行的偏离都只来自本行被改动的那一项。
    """
    frame = base_frame(size)
    frame['amount'] = amount
    frame['margin_turnover'] = margin
    frame['margin_net_buy'] = 1.0e9
    return frame


class MarginCompletenessTests(unittest.TestCase):
    """两融完整性校验：源站只发布部分观测的日子，两个两融字段一并按缺失处理。

    触发条件是**两条同时成立**（相对成交额偏低 + 相对自身历史偏低），
    单条成立不剔除——否则「放量而两融未同步放大」的真实交易日会被误伤。
    """

    def test_incomplete_margin_day_is_dropped_pairwise(self):
        frame = margin_frame()
        frame.loc[15, 'margin_turnover'] = 1.7e11 * 0.45
        normalized = normalize_frame(frame)
        for column in MARGIN_COMPLETENESS_COLUMNS:
            with self.subTest(column=column):
                self.assertTrue(math.isnan(normalized.frame.loc[15, column]))
        # 前后各日不受影响，且不做任何填充
        for row in (14, 16):
            with self.subTest(row=row):
                self.assertFalse(math.isnan(normalized.frame.loc[row, 'margin_turnover']))
                self.assertFalse(math.isnan(normalized.frame.loc[row, 'margin_net_buy']))
        self.assertEqual(normalized.dropped_counts['margin_turnover'], 1)
        self.assertEqual(normalized.dropped_counts['margin_net_buy'], 1)
        self.assertEqual(len(normalized.margin_completeness), 1)
        drop = normalized.margin_completeness[0]
        self.assertEqual(drop.day, frame['date'].iloc[15])
        self.assertAlmostEqual(drop.ratio, 1.7e11 * 0.45 / 1.0e12, places=12)
        self.assertAlmostEqual(drop.ratio_reference, 1.7e11 / 1.0e12, places=12)
        self.assertTrue(any('两融完整性校验' in warning for warning in normalized.warnings))
        self.assertTrue(any('2024-01' in warning for warning in normalized.warnings))

    def test_reference_uses_only_history(self):
        """参照只用严格早于当日的观测：把第一行做残，它不因缺少参照而被剔除。"""
        frame = margin_frame()
        frame.loc[0, 'margin_turnover'] = 1.7e11 * 0.10
        normalized = normalize_frame(frame)
        self.assertFalse(math.isnan(normalized.frame.loc[0, 'margin_turnover']))
        self.assertEqual(normalized.margin_completeness, ())
        # 平稳序列里参照就是比值本身，剔除日是第一个有多于 min_periods 个历史观测的异常行
        frame.loc[derive_min_periods(20), 'margin_turnover'] = 1.7e11 * 0.45
        normalized = normalize_frame(frame)
        self.assertEqual(len(normalized.margin_completeness), 1)
        self.assertEqual(normalized.margin_completeness[0].day, frame['date'].iloc[derive_min_periods(20)])

    def test_moderate_dip_is_kept(self):
        """真实库里的最低正常值约 0.83 倍参照：不能被误剔除。"""
        frame = margin_frame()
        frame.loc[15, 'margin_turnover'] = 1.7e11 * 0.83
        normalized = normalize_frame(frame)
        self.assertFalse(math.isnan(normalized.frame.loc[15, 'margin_turnover']))
        self.assertEqual(normalized.margin_completeness, ())

    def test_volume_surge_without_margin_surge_is_kept(self):
        """放量而两融未同步放大是真实形态（恐慌放量）：只满足"相对成交额偏低"，不剔除。"""
        frame = margin_frame()
        frame.loc[15, 'amount'] = 1.0e12 * 2.5
        normalized = normalize_frame(frame)
        self.assertFalse(math.isnan(normalized.frame.loc[15, 'margin_turnover']))
        self.assertEqual(normalized.margin_completeness, ())

    def test_margin_cooling_without_amount_cooling_is_kept(self):
        """只有两融自身降温（占成交额比重正常）也不剔除：那可能是真实的杠杆退潮。"""
        frame = margin_frame()
        frame.loc[15, 'margin_turnover'] = 1.7e11 * 0.55
        frame.loc[15, 'amount'] = 1.0e12 * 0.75
        normalized = normalize_frame(frame)
        self.assertFalse(math.isnan(normalized.frame.loc[15, 'margin_turnover']))
        self.assertEqual(normalized.margin_completeness, ())

    def test_zero_or_missing_amount_is_kept(self):
        """amount 为 0（真实极端缩量）或缺失时不判定：没有分母就不判完整性，也不得产生 inf。"""
        frame = margin_frame()
        frame.loc[15, 'amount'] = 0.0
        frame.loc[15, 'margin_turnover'] = 1.7e11 * 0.10  # 同一行：无分母 -> 不判定
        normalized = normalize_frame(frame)
        self.assertFalse(math.isnan(normalized.frame.loc[15, 'margin_turnover']))
        self.assertEqual(normalized.margin_completeness, ())
        # 0 成交额那一行不得把参照污染成 inf：它后面的异常行仍按正常参照被剔除
        frame.loc[18, 'margin_turnover'] = 1.7e11 * 0.45
        later = normalize_frame(frame)
        self.assertEqual(len(later.margin_completeness), 1)
        self.assertEqual(later.margin_completeness[0].day, frame['date'].iloc[18])
        self.assertAlmostEqual(later.margin_completeness[0].ratio_reference, 1.7e11 / 1.0e12, places=12)
        blank = margin_frame()
        blank['amount'] = np.nan
        blank.loc[15, 'margin_turnover'] = 1.7e11 * 0.10
        normalized = normalize_frame(blank)
        self.assertEqual(normalized.margin_completeness, ())
        self.assertFalse(np.isinf(normalized.frame['margin_turnover'].dropna()).any())

    def test_history_shorter_than_min_periods_is_kept(self):
        """参照样本不足（前 min_periods 行）时不判定，避免用小样本中位数下结论。"""
        frame = margin_frame(derive_min_periods(20))
        frame.loc[derive_min_periods(20) - 1, 'margin_turnover'] = 1.7e11 * 0.10
        normalized = normalize_frame(frame)
        self.assertEqual(normalized.margin_completeness, ())

    def test_threshold_zero_disables_the_check(self):
        frame = margin_frame()
        frame.loc[15, 'margin_turnover'] = 1.7e11 * 0.10
        config = SentimentConfig(quality=SentimentQuality(margin_completeness_threshold=0))
        normalized = normalize_frame(frame, config)
        self.assertFalse(math.isnan(normalized.frame.loc[15, 'margin_turnover']))
        self.assertEqual(normalized.margin_completeness, ())

    def test_prefix_invariance_of_the_check(self):
        """剔除判定不得依赖当日之后的观测（无未来函数）。"""
        frame = margin_frame()
        frame.loc[15, 'margin_turnover'] = 1.7e11 * 0.45
        full = normalize_frame(frame).frame
        for size in (15, 16, 17, len(frame)):
            with self.subTest(size=size):
                partial = normalize_frame(frame.iloc[:size]).frame
                pd.testing.assert_frame_equal(partial, full.iloc[:size].reset_index(drop=True))

    def test_drop_is_not_restored_by_ffill(self):
        """完整性判定是终局：白名单前向填充不得把剔除日补回来（那正是"用前一日顶替"）。"""
        frame = margin_frame()
        frame.loc[15, 'margin_turnover'] = 1.7e11 * 0.45
        config = SentimentConfig(
            quality=SentimentQuality(
                missing_policy='ffill',
                ffill_whitelist=('margin_turnover', 'margin_net_buy'),
            )
        )
        normalized = normalize_frame(frame, config)
        self.assertTrue(math.isnan(normalized.frame.loc[15, 'margin_turnover']))
        self.assertTrue(math.isnan(normalized.frame.loc[15, 'margin_net_buy']))

    def test_profile_reports_invalid_values(self):
        frame = margin_frame()
        frame.loc[15, 'margin_turnover'] = 1.7e11 * 0.45
        report = profile_columns(normalize_frame(frame))
        self.assertEqual(report.columns['margin_turnover'].status, codes.STATUS_INVALID_VALUES)
        self.assertEqual(report.columns['margin_turnover'].dropped_count, 1)

    def test_warning_text_is_bounded_and_names_the_reason(self):
        frame = margin_frame(60)
        for row in (15, 25, 35, 45, 55):
            frame.loc[row, 'margin_turnover'] = 1.7e11 * 0.45
        normalized = normalize_frame(frame)
        self.assertEqual(len(normalized.margin_completeness), 5)
        text = margin_completeness_warning(
            normalized.margin_completeness, window=20, limit=3
        )
        self.assertIn('剔除 5 个观测日', text)
        self.assertIn('等共 5 天', text)
        self.assertEqual(text.count('低于前 20 个交易日中位数'), 3)


if __name__ == '__main__':
    unittest.main()
