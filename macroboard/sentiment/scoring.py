"""指数合成、四象限状态、恐慌抛售得分、反转观察与量价状态（纯函数，无 IO）。

口径要点
- 参与度/方向 = 组内分量的（可用）加权平均；覆盖率不足则整体为 NaN（不可用）。
- 状态、频带、量价状态一律输出中性码值（见 codes.py），中文文案在 labels.py。
- 恐慌分用 renormalize：`100 × 命中 / 可评估`，另给 coverage 与 confidence；
  `panic_coverage < min_panic_coverage_for_reversal` 时不触发 reversal_watch。
- `reversal_watch` 恒为 True/False，数据不足时输出 False 并在 block_reason 说明原因。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import codes
from .config import (
    ComponentSpec,
    SentimentConfig,
    SentimentThresholds,
    resolve_min_periods,
    validate_config,
)
from .features import ComponentMeta, FeatureBundle, compute_features, rolling_percentile


@dataclass(frozen=True, slots=True)
class IndexResult:
    value: pd.Series
    coverage: pd.Series
    missing_asof: tuple[str, ...]
    never_available: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PanicResult:
    score: pd.Series
    hits: pd.Series
    coverage: pd.Series
    confidence: pd.Series
    reasons: pd.Series
    missing: pd.Series


@dataclass(frozen=True, slots=True)
class ReversalResult:
    watch: pd.Series
    reasons: pd.Series
    missing: pd.Series
    block_reason: pd.Series
    conditions: pd.DataFrame


@dataclass(frozen=True, slots=True)
class MacdVolumeResult:
    state: pd.Series
    divergence: pd.Series
    golden_cross: pd.Series
    dead_cross: pd.Series
    days_since_golden_cross: pd.Series


@dataclass(frozen=True, slots=True)
class ScoreBundle:
    frame: pd.DataFrame
    panic: PanicResult
    reversal: ReversalResult
    macd_volume: MacdVolumeResult
    component_meta: Mapping[str, ComponentMeta]
    warnings: tuple[str, ...]


def _tuple_column(flags: pd.DataFrame) -> pd.Series:
    """布尔矩阵 -> 每行命中的码值元组（列序固定，便于复现）。"""
    order = list(flags.columns)
    rows = flags.to_numpy(dtype=bool)
    values = [tuple(code for code, hit in zip(order, row, strict=True) if hit) for row in rows]
    return pd.Series(values, index=flags.index, dtype='object')


def _to_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors='coerce').astype('float64')


def missing_components(
    values: Mapping[str, pd.Series],
    specs: Sequence[ComponentSpec],
) -> pd.Series:
    """逐行的缺失分量码值元组（因果：只看当日，不看未来）。"""
    flags = pd.DataFrame({spec.name: _to_float(values[spec.name]).isna() for spec in specs})
    return _tuple_column(flags)


def _frame_column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name in frame.columns:
        return frame[name]
    return pd.Series(np.nan, index=frame.index, dtype='float64')


def compose_index(
    values: Mapping[str, pd.Series],
    specs: Sequence[ComponentSpec],
    *,
    min_coverage: float,
) -> IndexResult:
    """组内加权平均：权重按可用分量重归一，覆盖率不足则整列为 NaN。"""
    if not specs:
        raise ValueError('compose_index 需要至少一个分量')
    matrix = pd.DataFrame({spec.name: _to_float(values[spec.name]) for spec in specs})
    weights = pd.Series({spec.name: float(spec.weight) for spec in specs}, dtype='float64')
    total_weight = float(weights.sum())
    if total_weight <= 0:
        raise ValueError('组内权重之和必须 > 0')
    available = matrix.notna()
    available_weight = available.mul(weights, axis=1).sum(axis=1)
    with np.errstate(invalid='ignore', divide='ignore'):
        value = matrix.fillna(0.0).mul(weights, axis=1).sum(axis=1) / available_weight.replace(0.0, np.nan)
    coverage = available_weight / total_weight
    value = value.where(coverage >= min_coverage)
    value = value.clip(lower=0.0, upper=100.0)
    last_row = matrix.iloc[-1] if len(matrix) else pd.Series(dtype='float64')
    missing_asof = tuple(spec.name for spec in specs if bool(last_row.isna().get(spec.name, True)))
    never_available = tuple(spec.name for spec in specs if not bool(matrix[spec.name].notna().any()))
    return IndexResult(
        value=value,
        coverage=coverage.astype('float64'),
        missing_asof=missing_asof,
        never_available=never_available,
    )


def index_band(values: pd.Series, *, low: float, high: float) -> pd.Series:
    """高/中/低频带（用于真 2×2 网格）。"""
    numeric = _to_float(values)
    band = pd.Series(codes.BAND_UNAVAILABLE, index=numeric.index, dtype='object')
    band = band.mask(numeric.notna() & (numeric > high), codes.BAND_HIGH)
    band = band.mask(numeric.notna() & (numeric >= low) & (numeric <= high), codes.BAND_MID)
    band = band.mask(numeric.notna() & (numeric < low), codes.BAND_LOW)
    return band


def classify_state(
    participation_index: pd.Series,
    direction_index: pd.Series,
    thresholds: SentimentThresholds,
) -> pd.Series:
    """四象限状态码；边界值（恰好 60/40）落在 STATE_NEUTRAL。"""
    participation = _to_float(participation_index)
    direction = _to_float(direction_index)
    usable = participation.notna() & direction.notna()
    state = pd.Series(codes.STATE_UNAVAILABLE, index=participation.index, dtype='object')
    state = state.mask(usable, codes.STATE_NEUTRAL)
    state = state.mask(
        usable & (participation > thresholds.high_participation) & (direction > thresholds.high_direction),
        codes.STATE_GREED,
    )
    state = state.mask(
        usable & (participation > thresholds.high_participation) & (direction < thresholds.low_direction),
        codes.STATE_PANIC,
    )
    state = state.mask(
        usable & (participation < thresholds.low_participation) & (direction < thresholds.low_direction),
        codes.STATE_COLD,
    )
    state = state.mask(
        usable & (participation < thresholds.low_participation) & (direction > thresholds.high_direction),
        codes.STATE_THAW,
    )
    return state


def panic_metrics(
    *,
    turnover_pct: pd.Series,
    return_20d: pd.Series,
    volume_ratio: pd.Series,
    macd_hist: pd.Series,
    down_ratio: pd.Series,
    thresholds: SentimentThresholds,
    readiness: pd.Series | None = None,
) -> PanicResult:
    """恐慌抛售得分：renormalize + coverage + confidence。"""
    turnover = _to_float(turnover_pct)
    returns = _to_float(return_20d)
    ratio = _to_float(volume_ratio)
    hist = _to_float(macd_hist)
    down = _to_float(down_ratio)
    hist_prev = hist.shift(1)

    evaluable = pd.DataFrame(
        {
            codes.PANIC_TURNOVER_PCT_HIGH: turnover.notna(),
            codes.PANIC_RETURN_20D_LOW: returns.notna(),
            codes.PANIC_VOLUME_RATIO_HIGH: ratio.notna(),
            codes.PANIC_HIST_NEGATIVE_FALLING: hist.notna() & hist_prev.notna(),
            codes.PANIC_DOWN_RATIO_HIGH: down.notna(),
        }
    )
    conditions = pd.DataFrame(
        {
            codes.PANIC_TURNOVER_PCT_HIGH: turnover > thresholds.panic_turnover_pct,
            codes.PANIC_RETURN_20D_LOW: returns < thresholds.panic_return_20d,
            codes.PANIC_VOLUME_RATIO_HIGH: ratio > thresholds.panic_volume_ratio,
            codes.PANIC_HIST_NEGATIVE_FALLING: (hist < 0) & (hist < hist_prev),
            codes.PANIC_DOWN_RATIO_HIGH: down > thresholds.panic_down_ratio,
        }
    )
    hits = (conditions & evaluable).sum(axis=1)
    n_eval = evaluable.sum(axis=1)
    coverage = n_eval / len(evaluable.columns)
    with np.errstate(invalid='ignore', divide='ignore'):
        score = 100.0 * hits / n_eval.where(n_eval > 0)
    score = score.clip(lower=0.0, upper=100.0)
    if readiness is None:
        ready = pd.Series(1.0, index=score.index, dtype='float64')
    else:
        ready = _to_float(readiness).clip(lower=0.0, upper=1.0)
    confidence = (coverage * ready).astype('float64')
    hits_nullable = pd.Series(pd.array(hits.where(n_eval > 0), dtype='Int64'), index=score.index)
    return PanicResult(
        score=score,
        hits=hits_nullable,
        coverage=coverage.astype('float64'),
        confidence=confidence,
        reasons=_tuple_column(conditions & evaluable),
        missing=_tuple_column(~evaluable),
    )


def reversal_metrics(
    *,
    panic: PanicResult,
    macd_hist: pd.Series,
    volume: pd.Series,
    direction_index: pd.Series,
    volume_ma: int,
    shrink: float,
    panic_score_threshold: float,
    coverage_min: float,
    rebound_lag: int,
) -> tuple[ReversalResult, pd.Series]:
    """冰点反转观察信号；返回（结果, 成交量/20日峰值 比值）。"""
    hist = _to_float(macd_hist)
    volumes = _to_float(volume)
    direction = _to_float(direction_index)
    peak = volumes.rolling(volume_ma, min_periods=volume_ma).max()
    with np.errstate(invalid='ignore', divide='ignore'):
        shrink_ratio = volumes / peak.where(peak > 0)
    direction_prev = direction.shift(rebound_lag)

    evaluable = pd.DataFrame(
        {
            codes.REV_PANIC_SCORE: panic.score.notna(),
            codes.REV_HIST_SHRINKING: hist.notna() & hist.shift(1).notna() & hist.shift(2).notna(),
            codes.REV_VOLUME_SHRUNK: shrink_ratio.notna(),
            codes.REV_DIRECTION_REBOUND: direction.notna() & direction_prev.notna(),
        }
    )
    conditions = pd.DataFrame(
        {
            codes.REV_PANIC_SCORE: panic.score > panic_score_threshold,
            codes.REV_HIST_SHRINKING: (hist < 0) & (hist.shift(1) < 0) & (hist.shift(2) < 0)
            & (hist > hist.shift(1))
            & (hist.shift(1) > hist.shift(2)),
            codes.REV_VOLUME_SHRUNK: shrink_ratio < (1.0 - shrink),
            codes.REV_DIRECTION_REBOUND: direction > direction_prev,
        }
    )
    satisfied = conditions & evaluable
    has_missing = ~evaluable.all(axis=1)
    coverage_ok = panic.coverage >= coverage_min
    score_ok = panic.score.notna()

    block = pd.Series([None] * len(evaluable), index=evaluable.index, dtype='object')
    block = block.mask(~coverage_ok, codes.REV_BLOCK_PANIC_COVERAGE_LOW)
    block = block.mask(coverage_ok & ~score_ok, codes.REV_BLOCK_PANIC_SCORE_MISSING)
    block = block.mask(coverage_ok & score_ok & has_missing, codes.REV_BLOCK_CONDITION_DATA_MISSING)
    fired = coverage_ok & score_ok & ~has_missing & satisfied.all(axis=1)
    watch = fired.astype(bool)
    return (
        ReversalResult(
            watch=watch,
            reasons=_tuple_column(satisfied),
            missing=_tuple_column(~evaluable),
            block_reason=block,
            conditions=conditions,
        ),
        shrink_ratio.rename('volume_shrink_ratio'),
    )


def macd_volume_metrics(
    *,
    macd: pd.Series,
    macd_signal: pd.Series,
    volume_ratio: pd.Series,
    close: pd.Series,
    macd_hist: pd.Series,
    lookback: int,
    require_negative_hist: bool = False,
) -> MacdVolumeResult:
    """量价状态码 + 底背离。优先级：交叉（事件） > 底背离 > 中性。"""
    dif = _to_float(macd)
    dea = _to_float(macd_signal)
    ratio = _to_float(volume_ratio)
    prices = _to_float(close)
    hist = _to_float(macd_hist)
    dif_prev = dif.shift(1)
    dea_prev = dea.shift(1)

    golden = (dif > dea) & (dif_prev <= dea_prev) & dif.notna() & dea.notna() & dif_prev.notna() & dea_prev.notna()
    dead = (dif < dea) & (dif_prev >= dea_prev) & dif.notna() & dea.notna() & dif_prev.notna() & dea_prev.notna()
    golden = golden.fillna(False).astype(bool)
    dead = dead.fillna(False).astype(bool)

    price_min = prices.rolling(lookback, min_periods=lookback).min()
    hist_min_prev = hist.rolling(lookback, min_periods=lookback).min().shift(1)
    price_new_low = prices.notna() & (prices == price_min)
    hist_not_new_low = hist.notna() & (hist > hist_min_prev)
    divergence_mask = price_new_low & hist_not_new_low
    if require_negative_hist:
        divergence_mask = divergence_mask & (hist < 0)
    evaluable = prices.notna() & hist.notna() & hist_min_prev.notna()
    divergence = pd.Series(
        pd.array(
            np.where(evaluable.to_numpy(dtype=bool), divergence_mask.to_numpy(dtype=bool), None),
            dtype='boolean',
        ),
        index=prices.index,
    )

    state = pd.Series(codes.MACD_VOL_UNAVAILABLE, index=prices.index, dtype='object')
    usable = dif.notna() & dea.notna()
    state = state.mask(usable, codes.MACD_VOL_NEUTRAL)
    state = state.mask(usable & divergence_mask.fillna(False), codes.MACD_VOL_DIVERGENCE)
    surge = ratio > 1.0
    shrink = ratio < 1.0
    state = state.mask(golden & surge, codes.MACD_VOL_GOLDEN_SURGE)
    state = state.mask(golden & shrink, codes.MACD_VOL_GOLDEN_SHRINK)
    state = state.mask(golden & ~surge & ~shrink, codes.MACD_VOL_GOLDEN_UNKNOWN)
    state = state.mask(dead & surge, codes.MACD_VOL_DEAD_SURGE)
    state = state.mask(dead & shrink, codes.MACD_VOL_DEAD_SHRINK)
    state = state.mask(dead & ~surge & ~shrink, codes.MACD_VOL_DEAD_UNKNOWN)
    state = state.mask(~usable, codes.MACD_VOL_UNAVAILABLE)

    positions = pd.Series(np.arange(len(golden), dtype='float64'), index=golden.index)
    last_golden = positions.where(golden).ffill()
    days_since = positions - last_golden
    return MacdVolumeResult(
        state=state,
        divergence=divergence,
        golden_cross=golden,
        dead_cross=dead,
        days_since_golden_cross=days_since,
    )


def _readiness(bundle: FeatureBundle, component: str) -> pd.Series:
    """分位类条件的数据充分度：min(1, 当日有效样本数 / 主尺度 min_periods)。"""
    meta = bundle.meta[component]
    index = meta.windows.index(meta.primary_window)
    required = max(1, int(meta.min_periods[index]))
    counts = _to_float(bundle.counts[component])
    return (counts / required).clip(lower=0.0, upper=1.0)


def score_all(
    frame: pd.DataFrame,
    config: SentimentConfig | None = None,
    *,
    bundle: FeatureBundle | None = None,
) -> ScoreBundle:
    """从规范化输入计算全部情绪指标（不含展示层文案列之外的组装工作）。"""
    cfg = config or SentimentConfig()
    validate_config(cfg)
    features = bundle if bundle is not None else compute_features(frame, cfg)
    base = features.base
    warnings: list[str] = []

    participation_specs = [c for c in cfg.quality.components if c.group == 'participation']
    direction_specs = [c for c in cfg.quality.components if c.group == 'direction']
    participation = compose_index(
        features.values, participation_specs, min_coverage=cfg.quality.min_index_coverage
    )
    direction = compose_index(features.values, direction_specs, min_coverage=cfg.quality.min_index_coverage)
    if participation.never_available:
        warnings.append(f'参与度分量长期不可用：{list(participation.never_available)}')
    if direction.never_available:
        warnings.append(f'方向分量长期不可用：{list(direction.never_available)}')

    if cfg.quality.index_missing == 'strict':
        sentiment_index = 0.5 * participation.value + 0.5 * direction.value
    else:
        available = pd.DataFrame({'p': participation.value, 'd': direction.value})
        sentiment_index = available.mean(axis=1, skipna=True)
        sentiment_index = sentiment_index.where(available.notna().any(axis=1))

    sentiment_scale = cfg.windows.sentiment_percentile_scales[0]
    method = cfg.quality.percentile_method if sentiment_scale.method is None else str(sentiment_scale.method)
    min_periods = resolve_min_periods(sentiment_scale, method, cfg.windows.min_periods_ratio)
    sentiment_pct, _ = rolling_percentile(sentiment_index, sentiment_scale.window, min_periods, method)

    momentum_windows = cfg.windows.momentum
    state = classify_state(participation.value, direction.value, cfg.thresholds)
    participation_band = index_band(
        participation.value, low=cfg.thresholds.low_participation, high=cfg.thresholds.high_participation
    )
    direction_band = index_band(
        direction.value, low=cfg.thresholds.low_direction, high=cfg.thresholds.high_direction
    )

    readiness = _readiness(features, 'turnover') if 'turnover' in features.meta else None
    panic = panic_metrics(
        turnover_pct=features.values['turnover'],
        return_20d=base[f'return_{int(cfg.windows.return_horizons[0])}d'],
        volume_ratio=base['volume_ratio'],
        macd_hist=base['macd_hist'],
        down_ratio=base['down_ratio'],
        thresholds=cfg.thresholds,
        readiness=readiness,
    )
    reversal, shrink_ratio = reversal_metrics(
        panic=panic,
        macd_hist=base['macd_hist'],
        volume=_frame_column(frame, 'volume'),
        direction_index=direction.value,
        volume_ma=cfg.windows.volume_ma,
        shrink=cfg.thresholds.reversal_volume_shrink,
        panic_score_threshold=cfg.thresholds.reversal_panic_score,
        coverage_min=cfg.thresholds.min_panic_coverage_for_reversal,
        rebound_lag=cfg.windows.direction_rebound_lag,
    )
    macd_volume = macd_volume_metrics(
        macd=base['macd'],
        macd_signal=base['macd_signal'],
        volume_ratio=base['volume_ratio'],
        close=_frame_column(frame, 'close'),
        macd_hist=base['macd_hist'],
        lookback=cfg.windows.divergence_lookback,
        require_negative_hist=cfg.quality.divergence_require_negative_hist,
    )

    quality = _data_quality(
        participation=participation, direction=direction, panic=panic
    )
    columns: dict[str, pd.Series] = {
        'participation_index': participation.value,
        'direction_index': direction.value,
        'sentiment_index': sentiment_index,
        f'sentiment_pct_{sentiment_scale.window}': sentiment_pct,
        f'sentiment_momentum_{int(momentum_windows["sentiment"])}d': sentiment_index
        - sentiment_index.shift(int(momentum_windows['sentiment'])),
        f'participation_momentum_{int(momentum_windows["participation"])}d': participation.value
        - participation.value.shift(int(momentum_windows['participation'])),
        f'direction_momentum_{int(momentum_windows["direction"])}d': direction.value
        - direction.value.shift(int(momentum_windows['direction'])),
        'participation_coverage': participation.coverage,
        'direction_coverage': direction.coverage,
        'participation_missing': missing_components(features.values, participation_specs),
        'direction_missing': missing_components(features.values, direction_specs),
        'state': state,
        'participation_band': participation_band,
        'direction_band': direction_band,
        'panic_score': panic.score,
        'panic_hits': panic.hits,
        'panic_coverage': panic.coverage,
        'panic_confidence': panic.confidence,
        'panic_reasons': panic.reasons,
        'panic_missing': panic.missing,
        'reversal_watch': reversal.watch,
        'reversal_reasons': reversal.reasons,
        'reversal_missing': reversal.missing,
        'reversal_block_reason': reversal.block_reason,
        'macd_volume_state': macd_volume.state,
        'macd_bullish_divergence': macd_volume.divergence,
        'macd_golden_cross': macd_volume.golden_cross,
        'macd_dead_cross': macd_volume.dead_cross,
        'days_since_golden_cross': macd_volume.days_since_golden_cross,
        'volume_shrink_ratio': shrink_ratio,
        'direction_chg_5d': direction.value - direction.value.shift(cfg.windows.direction_rebound_lag),
        'data_quality': quality,
        'scale_partial_components': _partial_components(features, frame.index),
    }
    return ScoreBundle(
        frame=pd.DataFrame(columns, index=frame.index),
        panic=panic,
        reversal=reversal,
        macd_volume=macd_volume,
        component_meta=features.meta,
        warnings=tuple(warnings),
    )


def _partial_components(features: FeatureBundle, index: pd.Index) -> pd.Series:
    names = list(features.partial)
    if not names:
        return pd.Series([()] * len(index), index=index, dtype='object')
    matrix = pd.DataFrame(
        {name: features.partial[name].reindex(index).fillna(False).astype(bool) for name in names}
    )
    return _tuple_column(matrix)


def _data_quality(
    *,
    participation: IndexResult,
    direction: IndexResult,
    panic: PanicResult,
) -> pd.Series:
    core_missing = participation.value.isna() | direction.value.isna()
    partial = (participation.coverage < 1.0) | (direction.coverage < 1.0) | panic.score.isna()
    quality = pd.Series(codes.DQ_PARTIAL, index=participation.value.index, dtype='object')
    quality = quality.mask(~partial, codes.DQ_OK)
    quality = quality.mask(core_missing, codes.DQ_INSUFFICIENT)
    return quality
