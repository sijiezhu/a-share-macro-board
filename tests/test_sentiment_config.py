"""情绪模块 P1：配置对象、min_periods 推导与校验契约。"""

from __future__ import annotations

import unittest
from unittest import mock

from macroboard.config import SERIES
from macroboard.sentiment.config import (
    ComponentSpec,
    ScaleSpec,
    SentimentConfig,
    SentimentQuality,
    SentimentThresholds,
    SentimentWindows,
    derive_min_periods,
    resolve_min_periods,
    validate_config,
)
from macroboard.sentiment.registry import (
    DEFAULT_COMPONENTS,
    PROFILE_FAST,
    PROFILE_FAST_MIXED,
    PROFILE_MULTISCALE_SUGGESTED,
    PROFILE_PRESETS,
    VOLUME_RATIO_LEVEL_ANCHORS,
    WINDOW_PRESETS,
    config_for_profile,
    config_for_window,
    default_config,
    resolve_window,
    with_percentile_window,
)


class DeriveMinPeriodsTests(unittest.TestCase):
    def test_doc_table(self):
        self.assertEqual(derive_min_periods(63), 30)
        self.assertEqual(derive_min_periods(252), 120)
        self.assertEqual(derive_min_periods(504), 240)

    def test_clamped_to_window_range(self):
        self.assertEqual(derive_min_periods(2), 2)
        with self.assertRaises(ValueError):
            derive_min_periods(1)

    def test_default_min_periods_follows_ratio_for_both_methods(self):
        scale = ScaleSpec(252)
        self.assertEqual(resolve_min_periods(scale, 'midrank_exclusive', 120 / 252), 120)
        self.assertEqual(resolve_min_periods(scale, 'rank_inclusive', 120 / 252), 120)

    def test_full_window_is_opt_in(self):
        self.assertEqual(
            resolve_min_periods(ScaleSpec(252, min_periods=252), 'midrank_exclusive', 120 / 252), 252
        )

    def test_mode_a_uses_ratio(self):
        self.assertEqual(resolve_min_periods(ScaleSpec(252), 'rank_inclusive', 120 / 252), 120)
        self.assertEqual(resolve_min_periods(ScaleSpec(63), 'rank_inclusive', 120 / 252), 30)

    def test_explicit_min_periods_wins(self):
        scale = ScaleSpec(252, min_periods=40)
        self.assertEqual(resolve_min_periods(scale, 'midrank_exclusive', 120 / 252), 40)
        self.assertEqual(resolve_min_periods(scale, 'rank_inclusive', 120 / 252), 40)


