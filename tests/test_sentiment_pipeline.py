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
            'margin_turnover': [2e8 if i < rally else 6e8 for i in range(size)],
            'up_count': [1800.0 if i < rally else 400.0 for i in range(size)],
            'down_count': [800.0 if i < rally else 2200.0 for i in range(size)],
            'limit_up_count': [20.0 if i < rally else 3.0 for i in range(size)],
            'limit_down_count': [2.0 if i < rally else 45.0 for i in range(size)],
            'pcr': [0.8 if i < rally else 1.5 for i in range(size)],
            'implied_volatility': [16.0 if i < rally else 32.0 for i in range(size)],
        }
    )


def position_frame(size: int = 560) -> pd.DataFrame:
    """`crash_frame` + 上证指数收盘（先上行后急跌），用于验证高位/低位分支。

    与 `crash_frame` 分开：既有 fixture 必须保持"无位置信息"，继续验证
    位置未知时的四象限基础码回退路径。
    """
    frame = crash_frame(size)
    rally = max(size - min(60, size), 1)
    sh = [
        3200.0 + 700.0 * i / rally if i < rally else 3900.0 - 700.0 * (i - rally + 1) / min(60, size)
        for i in range(size)
    ]
    return frame.assign(sh_close=sh)


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
            'sh_close',
            'price_ma_60',
            'price_ma_gap',
            'price_position',
            'price_position_label',
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
        self.assertNotIn('margin_turnover_pct_252', columns)
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
        raw = crash_frame(300).drop(columns=['pcr', 'implied_volatility', 'margin_turnover', 'limit_up_count'])
        result = analyze(raw)
        frame = result.frame
        last = frame.iloc[-1]
        self.assertTrue(math.isnan(float(last['pcr_reverse_pct'])))
        self.assertTrue(math.isnan(float(last['margin_turnover_pct_20'])))
        self.assertTrue(math.isnan(float(last['limit_up_down_ratio_pct_252'])))
        self.assertAlmostEqual(float(last['direction_coverage']), 3 / 6, places=12)
        for column in ('pcr', 'implied_volatility', 'margin_turnover', 'limit_up_count'):
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
        # 参与度指数：两融交易额 20 日分位（min_periods=10，不做 T-1 滞后）自第 11 行起、
        # 量比得分（20 日均量）自第 20 行起有值
        self.assertEqual(int(frame['participation_index'].first_valid_index()), 10)
        self.assertEqual(int(frame['volume_ratio_score'].first_valid_index()), 19)
        self.assertEqual(int(frame['sentiment_pct_252'].first_valid_index()), 240)
        self.assertTrue(pd.isna(frame['sentiment_pct_252'].iloc[239]))
        last = frame.iloc[-1]
        # 参与度 =（量比得分 + 两融交易额 20 日分位）/ 2 =（100 + 50）/ 2
        self.assertAlmostEqual(float(last['volume_ratio_score']), 100.0, places=9)
        self.assertAlmostEqual(float(last['margin_turnover_pct_20']), 50.0, places=9)
        self.assertAlmostEqual(float(last['participation_index']), 75.0, places=9)
        self.assertAlmostEqual(float(last['direction_index']), 11.64021164021164, places=9)
        self.assertAlmostEqual(float(last['sentiment_index']), 43.32010582010582, places=9)
        self.assertAlmostEqual(float(last['panic_score']), 80.0, places=9)
        self.assertEqual(last['state'], codes.STATE_PANIC)
        self.assertEqual(last['macd_volume_state'], codes.MACD_VOL_DIVERGENCE)
        self.assertEqual(last['data_quality'], codes.DQ_OK)
        counts = frame['state'].value_counts().to_dict()
        self.assertEqual(counts.get(codes.STATE_UNAVAILABLE), 120)
        # 换成分位口径改成两融交易额后，急跌段切换的前 20 个交易日里两融放量会把
        # 参与度推到高位（旧口径下是净流出更负 -> 分位 0），因此恐慌象限读数变多：
        # 4 -> 16。这是口径变更的预期后果，不是随机波动。
        self.assertEqual(counts.get(codes.STATE_PANIC), 16)

    def test_config_round_trip_changes_nothing(self):
        raw = crash_frame(300)
        base = SentimentConfig()
        restored = SentimentConfig.from_dict(base.to_dict())
        pd.testing.assert_frame_equal(compute_sentiment(raw, base), compute_sentiment(raw, restored))


