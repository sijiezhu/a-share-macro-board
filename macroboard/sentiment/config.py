"""情绪模块配置对象：frozen dataclass，支持 from_dict / to_dict / replace。

设计要点
- 每个分量（ComponentSpec）自带尺度列表（scales），因此不同指标可以使用不同时间跨度，
  也可以做"短尺度 + 中尺度"的加权混合。
- 权重只要求非负且总和 > 0，使用时按组内总和归一化：全部填 1.0 即等权。
- `percentile_method` 默认 'midrank_exclusive'（严格早于当日 + 中位秩 + 满窗），
  与 docs/DATA_SOURCES.md 的股债利差分位同口径；'rank_inclusive' 为可选口径。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from dataclasses import fields as dataclass_fields
from typing import Any, Literal

PercentileMethod = Literal['midrank_exclusive', 'rank_inclusive']
ComponentGroup = Literal['participation', 'direction']
ComponentSign = Literal['direct', 'reverse']
Horizon = Literal['short', 'mid', 'long', 'mixed']

PERCENTILE_METHODS: tuple[str, ...] = ('midrank_exclusive', 'rank_inclusive')
COMPONENT_GROUPS: tuple[str, ...] = ('participation', 'direction')
COMPONENT_SIGNS: tuple[str, ...] = ('direct', 'reverse')
PRICE_GAP_POLICIES: tuple[str, ...] = ('ffill_state', 'nan_propagate')
ZERO_VOLUME_POLICIES: tuple[str, ...] = ('nan', 'keep')
MISSING_POLICIES: tuple[str, ...] = ('nan', 'ffill')
DUP_POLICIES: tuple[str, ...] = ('last', 'first', 'raise')
INDEX_MISSING_POLICIES: tuple[str, ...] = ('strict', 'renormalize')
ON_MISSING_COLUMN_POLICIES: tuple[str, ...] = ('mark', 'raise')

DEFAULT_MOMENTUM: Mapping[str, int] = {'sentiment': 20, 'participation': 20, 'direction': 20}
MIN_PERIODS_RATIO: float = 120 / 252
DEFAULT_PERCENTILE_WINDOW: int = 252


def derive_min_periods(window: int, ratio: float = MIN_PERIODS_RATIO) -> int:
    """按窗口推导 min_periods：ceil(window × ratio)，并夹在 [2, window]。"""
    if window < 2:
        raise ValueError(f'窗口必须 >= 2，收到 {window}')
    return max(2, min(int(window), math.ceil(window * ratio)))


@dataclass(frozen=True, slots=True)
class ScaleSpec:
    """一个分量在某一时间跨度上的分位配置。"""

    window: int
    weight: float = 1.0
    min_periods: int | None = None
    method: PercentileMethod | None = None


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    """一个情绪分量：来源、归属维度、方向、尺度与可得性滞后。"""

    name: str
    source: str
    group: ComponentGroup
    sign: ComponentSign = 'direct'
    scales: tuple[ScaleSpec, ...] = (ScaleSpec(DEFAULT_PERCENTILE_WINDOW),)
    weight: float = 1.0
    availability_lag: int = 0
    horizon: Horizon | None = None


@dataclass(frozen=True, slots=True)
class SentimentWindows:
    momentum: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_MOMENTUM))
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_ma: int = 20
    min_periods_ratio: float = MIN_PERIODS_RATIO
    return_horizons: tuple[int, ...] = (20,)
    sentiment_percentile_scales: tuple[ScaleSpec, ...] = (ScaleSpec(DEFAULT_PERCENTILE_WINDOW),)
    direction_rebound_lag: int = 5
    divergence_lookback: int = 20


@dataclass(frozen=True, slots=True)
class SentimentThresholds:
    high_participation: float = 60.0
    low_participation: float = 40.0
    high_direction: float = 60.0
    low_direction: float = 40.0
    panic_turnover_pct: float = 80.0
    panic_return_20d: float = -0.08
    panic_volume_ratio: float = 1.5
    panic_down_ratio: float = 0.70
    reversal_volume_shrink: float = 0.30
    reversal_panic_score: float = 60.0
    min_panic_coverage_for_reversal: float = 0.60


def _default_components() -> tuple[ComponentSpec, ...]:
    # 局部导入避免 config <-> registry 的循环依赖
    from .registry import DEFAULT_COMPONENTS

    return DEFAULT_COMPONENTS


@dataclass(frozen=True, slots=True)
class SentimentQuality:
    percentile_method: PercentileMethod = 'midrank_exclusive'
    components: tuple[ComponentSpec, ...] = field(default_factory=_default_components)
    scale_min_coverage: float = 0.5
    min_index_coverage: float = 0.5
    index_missing: str = 'strict'
    price_gap_policy: str = 'ffill_state'
    price_gap_ffill_limit: int | None = 20
    zero_volume_policy: str = 'nan'
    missing_policy: str = 'nan'
    ffill_limit: int = 0
    ffill_whitelist: tuple[str, ...] = ()
    winsorize_enabled: bool = False
    winsorize_window: int = 252
    winsorize_lower_q: float = 0.01
    winsorize_upper_q: float = 0.99
    dup_policy: str = 'last'
    cross_latch_days: int = 0
    divergence_require_negative_hist: bool = False
    warmup_bars: int = 0
    on_missing_column: str = 'mark'
    include_labels: bool = True


@dataclass(frozen=True, slots=True)
class SentimentConfig:
    windows: SentimentWindows = field(default_factory=SentimentWindows)
    thresholds: SentimentThresholds = field(default_factory=SentimentThresholds)
    quality: SentimentQuality = field(default_factory=SentimentQuality)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SentimentConfig:
        return _config_from_mapping(raw)

    def to_dict(self) -> dict[str, object]:
        return {
            'windows': _windows_to_dict(self.windows),
            'thresholds': _dataclass_to_dict(self.thresholds),
            'quality': _quality_to_dict(self.quality),
        }

    def replace(self, **overrides: object) -> SentimentConfig:
        """按 section 覆盖：`config.replace(thresholds={'high_participation': 70})`。"""
        allowed = {'windows', 'thresholds', 'quality'}
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f'未知配置段：{sorted(unknown)}，可用：{sorted(allowed)}')
        current: dict[str, object] = {
            'windows': self.windows,
            'thresholds': self.thresholds,
            'quality': self.quality,
        }
        for key, value in overrides.items():
            if value is None:
                continue
            if isinstance(value, Mapping):
                current[key] = _replace_section(current[key], dict(value))
            else:
                current[key] = value
        return SentimentConfig(
            windows=current['windows'],  # type: ignore[arg-type]
            thresholds=current['thresholds'],  # type: ignore[arg-type]
            quality=current['quality'],  # type: ignore[arg-type]
        )


def _replace_section(section: object, values: dict[str, object]) -> object:
    if isinstance(section, SentimentWindows):
        return _windows_from_mapping({**_windows_to_dict(section), **values})
    if isinstance(section, SentimentThresholds):
        return _thresholds_from_mapping({**_dataclass_to_dict(section), **values})
    if isinstance(section, SentimentQuality):
        return _quality_from_mapping({**_quality_to_dict(section), **values})
    raise ValueError(f'无法按段覆盖：{type(section)!r}')


def _dataclass_to_dict(obj: object) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in dataclass_fields(obj):
        name = item.name
        value = getattr(obj, name)
        if isinstance(value, ComponentSpec):
            out[name] = _component_to_dict(value)
        elif isinstance(value, ScaleSpec):
            out[name] = _scale_to_dict(value)
        elif isinstance(value, tuple) and value and isinstance(value[0], ComponentSpec):
            out[name] = [_component_to_dict(item) for item in value]
        elif isinstance(value, tuple) and value and isinstance(value[0], ScaleSpec):
            out[name] = [_scale_to_dict(item) for item in value]
        elif isinstance(value, Mapping):
            out[name] = dict(value)
        else:
            out[name] = value
    return out


def _scale_to_dict(scale: ScaleSpec) -> dict[str, object]:
    return {
        'window': scale.window,
        'weight': scale.weight,
        'min_periods': scale.min_periods,
        'method': scale.method,
    }


def _component_to_dict(component: ComponentSpec) -> dict[str, object]:
    return {
        'name': component.name,
        'source': component.source,
        'group': component.group,
        'sign': component.sign,
        'scales': [_scale_to_dict(scale) for scale in component.scales],
        'weight': component.weight,
        'availability_lag': component.availability_lag,
        'horizon': component.horizon,
    }


def _windows_to_dict(windows: SentimentWindows) -> dict[str, object]:
    return {
        'momentum': dict(windows.momentum),
        'macd_fast': windows.macd_fast,
        'macd_slow': windows.macd_slow,
        'macd_signal': windows.macd_signal,
        'volume_ma': windows.volume_ma,
        'min_periods_ratio': windows.min_periods_ratio,
        'return_horizons': list(windows.return_horizons),
        'sentiment_percentile_scales': [_scale_to_dict(s) for s in windows.sentiment_percentile_scales],
        'direction_rebound_lag': windows.direction_rebound_lag,
        'divergence_lookback': windows.divergence_lookback,
    }


def _quality_to_dict(quality: SentimentQuality) -> dict[str, object]:
    raw = _dataclass_to_dict(quality)
    raw['components'] = [_component_to_dict(c) for c in quality.components]
    raw['ffill_whitelist'] = list(quality.ffill_whitelist)
    return raw


def _scale_from_raw(raw: object) -> ScaleSpec:
    if isinstance(raw, ScaleSpec):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError(f'scales 元素必须是 ScaleSpec 或映射，收到 {raw!r}')
    unknown = set(raw) - {'window', 'weight', 'min_periods', 'method'}
    if unknown:
        raise ValueError(f'scales 含未知字段：{sorted(unknown)}')
    if 'window' not in raw:
        raise ValueError('scales 元素缺少 window')
    return ScaleSpec(
        window=int(raw['window']),  # type: ignore[arg-type]
        weight=float(raw.get('weight', 1.0)),  # type: ignore[arg-type]
        min_periods=None if raw.get('min_periods') is None else int(raw['min_periods']),  # type: ignore[arg-type]
        method=raw.get('method'),  # type: ignore[arg-type]
    )


def _component_from_raw(raw: object) -> ComponentSpec:
    if isinstance(raw, ComponentSpec):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError(f'components 元素必须是 ComponentSpec 或映射，收到 {raw!r}')
    allowed = {'name', 'source', 'group', 'sign', 'scales', 'weight', 'availability_lag', 'horizon'}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f'components 含未知字段：{sorted(unknown)}')
    missing = {'name', 'source', 'group'} - set(raw)
    if missing:
        raise ValueError(f'components 元素缺少字段：{sorted(missing)}')
    scales_raw = raw.get('scales')
    if scales_raw is None:
        scales = (ScaleSpec(DEFAULT_PERCENTILE_WINDOW),)
    else:
        if not isinstance(scales_raw, (list, tuple)) or not scales_raw:
            raise ValueError('components.scales 必须是非空列表')
        scales = tuple(_scale_from_raw(item) for item in scales_raw)
    return ComponentSpec(
        name=str(raw['name']),
        source=str(raw['source']),
        group=str(raw['group']),  # type: ignore[arg-type]
        sign=str(raw.get('sign', 'direct')),  # type: ignore[arg-type]
        scales=scales,
        weight=float(raw.get('weight', 1.0)),  # type: ignore[arg-type]
        availability_lag=int(raw.get('availability_lag', 0)),  # type: ignore[arg-type]
        horizon=None if raw.get('horizon') is None else str(raw['horizon']),  # type: ignore[arg-type]
    )


def _thresholds_from_mapping(raw: Mapping[str, object]) -> SentimentThresholds:
    fields = {
        'high_participation',
        'low_participation',
        'high_direction',
        'low_direction',
        'panic_turnover_pct',
        'panic_return_20d',
        'panic_volume_ratio',
        'panic_down_ratio',
        'reversal_volume_shrink',
        'reversal_panic_score',
        'min_panic_coverage_for_reversal',
    }
    unknown = set(raw) - fields
    if unknown:
        raise ValueError(f'thresholds 含未知字段：{sorted(unknown)}')
    kwargs: dict[str, Any] = {k: float(v) for k, v in raw.items()}  # type: ignore[arg-type]
    return replace(SentimentThresholds(), **kwargs)


def _windows_from_mapping(raw: Mapping[str, object]) -> SentimentWindows:
    fields = {
        'momentum',
        'macd_fast',
        'macd_slow',
        'macd_signal',
        'volume_ma',
        'min_periods_ratio',
        'return_horizons',
        'sentiment_percentile_scales',
        'direction_rebound_lag',
        'divergence_lookback',
    }
    unknown = set(raw) - fields
    if unknown:
        raise ValueError(f'windows 含未知字段：{sorted(unknown)}')
    values = dict(raw)
    if 'momentum' in values:
        momentum = values['momentum']
        if not isinstance(momentum, Mapping):
            raise ValueError('windows.momentum 必须是映射')
        values['momentum'] = {str(k): int(v) for k, v in momentum.items()}  # type: ignore[arg-type]
    if 'return_horizons' in values:
        horizons = values['return_horizons']
        if not isinstance(horizons, (list, tuple)):
            raise ValueError('windows.return_horizons 必须是列表')
        values['return_horizons'] = tuple(int(h) for h in horizons)
    if 'sentiment_percentile_scales' in values:
        scales = values['sentiment_percentile_scales']
        if not isinstance(scales, (list, tuple)) or not scales:
            raise ValueError('windows.sentiment_percentile_scales 必须是非空列表')
        values['sentiment_percentile_scales'] = tuple(_scale_from_raw(item) for item in scales)
    return replace(SentimentWindows(), **values)  # type: ignore[arg-type]


def _quality_from_mapping(raw: Mapping[str, object]) -> SentimentQuality:
    fields = {
        'percentile_method',
        'components',
        'scale_min_coverage',
        'min_index_coverage',
        'index_missing',
        'price_gap_policy',
        'price_gap_ffill_limit',
        'zero_volume_policy',
        'missing_policy',
        'ffill_limit',
        'ffill_whitelist',
        'winsorize_enabled',
        'winsorize_window',
        'winsorize_lower_q',
        'winsorize_upper_q',
        'dup_policy',
        'cross_latch_days',
        'divergence_require_negative_hist',
        'warmup_bars',
        'on_missing_column',
        'include_labels',
    }
    unknown = set(raw) - fields
    if unknown:
        raise ValueError(f'quality 含未知字段：{sorted(unknown)}')
    values = dict(raw)
    if 'components' in values:
        components = values['components']
        if not isinstance(components, (list, tuple)) or not components:
            raise ValueError('quality.components 必须是非空列表')
        values['components'] = tuple(_component_from_raw(item) for item in components)
    if 'ffill_whitelist' in values:
        whitelist = values['ffill_whitelist']
        if isinstance(whitelist, str) or not isinstance(whitelist, (list, tuple)):
            raise ValueError('quality.ffill_whitelist 必须是列表')
        values['ffill_whitelist'] = tuple(str(item) for item in whitelist)
    return replace(SentimentQuality(), **values)  # type: ignore[arg-type]


def _config_from_mapping(raw: Mapping[str, object]) -> SentimentConfig:
    unknown = set(raw) - {'windows', 'thresholds', 'quality'}
    if unknown:
        raise ValueError(f'配置含未知段：{sorted(unknown)}')
    windows = raw.get('windows')
    thresholds = raw.get('thresholds')
    quality = raw.get('quality')
    return SentimentConfig(
        windows=_windows_from_mapping(windows) if isinstance(windows, Mapping) else SentimentWindows(),
        thresholds=(
            _thresholds_from_mapping(thresholds) if isinstance(thresholds, Mapping) else SentimentThresholds()
        ),
        quality=_quality_from_mapping(quality) if isinstance(quality, Mapping) else SentimentQuality(),
    )


def resolve_min_periods(scale: ScaleSpec, method: str, ratio: float) -> int:
    """解析某个尺度的 min_periods：显式配置优先，否则按窗口推导（默认 252 -> 120）。

    分位口径（严格早于当日 + 中位秩）与最小样本数是两个独立旋钮：
    需要满窗时显式写 `min_periods=window` 即可，不再由 `percentile_method` 决定。
    """
    if scale.min_periods is not None:
        return int(scale.min_periods)
    return derive_min_periods(scale.window, ratio)


def resolve_method(scale: ScaleSpec, default_method: str) -> str:
    return str(scale.method) if scale.method is not None else str(default_method)


def validate_config(config: SentimentConfig) -> None:
    """校验配置；非法直接抛 ValueError（不静默纠正）。"""
    windows = config.windows
    thresholds = config.thresholds
    quality = config.quality

    if not (0 < windows.min_periods_ratio <= 1):
        raise ValueError(f'windows.min_periods_ratio 必须在 (0, 1]，收到 {windows.min_periods_ratio}')
    if windows.macd_fast < 1 or windows.macd_slow < 1 or windows.macd_signal < 1:
        raise ValueError('MACD 参数必须 >= 1')
    if windows.macd_fast >= windows.macd_slow:
        raise ValueError(f'windows.macd_fast 必须小于 macd_slow（收到 {windows.macd_fast} / {windows.macd_slow}）')
    if windows.volume_ma < 2:
        raise ValueError(f'windows.volume_ma 必须 >= 2，收到 {windows.volume_ma}')
    if windows.divergence_lookback < 2:
        raise ValueError(f'windows.divergence_lookback 必须 >= 2，收到 {windows.divergence_lookback}')
    if windows.direction_rebound_lag < 1:
        raise ValueError(f'windows.direction_rebound_lag 必须 >= 1，收到 {windows.direction_rebound_lag}')
    if not windows.return_horizons:
        raise ValueError('windows.return_horizons 不能为空')
    if any(int(h) < 1 for h in windows.return_horizons):
        raise ValueError(f'windows.return_horizons 必须为正整数，收到 {windows.return_horizons}')
    if len(set(int(h) for h in windows.return_horizons)) != len(windows.return_horizons):
        raise ValueError(f'windows.return_horizons 不能重复，收到 {windows.return_horizons}')
    for key in ('sentiment', 'participation', 'direction'):
        if key not in windows.momentum:
            raise ValueError(f'windows.momentum 缺少 {key!r}（需要 sentiment / participation / direction）')
        if int(windows.momentum[key]) < 1:
            raise ValueError(f'windows.momentum[{key!r}] 必须 >= 1，收到 {windows.momentum[key]}')
    if not windows.sentiment_percentile_scales:
        raise ValueError('windows.sentiment_percentile_scales 不能为空')
    for scale in windows.sentiment_percentile_scales:
        _validate_scale(scale, windows.min_periods_ratio, quality.percentile_method, context='sentiment')

    if thresholds.low_participation >= thresholds.high_participation:
        raise ValueError(
            f'thresholds.low_participation 必须小于 high_participation'
            f'（收到 {thresholds.low_participation} / {thresholds.high_participation}）'
        )
    if thresholds.low_direction >= thresholds.high_direction:
        raise ValueError(
            f'thresholds.low_direction 必须小于 high_direction'
            f'（收到 {thresholds.low_direction} / {thresholds.high_direction}）'
        )
    if not 0 <= thresholds.min_panic_coverage_for_reversal <= 1:
        raise ValueError(
            f'thresholds.min_panic_coverage_for_reversal 必须在 [0, 1]，'
            f'收到 {thresholds.min_panic_coverage_for_reversal}'
        )
    if not 0 <= thresholds.reversal_volume_shrink < 1:
        raise ValueError(
            f'thresholds.reversal_volume_shrink 必须在 [0, 1)，收到 {thresholds.reversal_volume_shrink}'
        )
    if not (0 <= quality.scale_min_coverage <= 1):
        raise ValueError(f'quality.scale_min_coverage 必须在 [0, 1]，收到 {quality.scale_min_coverage}')
    if not (0 <= quality.min_index_coverage <= 1):
        raise ValueError(f'quality.min_index_coverage 必须在 [0, 1]，收到 {quality.min_index_coverage}')
    if not (0 <= quality.winsorize_lower_q < quality.winsorize_upper_q <= 1):
        raise ValueError(
            f'quality.winsorize 分位必须满足 0 <= lower < upper <= 1，'
            f'收到 {quality.winsorize_lower_q} / {quality.winsorize_upper_q}'
        )
    if quality.winsorize_window < 2:
        raise ValueError(f'quality.winsorize_window 必须 >= 2，收到 {quality.winsorize_window}')
    if quality.ffill_limit < 0:
        raise ValueError(f'quality.ffill_limit 必须 >= 0，收到 {quality.ffill_limit}')
    if quality.warmup_bars < 0:
        raise ValueError(f'quality.warmup_bars 必须 >= 0，收到 {quality.warmup_bars}')
    if quality.cross_latch_days < 0:
        raise ValueError(f'quality.cross_latch_days 必须 >= 0，收到 {quality.cross_latch_days}')
    if quality.price_gap_ffill_limit is not None and quality.price_gap_ffill_limit < 0:
        raise ValueError(f'quality.price_gap_ffill_limit 必须 >= 0 或 None，收到 {quality.price_gap_ffill_limit}')
    if quality.percentile_method not in PERCENTILE_METHODS:
        raise ValueError(
            f'quality.percentile_method 必须是 {PERCENTILE_METHODS} 之一，收到 {quality.percentile_method!r}'
        )
    _validate_choice('quality.index_missing', quality.index_missing, INDEX_MISSING_POLICIES)
    _validate_choice('quality.price_gap_policy', quality.price_gap_policy, PRICE_GAP_POLICIES)
    _validate_choice('quality.zero_volume_policy', quality.zero_volume_policy, ZERO_VOLUME_POLICIES)
    _validate_choice('quality.missing_policy', quality.missing_policy, MISSING_POLICIES)
    _validate_choice('quality.dup_policy', quality.dup_policy, DUP_POLICIES)
    _validate_choice('quality.on_missing_column', quality.on_missing_column, ON_MISSING_COLUMN_POLICIES)

    if not quality.components:
        raise ValueError('quality.components 不能为空')
    names = [component.name for component in quality.components]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f'分量名重复：{duplicates}')
    for component in quality.components:
        if component.group not in COMPONENT_GROUPS:
            raise ValueError(
                f'分量 {component.name!r} 的 group 必须是 {COMPONENT_GROUPS} 之一，收到 {component.group!r}'
            )
        if component.sign not in COMPONENT_SIGNS:
            raise ValueError(
                f'分量 {component.name!r} 的 sign 必须是 {COMPONENT_SIGNS} 之一，收到 {component.sign!r}'
            )
        if component.weight < 0:
            raise ValueError(f'分量 {component.name!r} 的 weight 必须 >= 0，收到 {component.weight}')
        if component.availability_lag < 0:
            raise ValueError(f'分量 {component.name!r} 的 availability_lag 必须 >= 0')
        if not component.scales:
            raise ValueError(f'分量 {component.name!r} 的 scales 不能为空')
        windows_seen = [scale.window for scale in component.scales]
        if len(set(windows_seen)) != len(windows_seen):
            raise ValueError(f'分量 {component.name!r} 的 scales 窗口重复：{windows_seen}')
        if any(scale.weight < 0 for scale in component.scales):
            raise ValueError(f'分量 {component.name!r} 的 scales 权重必须 >= 0')
        if sum(scale.weight for scale in component.scales) <= 0:
            raise ValueError(f'分量 {component.name!r} 的 scales 权重之和必须 > 0')
        for scale in component.scales:
            _validate_scale(scale, windows.min_periods_ratio, quality.percentile_method, context=component.name)
    for group in COMPONENT_GROUPS:
        group_weight = sum(c.weight for c in quality.components if c.group == group)
        if group_weight <= 0:
            raise ValueError(f'{group} 组的权重之和必须 > 0')


def _validate_scale(scale: ScaleSpec, ratio: float, default_method: str, *, context: str) -> None:
    if scale.window < 2:
        raise ValueError(f'{context} 的窗口必须 >= 2，收到 {scale.window}')
    if scale.weight < 0:
        raise ValueError(f'{context} 的尺度权重必须 >= 0，收到 {scale.weight}')
    method = resolve_method(scale, default_method)
    if method not in PERCENTILE_METHODS:
        raise ValueError(f'{context} 的 method 必须是 {PERCENTILE_METHODS} 之一，收到 {method!r}')
    min_periods = resolve_min_periods(scale, method, ratio)
    if min_periods > scale.window:
        raise ValueError(
            f'{context} 的 min_periods（{min_periods}）不能大于窗口（{scale.window}）'
        )
    if min_periods < 2:
        raise ValueError(f'{context} 的 min_periods 必须 >= 2，收到 {min_periods}')


def _validate_choice(name: str, value: object, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(f'{name} 必须是 {allowed} 之一，收到 {value!r}')
