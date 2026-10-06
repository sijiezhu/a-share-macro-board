"""公开 API：analyze / compute_sentiment / sentiment_snapshot / output_columns。

本模块把 inputs -> features -> scoring 串成一次调用，输出固定 schema 的 DataFrame：
- schema 由配置生成（分量尺度可配），默认配置与规格书的列名一致；
- 分类列一律输出中性码值，`*_label` 列是展示层文案（可用 include_labels 关闭）；
- 纯函数、无 IO、无网络、无随机数；同输入必得同输出。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from . import codes, labels
from .config import SentimentConfig, validate_config
from .features import ComponentMeta, FeatureBundle, compute_features
from .inputs import (
    SUPPORTED_NUMERIC_COLUMNS,
    AvailabilityReport,
    MarginCompletenessDrop,
    normalize_frame,
    profile_columns,
)
from .registry import default_config, emitted_columns, price_ma_column
from .scoring import ScoreBundle, score_all

REQUIRED_COLUMNS: tuple[str, ...] = ('date',)
SUPPORTED_COLUMNS: tuple[str, ...] = ('date', *SUPPORTED_NUMERIC_COLUMNS)


@dataclass(frozen=True, slots=True)
class SentimentResult:
    frame: pd.DataFrame
    availability: AvailabilityReport
    component_meta: Mapping[str, ComponentMeta]
    warnings: tuple[str, ...]
    as_of: pd.Timestamp | None
    config: SentimentConfig
    margin_completeness: tuple[MarginCompletenessDrop, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """JSON 友好的摘要（用于缓存/序列化），不含任何建议性字段。"""
        return {
            'as_of': None if self.as_of is None else self.as_of.strftime('%Y-%m-%d'),
            'rows': int(len(self.frame)),
            'warnings': list(self.warnings),
            'unavailable_columns': list(self.availability.unavailable),
            'latest': sentiment_snapshot(self.frame, self.config),
            'config': self.config.to_dict(),
            'margin_completeness': [
                {
                    'day': item.day.strftime('%Y-%m-%d'),
                    'ratio': item.ratio,
                    'ratio_reference': item.ratio_reference,
                    'margin_reference': item.margin_reference,
                    'threshold': item.threshold,
                }
                for item in self.margin_completeness
            ],
        }


def output_columns(config: SentimentConfig | None = None) -> tuple[str, ...]:
    """按配置生成输出列（顺序固定，可复现）。"""
    cfg = config or SentimentConfig()
    validate_config(cfg)
    quality = cfg.quality
    windows = cfg.windows
    sentiment_scale = windows.sentiment_percentile_scales[0]
    momentum = windows.momentum

    columns: list[str] = [
        'date',
        'participation_index',
        'direction_index',
        'sentiment_index',
        f'sentiment_pct_{sentiment_scale.window}',
        f'sentiment_momentum_{int(momentum["sentiment"])}d',
        f'participation_momentum_{int(momentum["participation"])}d',
        f'direction_momentum_{int(momentum["direction"])}d',
        'participation_coverage',
        'direction_coverage',
        'participation_missing',
        'direction_missing',
        'state',
        'sh_close',
        'price_position',
        'participation_band',
        'direction_band',
    ]
    if quality.include_labels:
        columns.append('state_label')
        columns.append('price_position_label')
    columns += [
        'panic_score',
        'panic_hits',
        'panic_coverage',
        'panic_confidence',
        'panic_reasons',
        'panic_missing',
        'reversal_watch',
        'reversal_reasons',
        'reversal_missing',
        'reversal_block_reason',
        'macd_volume_state',
    ]
    if quality.include_labels:
        columns.append('macd_volume_state_label')
    columns += [
        'macd_bullish_divergence',
        'macd_golden_cross',
        'macd_dead_cross',
        'days_since_golden_cross',
        'macd',
        'macd_signal',
        'macd_hist',
        'macd_hist_norm',
        'volume_ratio',
        price_ma_column(int(windows.price_ma_window)),
        'price_ma_gap',
    ]
    columns += [f'return_{int(h)}d' for h in windows.return_horizons]
    columns += [
        'up_ratio',
        'down_ratio',
        'limit_up_down_ratio',
        'volume_shrink_ratio',
        'direction_chg_5d',
    ]
    for component in quality.components:
        columns.extend(emitted_columns(component))
    columns.append('data_quality')
    if quality.include_labels:
        columns.append('data_quality_label')
    columns += ['scale_partial_components', 'unavailable_columns']
    return tuple(columns)


DEFAULT_OUTPUT_COLUMNS: tuple[str, ...] = output_columns()


def analyze(df: pd.DataFrame, config: SentimentConfig | None = None) -> SentimentResult:
    """完整流程：规范化 -> 可用性画像 -> 基础指标/分位 -> 打分 -> 组装输出。"""
    cfg = config or default_config()
    validate_config(cfg)
    if df is None:
        raise ValueError('输入不能是 None；空表请传入带 date 列的空 DataFrame')
    source = df if len(df) else pd.DataFrame({'date': pd.Series(dtype='datetime64[ns]')})
    normalized = normalize_frame(source, cfg)
    frame = normalized.frame
    availability = profile_columns(normalized)
    bundle = compute_features(frame, cfg)
    scored = score_all(frame, cfg, bundle=bundle)
    out = _assemble(frame, cfg, bundle, scored, availability)
    warnings = (*normalized.warnings, *scored.warnings)
    return SentimentResult(
        frame=out,
        availability=availability,
        component_meta=bundle.meta,
        warnings=tuple(warnings),
        as_of=None if frame.empty else pd.Timestamp(frame['date'].iloc[-1]),
        config=cfg,
        margin_completeness=normalized.margin_completeness,
    )


def compute_sentiment(df: pd.DataFrame, config: SentimentConfig | None = None) -> pd.DataFrame:
    """便捷入口：只要 DataFrame。"""
    return analyze(df, config).frame


def _assemble(
    frame: pd.DataFrame,
    cfg: SentimentConfig,
    bundle: FeatureBundle,
    scored: ScoreBundle,
    availability: AvailabilityReport,
) -> pd.DataFrame:
    data: dict[str, pd.Series] = {'date': frame['date']}
    for name, series in scored.frame.items():
        data[name] = series
    percentiles = bundle.percentiles
    for name in percentiles.columns:
        data[name] = percentiles[name]
    base = bundle.base
    for name in base.columns:
        data[name] = base[name]

    if cfg.quality.include_labels:
        data['state_label'] = labels.label_series(data['state'], labels.STATE_LABELS)
        data['price_position_label'] = labels.label_series(
            data['price_position'], labels.POSITION_LABELS
        )
        data['macd_volume_state_label'] = labels.label_series(
            data['macd_volume_state'], labels.MACD_VOL_STATE_LABELS
        )
        data['data_quality_label'] = labels.label_series(data['data_quality'], labels.DATA_QUALITY_LABELS)
    data['unavailable_columns'] = pd.Series(
        [availability.unavailable] * len(frame), index=frame.index, dtype='object'
    )

    columns = output_columns(cfg)
    missing = [name for name in columns if name not in data]
    if missing:
        raise ValueError(f'输出组装缺少列（实现与 output_columns 不一致）：{missing}')
    return pd.DataFrame({name: data[name] for name in columns}, index=frame.index)


def sentiment_snapshot(
    frame: pd.DataFrame,
    config: SentimentConfig | None = None,
) -> dict[str, object]:
    """最新一个交易日的摘要，供页面卡片直接使用（无任何建议性字段）。"""
    if frame is None or len(frame) == 0:
        return {
            'as_of': None,
            'rows': 0,
            'state': codes.STATE_UNAVAILABLE,
            'state_label': labels.STATE_LABELS[codes.STATE_UNAVAILABLE],
            'price_position': codes.POS_UNKNOWN,
            'price_position_label': labels.POSITION_LABELS[codes.POS_UNKNOWN],
            'sh_close': None,
            'price_ma': None,
            'price_ma_column': None,
            'price_ma_window': None,
            'price_ma_gap': None,
        }
    last = frame.iloc[-1]
    pct_columns = [name for name in frame.columns if name.startswith('sentiment_pct_')]
    sentiment_pct_column = pct_columns[0] if pct_columns else None
    # 均线列名带窗口后缀（price_ma_60）；config 缺省时从列名反查，排除乖离列 price_ma_gap
    ma_columns = [
        name for name in frame.columns if name.startswith('price_ma_') and name != 'price_ma_gap'
    ]
    price_ma_column_name = ma_columns[0] if ma_columns else None
    snapshot: dict[str, object] = {
        'as_of': pd.Timestamp(last['date']).strftime('%Y-%m-%d'),
        'rows': int(len(frame)),
        'participation_index': _maybe_float(last.get('participation_index')),
        'direction_index': _maybe_float(last.get('direction_index')),
        'sentiment_index': _maybe_float(last.get('sentiment_index')),
        'sentiment_pct': _maybe_float(last.get(sentiment_pct_column)) if sentiment_pct_column else None,
        'sentiment_pct_column': sentiment_pct_column,
        'participation_coverage': _maybe_float(last.get('participation_coverage')),
        'direction_coverage': _maybe_float(last.get('direction_coverage')),
        'participation_missing': list(last.get('participation_missing') or ()),
        'direction_missing': list(last.get('direction_missing') or ()),
        'state': last.get('state'),
        'state_label': labels.STATE_LABELS.get(str(last.get('state')), str(last.get('state'))),
        'price_position': last.get('price_position'),
        'price_position_label': labels.POSITION_LABELS.get(
            str(last.get('price_position')), str(last.get('price_position'))
        ),
        'sh_close': _maybe_float(last.get('sh_close')),
        'price_ma': _maybe_float(last.get(price_ma_column_name)),
        'price_ma_column': price_ma_column_name,
        'price_ma_window': int(config.windows.price_ma_window) if config is not None else None,
        'price_ma_gap': _maybe_float(last.get('price_ma_gap')),
        'participation_band': last.get('participation_band'),
        'direction_band': last.get('direction_band'),
        'panic_score': _maybe_float(last.get('panic_score')),
        'panic_hits': None if pd.isna(last.get('panic_hits')) else int(last.get('panic_hits')),
        'panic_coverage': _maybe_float(last.get('panic_coverage')),
        'panic_confidence': _maybe_float(last.get('panic_confidence')),
        'panic_reasons': list(last.get('panic_reasons') or ()),
        'panic_reasons_text': labels.label_history(last.get('panic_reasons'), labels.PANIC_REASON_LABELS),
        'panic_missing': list(last.get('panic_missing') or ()),
        'reversal_watch': bool(last.get('reversal_watch', False)),
        'reversal_reasons': list(last.get('reversal_reasons') or ()),
        'reversal_reasons_text': labels.label_history(last.get('reversal_reasons'), labels.REV_REASON_LABELS),
        'reversal_block_reason': _block_reason(last),
        'reversal_block_reason_text': None
        if _block_reason(last) is None
        else labels.REV_BLOCK_REASON_LABELS.get(str(_block_reason(last)), str(_block_reason(last))),
        'macd_volume_state': last.get('macd_volume_state'),
        'macd_volume_state_label': labels.MACD_VOL_STATE_LABELS.get(
            str(last.get('macd_volume_state')), str(last.get('macd_volume_state'))
        ),
        'macd_bullish_divergence': None
        if pd.isna(last.get('macd_bullish_divergence'))
        else bool(last.get('macd_bullish_divergence')),
        'data_quality': last.get('data_quality'),
        'data_quality_label': labels.DATA_QUALITY_LABELS.get(
            str(last.get('data_quality')), str(last.get('data_quality'))
        ),
        'unavailable_columns': list(last.get('unavailable_columns') or ()),
        'missing_components': list(last.get('participation_missing') or ())
        + list(last.get('direction_missing') or ()),
    }
    if config is not None:
        snapshot['component_scales'] = {
            component.name: [scale.window for scale in component.scales]
            for component in config.quality.components
        }
        snapshot['percentile_method'] = config.quality.percentile_method
    return snapshot


def _block_reason(row: pd.Series) -> str | None:
    """门控原因：None 表示未触发任何门控（不是"缺失"）。"""
    value = row.get('reversal_block_reason')
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    text = str(value)
    return None if text in {'nan', 'None'} else text


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number