class PositionStateTests(unittest.TestCase):
    """价格位置：不进指数、只切状态；位置未知时退回四象限基础码。"""

    def test_sh_close_does_not_change_index_math(self):
        """核心不变量：加不加 sh_close，指数/覆盖率/恐慌分逐值不变。"""
        without = crash_frame(400)  # fixture 本身不含 sh_close
        with_position = position_frame(400)
        self.assertNotIn('sh_close', without.columns)
        left = compute_sentiment(without)
        right = compute_sentiment(with_position)
        for column in (
            'participation_index',
            'direction_index',
            'sentiment_index',
            'sentiment_pct_252',
            'panic_score',
            'panic_coverage',
            'panic_confidence',
            'participation_coverage',
            'direction_coverage',
            'data_quality',
        ):
            with self.subTest(column=column):
                pd.testing.assert_series_equal(left[column], right[column], check_names=False)

    def test_price_position_starts_after_the_full_window(self):
        frame = compute_sentiment(position_frame(300))
        self.assertTrue((frame['price_position'].iloc[:59] == codes.POS_UNKNOWN).all())
        self.assertFalse((frame['price_position'].iloc[59:] == codes.POS_UNKNOWN).any())

    def test_state_is_composite_when_position_is_known(self):
        frame = compute_sentiment(position_frame(560))
        last = frame.iloc[-1]
        self.assertEqual(last['price_position'], codes.POS_LOW)  # 急跌段收在均线下
        self.assertEqual(last['state'], codes.STATE_PANIC_LOW)
        self.assertEqual(last['price_position_label'], '低位')
        self.assertEqual(last['state_label'], labels.STATE_LABELS[codes.STATE_PANIC_LOW])

    def test_missing_sh_close_falls_back_to_base_state(self):
        """没有位置信息时，状态与加入位置维度之前完全一致。"""
        raw = crash_frame(560)
        self.assertNotIn('sh_close', raw.columns)
        frame = compute_sentiment(raw)
        self.assertEqual(set(frame['price_position'].unique()), {codes.POS_UNKNOWN})
        last = frame.iloc[-1]
        self.assertEqual(last['state'], codes.STATE_PANIC)
        self.assertEqual(last['state_label'], labels.STATE_LABELS[codes.STATE_PANIC])

    def test_same_quadrant_differs_by_position(self):
        """同一象限在高低位下文案必须不同（本次改动的核心诉求）。

        情绪输入（成交量/换手/涨跌家数）保持不变 -> 象限不变；
        只把上证指数改成单边上行 -> 收盘站上自己的 60 日均线 -> 位置翻到高位。
        """
        low = compute_sentiment(position_frame(560)).iloc[-1]
        raw = position_frame(560)
        raw['sh_close'] = [3000.0 + 5.0 * index for index in range(len(raw))]
        high = compute_sentiment(raw).iloc[-1]
        self.assertEqual(low['price_position'], codes.POS_LOW)
        self.assertEqual(high['price_position'], codes.POS_HIGH)
        # 象限相同（都是放量急跌），只有位置不同
        self.assertEqual(low['state'], codes.STATE_PANIC_LOW)
        self.assertEqual(high['state'], codes.STATE_PANIC_HIGH)
        self.assertNotEqual(low['state_label'], high['state_label'])
        self.assertIn('恐慌', low['state_label'])
        self.assertIn('终止预警', high['state_label'])

    def test_snapshot_exposes_position_fields(self):
        snapshot = sentiment_snapshot(compute_sentiment(position_frame(560)))
        for key in ('price_position', 'price_position_label', 'sh_close', 'price_ma', 'price_ma_gap'):
            with self.subTest(key=key):
                self.assertIn(key, snapshot)
        self.assertEqual(snapshot['price_position'], codes.POS_LOW)
        self.assertLess(float(snapshot['price_ma_gap']), 0.0)

    def test_empty_frame_snapshot_has_position_fields(self):
        snapshot = sentiment_snapshot(pd.DataFrame())
        self.assertEqual(snapshot['price_position'], codes.POS_UNKNOWN)
        self.assertIsNone(snapshot['sh_close'])