class DefaultConfigTests(unittest.TestCase):
    def test_default_is_valid_and_matches_design(self):
        config = SentimentConfig()
        validate_config(config)
        self.assertEqual(config.quality.percentile_method, 'midrank_exclusive')
        self.assertEqual(config.windows.macd_fast, 12)
        self.assertEqual(config.windows.macd_slow, 26)
        self.assertEqual(config.windows.macd_signal, 9)
        self.assertEqual(config.windows.volume_ma, 20)
        self.assertEqual(config.windows.price_ma_window, 60)
        self.assertEqual(config.windows.momentum['sentiment'], 20)
        self.assertEqual(config.thresholds.high_participation, 60.0)
        self.assertEqual(config.thresholds.panic_return_20d, -0.08)
        self.assertEqual(config.thresholds.min_panic_coverage_for_reversal, 0.60)

    def test_component_groups(self):
        participation = [c for c in DEFAULT_COMPONENTS if c.group == 'participation']
        direction = [c for c in DEFAULT_COMPONENTS if c.group == 'direction']
        self.assertEqual(len(participation), 4)
        self.assertEqual(len(direction), 6)
        self.assertEqual([c.name for c in participation if c.sign == 'reverse'], [])
        self.assertEqual(sorted(c.name for c in direction if c.sign == 'reverse'), ['iv', 'pcr'])

    def test_participation_index_is_level_map_plus_twenty_day_percentile(self):
        """参与度指数只由量比（阈值映射）与两融交易额 20 日分位决定。"""
        by_name = {c.name: c for c in DEFAULT_COMPONENTS}
        index_components = [c for c in DEFAULT_COMPONENTS if c.group == 'participation' and c.in_index]
        self.assertEqual([c.name for c in index_components], ['volume_ratio', 'margin_turnover'])
        self.assertEqual([c.weight for c in index_components], [1.0, 1.0])
        volume_ratio = by_name['volume_ratio']
        self.assertEqual(volume_ratio.transform, 'level_map')
        self.assertEqual(volume_ratio.level_map, ((0.5, 0.0), (1.0, 50.0), (2.0, 100.0)))
        self.assertEqual(volume_ratio.scales[0].window, 20)
        margin = by_name['margin_turnover']
        self.assertEqual(margin.transform, 'percentile')
        self.assertEqual(margin.source, 'margin_turnover')
        self.assertEqual(margin.scales[0].window, 20)
        self.assertEqual(margin.availability_lag, 0)

    def test_margin_net_buy_stays_collected_but_out_of_index(self):
        """净买入有符号，会与方向指数同源，因此只采集展示、不进指数。"""
        by_name = {c.name: c for c in DEFAULT_COMPONENTS}
        self.assertNotIn('margin_net_buy', by_name)
        self.assertIn('margin_net_buy', SERIES)
        self.assertIn('margin_turnover', SERIES)

    def test_turnover_and_amount_stay_collected_but_out_of_index(self):
        by_name = {c.name: c for c in DEFAULT_COMPONENTS}
        for name in ('turnover', 'amount'):
            with self.subTest(component=name):
                self.assertFalse(by_name[name].in_index)
                self.assertEqual(by_name[name].transform, 'percentile')
        self.assertTrue(by_name['turnover'].in_index is False)

    def test_margin_turnover_is_not_lagged(self):
        """两融 T+1 公布，但不用前一日的值顶替：当日缺就按缺失处理。

        取 T-1 会把昨天的活跃度记到今天头上，读数与「数据日期」不符；
        当日缺失时 `compose_index` 按可用分量重归一，参与度退化为量比得分（权重 100%）。
        """
        by_name = {c.name: c for c in DEFAULT_COMPONENTS}
        self.assertEqual(by_name['margin_turnover'].availability_lag, 0)
        self.assertEqual(by_name['margin_turnover'].scales[0].window, 20)
        self.assertEqual(by_name['turnover'].availability_lag, 0)

    def test_multiscale_profile_is_valid(self):
        config = SentimentConfig(quality=SentimentQuality(components=PROFILE_MULTISCALE_SUGGESTED))
        validate_config(config)
        by_name = {c.name: c for c in PROFILE_MULTISCALE_SUGGESTED}
        self.assertEqual([s.window for s in by_name['turnover'].scales], [63, 252])
        self.assertEqual(by_name['pcr'].scales[0].window, 252)

    def test_every_profile_keeps_the_participation_index_fixed(self):
        """任何画像下参与度指数都是「量比阈值映射 + 两融交易额 20 日分位」。"""
        for name, components in PROFILE_PRESETS.items():
            with self.subTest(profile=name):
                index_components = [
                    c for c in components if c.group == 'participation' and c.in_index
                ]
                self.assertEqual([c.name for c in index_components], ['volume_ratio', 'margin_turnover'])
                self.assertEqual(index_components[0].transform, 'level_map')
                self.assertEqual(index_components[0].level_map, VOLUME_RATIO_LEVEL_ANCHORS)
                self.assertEqual(index_components[1].scales[0].window, 20)
                self.assertEqual(index_components[1].availability_lag, 0)


