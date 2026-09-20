"""情绪模块 P1：配置对象、min_periods 推导与校验契约。"""

from __future__ import annotations

import unittest
from unittest import mock

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

    def test_margin_net_buy_has_availability_lag(self):
        by_name = {c.name: c for c in DEFAULT_COMPONENTS}
        self.assertEqual(by_name['margin_net_buy'].availability_lag, 1)
        self.assertEqual(by_name['turnover'].availability_lag, 0)

    def test_multiscale_profile_is_valid(self):
        config = SentimentConfig(quality=SentimentQuality(components=PROFILE_MULTISCALE_SUGGESTED))
        validate_config(config)
        by_name = {c.name: c for c in PROFILE_MULTISCALE_SUGGESTED}
        self.assertEqual([s.window for s in by_name['turnover'].scales], [63, 252])
        self.assertEqual(by_name['pcr'].scales[0].window, 252)


class WindowProfileTests(unittest.TestCase):
    """窗口画像：21 天（约 1 个月）与 21/63 混合，以及窗口敏感性工具。"""

    def test_fast_profile_uses_one_month_window(self):
        config = config_for_profile(PROFILE_FAST, percentile_window=21)
        validate_config(config)
        self.assertEqual({s.window for c in config.quality.components for s in c.scales}, {21})
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
        for name in ('volume_ratio', 'return_20d', 'macd_hist_norm'):
            with self.subTest(component=name):
                self.assertEqual(by_name[name].scales[0].window, 63)

    def test_with_percentile_window_rewrites_every_scale(self):
        rewritten = with_percentile_window(DEFAULT_COMPONENTS, 21)
        self.assertEqual({s.window for c in rewritten for s in c.scales}, {21})
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

    def test_config_for_window_scales_components_and_composite(self):
        config = config_for_window('1个季度')
        validate_config(config)
        self.assertEqual(config.windows.sentiment_percentile_scales[0].window, 63)
        self.assertEqual({s.window for c in config.quality.components for s in c.scales}, {63})
        self.assertEqual(len(config.quality.components), len(DEFAULT_COMPONENTS))

    def test_mixed_profile_shields_smoothed_components(self):
        config = config_for_window(21, profile='mixed')
        validate_config(config)
        windows = {c.name: c.scales[0].window for c in config.quality.components}
        self.assertEqual(windows['turnover'], 21)
        self.assertEqual(windows['pcr'], 21)
        for name in ('volume_ratio', 'return_20d', 'macd_hist_norm'):
            with self.subTest(component=name):
                self.assertEqual(windows[name], 63)
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


if __name__ == '__main__':
    unittest.main()