class ParticipationCompositionTests(unittest.TestCase):
    """参与度指数只由量比得分与两融交易额 20 日分位决定（两者都无符号）。"""

    def test_participation_is_mean_of_volume_ratio_score_and_margin_percentile(self):
        frame = compute_sentiment(crash_frame(300))
        # 与 compose_index 一致：按可用分量重归一，两个分量都缺才不可用
        expected = pd.DataFrame(
            {'volume_ratio': frame['volume_ratio_score'], 'margin': frame['margin_turnover_pct_20']}
        ).mean(axis=1)
        pd.testing.assert_series_equal(
            frame['participation_index'], expected, check_names=False
        )
        # 覆盖率只有 0（两个分量都缺）、0.5（只有一侧）、1（两侧都有）
        self.assertEqual(set(frame['participation_coverage'].unique()), {0.0, 0.5, 1.0})

    def test_turnover_and_amount_do_not_move_the_participation_index(self):
        base = crash_frame(300)
        bumped = base.copy()
        bumped['turnover_rate'] = bumped['turnover_rate'] * 5.0
        bumped['amount'] = bumped['amount'] * 7.0
        first = compute_sentiment(base)
        second = compute_sentiment(bumped)
        pd.testing.assert_series_equal(
            first['participation_index'], second['participation_index'], check_names=False
        )
        pd.testing.assert_series_equal(
            first['volume_ratio_score'], second['volume_ratio_score'], check_names=False
        )
        # 换手率不再进入指数，但仍照常计算（分位与尺度同步，因此数值不变）
        self.assertTrue(first['turnover_pct_252'].notna().any())

    def test_volume_ratio_score_alone_can_move_the_participation_index(self):
        base = crash_frame(120)
        quiet = base.copy()
        quiet.loc[quiet.index[-1], 'volume'] = 1.0e6
        surging = base.copy()
        surging.loc[surging.index[-1], 'volume'] = 1.0e9
        before = compute_sentiment(quiet).iloc[-1]
        after = compute_sentiment(surging).iloc[-1]
        self.assertLess(float(before['volume_ratio_score']), 50.0)
        self.assertGreater(float(after['volume_ratio_score']), 50.0)
        self.assertGreater(float(after['participation_index']), float(before['participation_index']))
        # 融资净买入不变，参与度的差值正好是量比得分差值的一半
        self.assertAlmostEqual(
            float(after['participation_index']) - float(before['participation_index']),
            0.5 * (float(after['volume_ratio_score']) - float(before['volume_ratio_score'])),
            places=9,
        )