class WindowProfileTests(unittest.TestCase):
    """窗口画像：21 天（约 1 个月）与 21/63 混合，以及窗口敏感性工具。"""

    def test_fast_profile_uses_one_month_window(self):
        config = config_for_profile(PROFILE_FAST, percentile_window=21)
        validate_config(config)
        windows = {c.name: c.scales[0].window for c in config.quality.components}
        self.assertEqual(windows['return_20d'], 21)
        self.assertEqual(windows['turnover'], 21)
        # 参与度指数是固定口径：量比 20 日均量 + 两融交易额 20 日分位
        self.assertEqual(windows['volume_ratio'], 20)
        self.assertEqual(windows['margin_turnover'], 20)
        self.assertEqual(config.windows.sentiment_percentile_scales[0].window, 21)

    def test_fast_profile_min_periods_is_ten(self):
        config = config_for_profile(PROFILE_FAST, percentile_window=21)
        scale = config.quality.components[0].scales[0]
        self.assertEqual(resolve_min_periods(scale, 'midrank_exclusive', config.windows.min_periods_ratio), 10)

    def test_mixed_profile_keeps_smoothed_components_longer(self):
        config = config_for_profile(PROFILE_FAST_MIXED, percentile_window=21)
        validate_config(config)
        by_name = {c.name: c for c in config.quality.components}
        self.assertEqual(by_name['turnover'].scales[0].window, 21)
        for name in ('return_20d', 'macd_hist_norm'):
            with self.subTest(component=name):
                self.assertEqual(by_name[name].scales[0].window, 63)

    def test_with_percentile_window_rewrites_every_scale_except_participation_index(self):
        rewritten = with_percentile_window(DEFAULT_COMPONENTS, 21)
        windows = {c.name: {s.window for s in c.scales} for c in rewritten}
        self.assertEqual(windows['turnover'], {21})
        self.assertEqual(windows['return_20d'], {21})
        self.assertEqual(windows['volume_ratio'], {20})
        self.assertEqual(windows['margin_turnover'], {20})
        self.assertEqual(len(rewritten), len(DEFAULT_COMPONENTS))

    def test_config_for_profile_defaults_to_largest_window(self):
        config = config_for_profile(DEFAULT_COMPONENTS)
        self.assertEqual(config.windows.sentiment_percentile_scales[0].window, 252)
        validate_config(config)


