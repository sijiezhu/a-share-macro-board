"""基础指标与分量分位（纯函数，无 IO）。

本模块实现：
- 两种滚动分位口径（默认 midrank_exclusive：严格早于当日 + 中位秩 + 满窗）；
- 价格类指标（MACD 三段、20 日收益）、量能指标（量比）、结构指标（涨跌停比、家数占比）；
- 分量注册表遍历：每个分量按自己的尺度算分位，支持多尺度加权混合。

所有计算只依赖 t 日及之前的数据：不使用负向 shift、center=True、bfill 或插值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from .config import (
    ComponentSpec,
    SentimentConfig,
    resolve_method,
    resolve_min_periods,
    validate_config,
)
from .registry import (
    count_column,
    emitted_columns,
    primary_scale,
    scale_column,
    value_column,
)


@dataclass(frozen=True, slots=True)
class ScaleResult:
    pct: pd.Series
    count: pd.Series


@dataclass(frozen=True, slots=True)
class BlendResult:
    value: pd.Series
    count: pd.Series
    partial: pd.Series


@dataclass(frozen=True, slots=True)
class ComponentMeta:
    name: str
    group: str
    sign: str
    horizon: str | None
    source: str
    windows: tuple[int, ...]
    weights: tuple[float, ...]
    min_periods: tuple[int, ...]
    methods: tuple[str, ...]
    primary_window: int
    availability_lag: int


@dataclass(frozen=True, slots=True)
class FeatureBundle:
    base: pd.DataFrame
    percentiles: pd.DataFrame
    values: Mapping[str, pd.Series]
    counts: Mapping[str, pd.Series]
    partial: Mapping[str, pd.Series]
    meta: Mapping[str, ComponentMeta]


# --- 滚动分位 -----------------------------------------------------------------


def midrank_percentile(
    series: pd.Series,
    window: int,
    min_periods: int | None = None,
) -> tuple[pd.Series, pd.Series]:
    """模式 B：`100 × (小于 + 0.5 × 等于) ÷ 有效样本数`，样本严格早于当日。

    滑动窗口的取值区间为 `[t-window, t-1]`，实现上永远不读取下标 >= t 的元素。
    `min_periods=None` 表示满窗（= window）。
    """
    if window < 2:
        raise ValueError(f'window 必须 >= 2，收到 {window}')
    required = window if min_periods is None else int(min_periods)
    if required > window:
        raise ValueError(f'min_periods（{required}）不能大于 window（{window}）')

    values = np.asarray(series.to_numpy(dtype='float64', copy=False), dtype='float64')
    size = values.shape[0]
    pct = np.full(size, np.nan, dtype='float64')
    counts = np.zeros(size, dtype='float64')
    if size:
        # 前面补 window 个 NaN，使第 j 行窗口恰好覆盖 [t-window, t-1]（t = j），
        # 即"严格早于当日"；不足 window 行的部分自然由有效样本数体现。
        padded = np.concatenate([np.full(window, np.nan, dtype='float64'), values])
        windows = sliding_window_view(padded, window)[:size]
        targets = values
        valid = ~np.isnan(windows)
        sample_counts = valid.sum(axis=1)
        lower = np.count_nonzero((windows < targets[:, None]) & valid, axis=1)
        equal = np.count_nonzero((windows == targets[:, None]) & valid, axis=1)
        counts = sample_counts.astype('float64')
        with np.errstate(invalid='ignore', divide='ignore'):
            computed = 100.0 * (lower + 0.5 * equal) / np.where(sample_counts > 0, sample_counts, np.nan)
        computed = np.where(sample_counts >= required, computed, np.nan)
        pct = np.where(np.isnan(targets), np.nan, computed)
    return (
        pd.Series(pct, index=series.index, name=series.name),
        pd.Series(counts, index=series.index, name=series.name),
    )


def rank_percentile(
    series: pd.Series,
    window: int,
    min_periods: int | None = None,
) -> tuple[pd.Series, pd.Series]:
    """模式 A：`100 × (≤ 当前值的样本数 ÷ 有效样本数)`，样本含当日。

    `min_periods=None` 表示满窗（= window），与 `midrank_percentile` 一致；
    实际调用由配置层的 `resolve_min_periods` 给出（默认 252 -> 120）。
    """
    if window < 2:
        raise ValueError(f'window 必须 >= 2，收到 {window}')
    required = window if min_periods is None else int(min_periods)
    if required > window:
        raise ValueError(f'min_periods（{required}）不能大于 window（{window}）')
    pct = series.rolling(window, min_periods=required).rank(method='max', pct=True) * 100.0
    counts = series.rolling(window, min_periods=1).count()
    return pct.rename(series.name), counts.rename(series.name)


def rolling_percentile(
    series: pd.Series,
    window: int,
    min_periods: int | None,
    method: str,
) -> tuple[pd.Series, pd.Series]:
    if method == 'midrank_exclusive':
        return midrank_percentile(series, window, min_periods)
    if method == 'rank_inclusive':
        return rank_percentile(series, window, min_periods)
    raise ValueError(f'未知分位口径：{method!r}')


# --- 基础指标 -----------------------------------------------------------------


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    """取列；列缺失时按全 NaN 处理（缺失字段只让对应子指标不可用）。"""
    if name in frame.columns:
        return frame[name]
    return pd.Series(np.nan, index=frame.index, dtype='float64')


def ema_features(
    close: pd.Series,
    *,
    fast: int,
    slow: int,
    signal: int,
    policy: str = 'ffill_state',
    ffill_limit: int | None = 20,
    warmup_bars: int = 0,
) -> pd.DataFrame:
    """MACD 三段（DIF / DEA / 柱）与标准化柱。

    `policy='ffill_state'`：用前值填充后的序列作为 EMA 的递归状态输入，
    但原始 close 为缺失的行输出一律置 NaN（不发布填充值）。
    """
    if policy not in ('ffill_state', 'nan_propagate'):
        raise ValueError(f'未知价格缺口策略：{policy!r}')
    state = close.ffill(limit=ffill_limit) if policy == 'ffill_state' else close
    ema_fast = state.ewm(span=fast, adjust=False).mean()
    ema_slow = state.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = 2.0 * (dif - dea)
    hist_norm = hist / close.where(close > 0) * 100.0

    out = pd.DataFrame(
        {
            'macd': dif,
            'macd_signal': dea,
            'macd_hist': hist,
            'macd_hist_norm': hist_norm,
        }
    )
    missing = close.isna()
    if bool(missing.any()):
        # 两种策略都不对外发布缺失行的数值：差异只体现在 EMA 的递归状态上。
        out = out.mask(missing, np.nan, axis=0)
    if warmup_bars > 0:
        out.iloc[:warmup_bars, :] = np.nan
    return out


def volume_ratio(volume: pd.Series, window: int) -> pd.Series:
    """量比 = 当日成交量 / 过去 window 日均量（满窗，均值 <= 0 时置 NaN）。"""
    mean = volume.rolling(window, min_periods=window).mean()
    ratio = volume / mean.where(mean > 0)
    return ratio.rename('volume_ratio')


def return_series(close: pd.Series, horizon: int) -> pd.Series:
    """`close / close.shift(horizon) - 1`；shift 后非正或缺失则 NaN。"""
    base = close.shift(horizon)
    return (close / base.where(base > 0) - 1.0).rename(f'return_{horizon}d')


def ratio_features(frame: pd.DataFrame, *, limit_down_plus_one: bool = True) -> pd.DataFrame:
    """涨跌停比与涨跌家数占比（分母为 0 时输出 NaN，不做 0 除）。"""
    up = _column(frame, 'up_count')
    down = _column(frame, 'down_count')
    total = up + down
    up_ratio = (up / total.where(total > 0)).rename('up_ratio')
    down_ratio = (down / total.where(total > 0)).rename('down_ratio')
    limit_up = _column(frame, 'limit_up_count')
    limit_down = _column(frame, 'limit_down_count')
    denominator = limit_down + 1.0 if limit_down_plus_one else limit_down
    limit_ratio = (limit_up / denominator.where(denominator > 0)).rename('limit_up_down_ratio')
    return pd.DataFrame(
        {
            'up_ratio': up_ratio,
            'down_ratio': down_ratio,
            'limit_up_down_ratio': limit_ratio,
        }
    )


# --- 分量分位 -----------------------------------------------------------------


def blend_scales(
    results: Mapping[int, ScaleResult],
    component: ComponentSpec,
    *,
    min_coverage: float,
) -> BlendResult:
    """多尺度加权混合：只用可用尺度，权重重归一；覆盖不足则整体 NaN。"""
    windows = [scale.window for scale in component.scales]
    missing = [window for window in windows if window not in results]
    if missing:
        raise ValueError(f'分量 {component.name!r} 缺少尺度结果：{missing}')
    matrix = pd.DataFrame({window: results[window].pct for window in windows})
    weights = pd.Series({scale.window: float(scale.weight) for scale in component.scales}, dtype='float64')
    total_weight = float(weights.sum())
    if total_weight <= 0:
        raise ValueError(f'分量 {component.name!r} 的尺度权重之和必须 > 0')
    available = matrix.notna()
    available_weight = available.mul(weights, axis=1).sum(axis=1)
    numerator = matrix.fillna(0.0).mul(weights, axis=1).sum(axis=1)
    with np.errstate(invalid='ignore', divide='ignore'):
        value = numerator / available_weight.replace(0.0, np.nan)
    coverage = available_weight / total_weight
    value = value.where(coverage >= min_coverage)
    partial = available.sum(axis=1) < len(component.scales)
    primary = primary_scale(component)
    return BlendResult(
        value=value.rename(value_column(component.name, component.sign)),
        count=results[primary.window].count.rename(count_column(component.name, component.sign)),
        partial=partial.rename(f'{component.name}_scale_partial'),
    )


def component_sources(frame: pd.DataFrame, base: pd.DataFrame, config: SentimentConfig) -> dict[str, pd.Series]:
    """可用的分量来源（输入列 + 派生量）。"""
    primary_return = f'return_{int(config.windows.return_horizons[0])}d'
    sources: dict[str, pd.Series] = {
        'turnover_rate': _column(frame, 'turnover_rate'),
        'amount': _column(frame, 'amount'),
        'volume_ratio': base['volume_ratio'],
        'margin_net_buy': _column(frame, 'margin_net_buy'),
        'return': base[primary_return],
        'macd_hist_norm': base['macd_hist_norm'],
        'limit_up_down_ratio': base['limit_up_down_ratio'],
        'up_ratio': base['up_ratio'],
        'pcr': _column(frame, 'pcr'),
        'implied_volatility': _column(frame, 'implied_volatility'),
    }
    for horizon in config.windows.return_horizons:
        sources[f'return_{int(horizon)}d'] = base[f'return_{int(horizon)}d']
    return sources


def compute_base(frame: pd.DataFrame, config: SentimentConfig) -> pd.DataFrame:
    """基础指标表：MACD 三段、量比、各期收益、涨跌停比与家数占比。"""
    windows = config.windows
    quality = config.quality
    macd = ema_features(
        _column(frame, 'close'),
        fast=windows.macd_fast,
        slow=windows.macd_slow,
        signal=windows.macd_signal,
        policy=quality.price_gap_policy,
        ffill_limit=quality.price_gap_ffill_limit,
        warmup_bars=quality.warmup_bars,
    )
    base = pd.DataFrame(index=frame.index)
    base['volume_ratio'] = volume_ratio(_column(frame, 'volume'), windows.volume_ma)
    for horizon in windows.return_horizons:
        column = f'return_{int(horizon)}d'
        base[column] = return_series(_column(frame, 'close'), int(horizon))
    base = pd.concat([macd, base, ratio_features(frame)], axis=1)
    return base


def compute_features(frame: pd.DataFrame, config: SentimentConfig | None = None) -> FeatureBundle:
    """计算基础指标与所有分量的分位（含多尺度混合）。"""
    cfg = config or SentimentConfig()
    validate_config(cfg)
    base = compute_base(frame, cfg)
    sources = component_sources(frame, base, cfg)
    ratio = cfg.windows.min_periods_ratio
    columns: dict[str, pd.Series] = {}
    values: dict[str, pd.Series] = {}
    counts: dict[str, pd.Series] = {}
    partial: dict[str, pd.Series] = {}
    meta: dict[str, ComponentMeta] = {}

    for component in cfg.quality.components:
        try:
            series = sources[component.source]
        except KeyError as exc:
            available = sorted(sources)
            raise ValueError(
                f'分量 {component.name!r} 的来源 {component.source!r} 不可用；'
                f'可用来源：{available}'
            ) from exc
        lagged = series.shift(component.availability_lag) if component.availability_lag else series
        results: dict[int, ScaleResult] = {}
        methods: list[str] = []
        mins: list[int] = []
        for scale in component.scales:
            method = resolve_method(scale, cfg.quality.percentile_method)
            min_periods = resolve_min_periods(scale, method, ratio)
            pct, count = rolling_percentile(lagged, scale.window, min_periods, method)
            if component.sign == 'reverse':
                pct = 100.0 - pct
            results[scale.window] = ScaleResult(pct=pct, count=count)
            methods.append(method)
            mins.append(min_periods)
        blended = blend_scales(results, component, min_coverage=cfg.quality.scale_min_coverage)

        multi = len(component.scales) > 1
        for scale in component.scales:
            if component.sign == 'direct' or multi:
                name = scale_column(component.name, component.sign, scale.window)
                columns[name] = results[scale.window].pct
        if component.sign == 'reverse' or multi:
            columns[value_column(component.name, component.sign)] = blended.value
        columns[count_column(component.name, component.sign)] = blended.count
        for name in emitted_columns(component):
            if name not in columns:
                raise ValueError(f'分量 {component.name!r} 的列 {name!r} 未生成')

        values[component.name] = blended.value
        counts[component.name] = blended.count
        partial[component.name] = blended.partial
        meta[component.name] = ComponentMeta(
            name=component.name,
            group=component.group,
            sign=component.sign,
            horizon=component.horizon,
            source=component.source,
            windows=tuple(scale.window for scale in component.scales),
            weights=tuple(float(scale.weight) for scale in component.scales),
            min_periods=tuple(mins),
            methods=tuple(methods),
            primary_window=primary_scale(component).window,
            availability_lag=component.availability_lag,
        )

    ordered: dict[str, pd.Series] = {}
    for component in cfg.quality.components:
        for name in emitted_columns(component):
            ordered[name] = columns[name]
    percentiles = pd.DataFrame(ordered, index=frame.index)
    return FeatureBundle(
        base=base,
        percentiles=percentiles,
        values=values,
        counts=counts,
        partial=partial,
        meta=meta,
    )