class MarginCompletenessPipelineTests(unittest.TestCase):
    """源站发布不完整的两融观测日：参与度退化为量比得分（权重 100%），覆盖率 0.5。"""

    def _stable_frame(self, size: int = 300) -> pd.DataFrame:
        """成交额与两融交易额都恒定的合成样本（比值恒定），便于单独制造"某一日发布不完整"。"""
        frame = crash_frame(size)
        frame['amount'] = 1.0e12
        frame['margin_turnover'] = 1.7e11
        frame['margin_net_buy'] = 1.0e8
        return frame

    def _frame(self, size: int = 300) -> pd.DataFrame:
        """在恒定比值上把最后一行改残：两融交易额掉到参照的 40%。"""
        frame = self._stable_frame(size)
        frame.loc[frame.index[-1], 'margin_turnover'] = 1.7e11 * 0.40
        return frame

    def test_latest_incomplete_day_degrades_the_participation_index(self):
        frame = self._frame()
        result = compute_sentiment(frame)
        last = result.iloc[-1]
        self.assertTrue(math.isnan(float(last['margin_turnover_pct_20'])))
        self.assertAlmostEqual(
            float(last['participation_index']), float(last['volume_ratio_score']), places=9
        )
        self.assertAlmostEqual(float(last['participation_coverage']), 0.5, places=9)
        self.assertIn('margin_turnover', tuple(last['participation_missing']))
        self.assertEqual(last['data_quality'], codes.DQ_PARTIAL)
        # 倒数第二行不受影响：完整性判定只作用于命中当日
        self.assertFalse(math.isnan(float(result.iloc[-2]['margin_turnover_pct_20'])))

    def test_analyze_reports_the_dropped_observation(self):
        frame = self._frame()
        analysis = analyze(frame)
        self.assertEqual(len(analysis.margin_completeness), 1)
        drop = analysis.margin_completeness[0]
        self.assertEqual(drop.day, pd.Timestamp(frame['date'].iloc[-1]))
        self.assertAlmostEqual(drop.ratio, 1.7e11 * 0.40 / 1.0e12, places=12)
        self.assertAlmostEqual(drop.ratio_reference, 1.7e11 / 1.0e12, places=12)
        payload = json.dumps(analysis.as_dict(), ensure_ascii=False)
        self.assertIn('margin_completeness', payload)
        self.assertIn('ratio_reference', payload)
        self.assertTrue(any('两融完整性校验' in warning for warning in analysis.warnings))

    def test_drop_is_reversible_when_the_source_publishes_the_full_value(self):
        """源站补齐后自动恢复等权口径：不需要人工白名单。"""
        analysis = analyze(self._stable_frame())
        self.assertEqual(analysis.margin_completeness, ())
        self.assertAlmostEqual(float(analysis.frame.iloc[-1]['participation_coverage']), 1.0, places=9)


class ConfigBehaviourTests(unittest.TestCase):
    def test_project_constant_switches_window_without_code_change(self):
        """改 macroboard/config.py 一行常量，pipeline 默认行为随动。"""
        from macroboard import config as project_config

        raw = crash_frame(300)
        before = compute_sentiment(raw)
        self.assertIn('turnover_pct_252', before.columns)
        self.assertNotIn('turnover_pct_21', before.columns)
        with mock.patch.object(project_config, 'SENTIMENT_PERCENTILE_WINDOW', 21):
            fast = compute_sentiment(raw)
        # 窗口旋钮作用于方向指数与不参与指数的输入
        self.assertIn('turnover_pct_21', fast.columns)
        self.assertNotEqual(
            float(fast['direction_index'].iloc[-1]), float(before['direction_index'].iloc[-1])
        )
        # 参与度指数是固定口径，不受窗口旋钮影响
        self.assertEqual(
            fast['participation_index'].first_valid_index(),
            before['participation_index'].first_valid_index(),
        )
        pd.testing.assert_series_equal(
            fast['participation_index'], before['participation_index'], check_names=False
        )

    def test_fast_profile_needs_only_one_month_of_history(self):
        """21 天窗口：一个月数据即可出方向读数；参与度指数与窗口无关。"""
        raw = crash_frame(120)
        fast = compute_sentiment(raw, config_for_profile(PROFILE_FAST, percentile_window=21))
        self.assertGreater(int(fast['direction_index'].notna().sum()), 80)
        default = compute_sentiment(raw)
        self.assertEqual(int(default['direction_index'].notna().sum()), 0)
        # 参与度指数只由量比得分与两融交易额 20 日分位决定，两者与 252 天窗口无关。
        # 120 行输入下第 10 行起两融分位可用，故 110 天有读数（去掉 T-1 滞后后比原先多一天）
        self.assertEqual(int(default['participation_index'].notna().sum()), 110)
        pd.testing.assert_series_equal(
            fast['participation_index'], default['participation_index'], check_names=False
        )

    def test_thresholds_change_state(self):
        raw = crash_frame(560)
        default = compute_sentiment(raw).iloc[-1]
        strict = compute_sentiment(
            raw, SentimentConfig(thresholds=SentimentThresholds(high_participation=90, low_direction=5))
        ).iloc[-1]
        self.assertEqual(default['state'], codes.STATE_PANIC)
        self.assertNotEqual(strict['state'], codes.STATE_PANIC)

    def test_index_missing_policy(self):
        raw = crash_frame(300).drop(columns=['turnover_rate', 'amount', 'volume', 'margin_turnover'])
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
        self.assertEqual(snapshot['state_label'], '恐慌抛售')
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