class WindowKnobTests(unittest.TestCase):
    """窗口旋钮：一行换窗口，默认仍是 252。"""

    def test_presets_are_documented_numbers(self):
        self.assertEqual(WINDOW_PRESETS['1个月'], 21)
        self.assertEqual(WINDOW_PRESETS['1个季度'], 63)
        self.assertEqual(WINDOW_PRESETS['1年'], 252)

    def test_resolve_window_accepts_name_or_number(self):
        self.assertEqual(resolve_window('半年'), 126)
        self.assertEqual(resolve_window(21), 21)
        with self.assertRaises(ValueError):
            resolve_window('两年')
        with self.assertRaises(ValueError):
            resolve_window(1)

    def test_price_ma_window_is_not_tied_to_the_window_knob(self):
        """位置口径固定 60 日：换分位窗口不应改变"收盘 vs 60 日均线"这条定义。"""
        for window in ('1个季度', '半年', '1年'):
            with self.subTest(window=window):
                self.assertEqual(config_for_window(window).windows.price_ma_window, 60)

    def test_price_ma_window_round_trips(self):
        self.assertEqual(SentimentWindows().price_ma_window, 60)
        config = SentimentConfig().replace(windows={'price_ma_window': 120})
        validate_config(config)
        self.assertEqual(config.windows.price_ma_window, 120)
        restored = SentimentConfig.from_dict(config.to_dict())
        self.assertEqual(restored.windows.price_ma_window, 120)

    def test_config_for_window_scales_components_and_composite(self):
        config = config_for_window('1个季度')
        validate_config(config)
        self.assertEqual(config.windows.sentiment_percentile_scales[0].window, 63)
        windows = {c.name: c.scales[0].window for c in config.quality.components}
        self.assertEqual(windows['turnover'], 63)
        self.assertEqual(windows['return_20d'], 63)
        # 参与度指数的两个分量不随窗口旋钮变化
        self.assertEqual(windows['volume_ratio'], 20)
        self.assertEqual(windows['margin_turnover'], 20)
        self.assertEqual(len(config.quality.components), len(DEFAULT_COMPONENTS))

    def test_mixed_profile_shields_smoothed_components(self):
        config = config_for_window(21, profile='mixed')
        validate_config(config)
        windows = {c.name: c.scales[0].window for c in config.quality.components}
        self.assertEqual(windows['turnover'], 21)
        self.assertEqual(windows['pcr'], 21)
        for name in ('return_20d', 'macd_hist_norm'):
            with self.subTest(component=name):
                self.assertEqual(windows[name], 63)
        self.assertEqual(windows['volume_ratio'], 20)
        self.assertEqual(windows['margin_turnover'], 20)
        self.assertEqual(config.windows.sentiment_percentile_scales[0].window, 21)

    def test_multiscale_profile_keeps_its_own_scales(self):
        config = config_for_window(21, profile='multiscale')
        validate_config(config)
        turnover = next(c for c in config.quality.components if c.name == 'turnover')
        self.assertEqual([s.window for s in turnover.scales], [63, 252])
        self.assertEqual(config.windows.sentiment_percentile_scales[0].window, 21)

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            config_for_window(21, profile='turbo')

    def test_default_config_follows_project_constant(self):
        config = default_config()
        validate_config(config)
        self.assertEqual(config.windows.sentiment_percentile_scales[0].window, 252)

    def test_project_constant_can_switch_window_without_code_change(self):
        from macroboard import config as project_config

        with mock.patch.object(project_config, 'SENTIMENT_PERCENTILE_WINDOW', 63), mock.patch.object(
            project_config, 'SENTIMENT_WINDOW_PROFILE', 'mixed'
        ):
            config = default_config()
        self.assertEqual(config.windows.sentiment_percentile_scales[0].window, 63)
        windows = {c.name: c.scales[0].window for c in config.quality.components}
        self.assertEqual(windows['return_20d'], 63)


class ValidationTests(unittest.TestCase):
    def _config(self, **quality: object) -> SentimentConfig:
        return SentimentConfig(quality=SentimentQuality(**quality))  # type: ignore[arg-type]

    def test_min_periods_above_window_raises(self):
        component = ComponentSpec(
            name='turnover',
            source='turnover_rate',
            group='participation',
            scales=(ScaleSpec(63, min_periods=100),),
        )
        with self.assertRaises(ValueError) as ctx:
            validate_config(self._config(components=(component,)))
        self.assertIn('min_periods', str(ctx.exception))

    def test_threshold_order_is_enforced(self):
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(thresholds=SentimentThresholds(low_participation=70, high_participation=60)))
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(thresholds=SentimentThresholds(low_direction=60, high_direction=60)))

    def test_duplicate_component_names_raise(self):
        component = ComponentSpec(name='turnover', source='turnover_rate', group='participation')
        with self.assertRaises(ValueError) as ctx:
            validate_config(self._config(components=(component, component)))
        self.assertIn('重复', str(ctx.exception))

    def test_duplicate_scale_windows_raise(self):
        component = ComponentSpec(
            name='turnover',
            source='turnover_rate',
            group='participation',
            scales=(ScaleSpec(63), ScaleSpec(63)),
        )
        with self.assertRaises(ValueError):
            validate_config(self._config(components=(component,)))

    def test_zero_scale_weight_raises(self):
        component = ComponentSpec(
            name='turnover',
            source='turnover_rate',
            group='participation',
            scales=(ScaleSpec(63, 0.0), ScaleSpec(252, 0.0)),
        )
        with self.assertRaises(ValueError):
            validate_config(self._config(components=(component,)))

    def test_empty_group_raises(self):
        component = ComponentSpec(name='turnover', source='turnover_rate', group='participation')
        with self.assertRaises(ValueError) as ctx:
            validate_config(self._config(components=(component,)))
        self.assertIn('direction', str(ctx.exception))

    def test_bad_choices_raise(self):
        for field in ('index_missing', 'price_gap_policy', 'zero_volume_policy', 'missing_policy', 'dup_policy'):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_config(self._config(**{field: 'nonsense'}))
        with self.assertRaises(ValueError):
            validate_config(self._config(percentile_method='nonsense'))

    def test_macd_and_window_guards(self):
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(windows=SentimentWindows(macd_fast=26, macd_slow=12)))
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(windows=SentimentWindows(volume_ma=1)))
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(windows=SentimentWindows(price_ma_window=1)))
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(windows=SentimentWindows(return_horizons=(20, 20))))
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(windows=SentimentWindows(momentum={'sentiment': 20})))

    def test_min_periods_ratio_bounds(self):
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(windows=SentimentWindows(min_periods_ratio=0.0)))
        with self.assertRaises(ValueError):
            validate_config(SentimentConfig(windows=SentimentWindows(min_periods_ratio=1.5)))

    def test_winsorize_bounds(self):
        with self.assertRaises(ValueError):
            validate_config(self._config(winsorize_lower_q=0.5, winsorize_upper_q=0.4))

    def test_level_map_anchors_are_validated(self):
        direction = ComponentSpec(name='pcr', source='pcr', group='direction')

        def component(**overrides: object) -> ComponentSpec:
            base: dict[str, object] = {
                'name': 'volume_ratio',
                'source': 'volume_ratio',
                'group': 'participation',
                'scales': (ScaleSpec(20),),
                'transform': 'level_map',
                'level_map': ((0.5, 0.0), (1.0, 50.0)),
            }
            base.update(overrides)
            return ComponentSpec(**base)  # type: ignore[arg-type]

        def config_for(**overrides: object) -> SentimentConfig:
            return self._config(components=(component(**overrides), direction))

        validate_config(config_for())
        with self.assertRaises(ValueError) as ctx:
            validate_config(config_for(level_map=()))
        self.assertIn('至少 2 个锚点', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate_config(config_for(level_map=((1.0, 0.0), (0.5, 100.0))))
        self.assertIn('严格递增', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate_config(config_for(level_map=((0.5, 0.0), (1.0, 120.0))))
        self.assertIn('0—100', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate_config(
                self._config(
                    components=(
                        ComponentSpec(
                            name='turnover',
                            source='turnover_rate',
                            group='participation',
                            level_map=((1.0, 50.0),),
                        ),
                        direction,
                    )
                )
            )
        self.assertIn('level_map', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            validate_config(config_for(transform='percentile'))
        self.assertIn('transform', str(ctx.exception))

    def test_in_index_weight_is_what_counts(self):
        """只有 in_index=True 的分量计入组权重：全是不入指数的分量应判非法。"""
        component = ComponentSpec(
            name='turnover',
            source='turnover_rate',
            group='participation',
            in_index=False,
        )
        with self.assertRaises(ValueError) as ctx:
            validate_config(self._config(components=(component,)))
        self.assertIn('参与指数合成', str(ctx.exception))


class SerializationTests(unittest.TestCase):
    def test_round_trip_preserves_config(self):
        original = SentimentConfig(quality=SentimentQuality(components=PROFILE_MULTISCALE_SUGGESTED))
        restored = SentimentConfig.from_dict(original.to_dict())
        self.assertEqual(restored.to_dict(), original.to_dict())
        validate_config(restored)

    def test_from_dict_accepts_plain_dict(self):
        config = SentimentConfig.from_dict(
            {
                'windows': {'macd_fast': 8, 'macd_slow': 21, 'return_horizons': [20, 60]},
                'thresholds': {'high_participation': 70, 'low_participation': 30},
                'quality': {
                    'percentile_method': 'rank_inclusive',
                    'components': [
                        {'name': 'turnover', 'source': 'turnover_rate', 'group': 'participation',
                         'scales': [{'window': 252, 'weight': 1.0}]},
                        {'name': 'pcr', 'source': 'pcr', 'group': 'direction', 'sign': 'reverse',
                         'scales': [{'window': 252}]},
                    ],
                },
            }
        )
        self.assertEqual(config.windows.macd_fast, 8)
        self.assertEqual(config.windows.return_horizons, (20, 60))
        self.assertEqual(config.thresholds.high_participation, 70.0)
        self.assertEqual(config.quality.percentile_method, 'rank_inclusive')
        self.assertEqual(len(config.quality.components), 2)
        validate_config(config)

    def test_from_dict_rejects_unknown_keys(self):
        with self.assertRaises(ValueError):
            SentimentConfig.from_dict({'unknown': {}})
        with self.assertRaises(ValueError):
            SentimentConfig.from_dict({'windows': {'nope': 1}})
        with self.assertRaises(ValueError):
            SentimentConfig.from_dict(
                {'quality': {'components': [{'name': 'a', 'source': 'b', 'group': 'participation', 'zzz': 1}]}}
            )

    def test_replace_section_keeps_other_fields(self):
        base = SentimentConfig(thresholds=SentimentThresholds(panic_volume_ratio=2.0))
        updated = base.replace(thresholds={'high_participation': 75})
        self.assertEqual(updated.thresholds.high_participation, 75.0)
        self.assertEqual(updated.thresholds.panic_volume_ratio, 2.0)
        self.assertEqual(updated.windows.volume_ma, base.windows.volume_ma)
        validate_config(updated)

    def test_replace_rejects_unknown_section(self):
        with self.assertRaises(ValueError):
            SentimentConfig().replace(unknown={'a': 1})

    def test_replace_windows_rebuilds_scales(self):
        updated = SentimentConfig().replace(
            windows={'sentiment_percentile_scales': [{'window': 126, 'min_periods': 60}]}
        )
        scale = updated.windows.sentiment_percentile_scales[0]
        self.assertEqual(scale.window, 126)
        self.assertEqual(scale.min_periods, 60)
        validate_config(updated)

    def test_margin_completeness_defaults_and_round_trip(self):
        config = SentimentConfig()
        self.assertEqual(config.quality.margin_completeness_window, 20)
        self.assertEqual(config.quality.margin_completeness_threshold, 0.7)
        raw = config.to_dict()['quality']
        self.assertIn('margin_completeness_window', raw)
        self.assertIn('margin_completeness_threshold', raw)
        restored = SentimentConfig.from_dict(config.to_dict())
        self.assertEqual(restored.to_dict(), config.to_dict())

    def test_margin_completeness_replace_and_coercion(self):
        updated = SentimentConfig().replace(
            quality={'margin_completeness_window': '63', 'margin_completeness_threshold': '0.5'}
        )
        self.assertEqual(updated.quality.margin_completeness_window, 63)
        self.assertAlmostEqual(updated.quality.margin_completeness_threshold, 0.5, places=12)
        # 参与度是固定口径，窗口旋钮不得改动完整性校验的两个字段
        for knob in (
            config_for_window(63),
            config_for_window('1个季度'),
            config_for_profile(PROFILE_FAST, percentile_window=21),
        ):
            with self.subTest(config=knob.quality.margin_completeness_window):
                self.assertEqual(knob.quality.margin_completeness_window, 20)
                self.assertEqual(knob.quality.margin_completeness_threshold, 0.7)

    def test_margin_completeness_rejects_invalid_values(self):
        for kwargs in (
            {'margin_completeness_window': 1},
            {'margin_completeness_window': 0},
            {'margin_completeness_threshold': -0.1},
            {'margin_completeness_threshold': 1.5},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    validate_config(SentimentConfig(quality=SentimentQuality(**kwargs)))  # type: ignore[arg-type]
        # 0（关闭）与 1（最严格）都是合法取值
        for threshold in (0.0, 1.0):
            with self.subTest(threshold=threshold):
                validate_config(SentimentConfig(quality=SentimentQuality(margin_completeness_threshold=threshold)))


if __name__ == '__main__':
    unittest.main()
