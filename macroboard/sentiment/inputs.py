"""输入校验、规范化与字段可用性画像（纯函数，无 IO、无网络）。

口径
- 只做无效值剔除，不做插值；默认也**不做前向填充**（`missing_policy='nan'`）。
- `volume == 0`（停牌）默认按缺失处理；`amount == 0` 视为真实极端缩量保留。
- `margin_net_buy` 是有符号流量，允许为负。
- 缺失列默认按"不可用"处理（`on_missing_column='mark'`），不会抛异常。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import codes
from .config import SentimentConfig, validate_config

# 12 个输入字段中的 11 个数值列（date 单独处理）
SUPPORTED_NUMERIC_COLUMNS: tuple[str, ...] = (
    'close',
    'volume',
    'amount',
    'turnover_rate',
    'margin_net_buy',
    'up_count',
    'down_count',
    'limit_up_count',
    'limit_down_count',
    'pcr',
    'implied_volatility',
)

# 必须为正 / 必须非负的列（margin_net_buy 不在此列：允许为负）
POSITIVE_COLUMNS: tuple[str, ...] = ('close', 'pcr', 'implied_volatility')
NON_NEGATIVE_COLUMNS: tuple[str, ...] = (
    'volume',
    'amount',
    'turnover_rate',
    'up_count',
    'down_count',
    'limit_up_count',
    'limit_down_count',
)


@dataclass(frozen=True, slots=True)
class NormalizedInput:
    frame: pd.DataFrame
    warnings: tuple[str, ...]
    dropped_counts: Mapping[str, int]
    original_columns: tuple[str, ...]
    missing_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ColumnStatus:
    name: str
    status: str
    valid_count: int
    total_count: int
    first_valid: pd.Timestamp | None
    last_valid: pd.Timestamp | None
    dropped_count: int = 0
    note: str = ''


@dataclass(frozen=True, slots=True)
class AvailabilityReport:
    columns: Mapping[str, ColumnStatus]
    unavailable: tuple[str, ...]

    def valid_count(self, name: str) -> int:
        status = self.columns.get(name)
        return 0 if status is None else status.valid_count

    def coverage(self, names: tuple[str, ...]) -> float:
        if not names:
            return 0.0
        usable = sum(1 for name in names if self.valid_count(name) > 0)
        return usable / len(names)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            f'{status.name}:{status.status}' for status in self.columns.values() if status.valid_count == 0
        )


def normalize_frame(df: pd.DataFrame, config: SentimentConfig | None = None) -> NormalizedInput:
    """排序、去重、类型转换、无效值剔除；返回规范化后的输入与告警。"""
    cfg = config or SentimentConfig()
    validate_config(cfg)
    if not isinstance(df, pd.DataFrame):
        raise ValueError(f'输入必须是 pandas.DataFrame，收到 {type(df)!r}')
    if 'date' not in df.columns:
        raise ValueError('输入至少需要 date 列')

    warnings: list[str] = []
    frame = df.copy()
    dates = pd.to_datetime(frame['date'], errors='coerce')
    if bool(dates.isna().any()):
        bad = int(dates.isna().sum())
        raise ValueError(f'date 列有 {bad} 个无法解析的值')
    frame['date'] = dates
    frame = frame.sort_values('date', kind='stable')

    duplicated = frame['date'].duplicated(keep=False)
    if bool(duplicated.any()):
        policy = cfg.quality.dup_policy
        if policy == 'raise':
            days = frame.loc[duplicated, 'date'].dt.strftime('%Y-%m-%d').unique().tolist()
            raise ValueError(f'date 列存在重复日期（dup_policy=raise）：{days[:5]}')
        keep = 'last' if policy == 'last' else 'first'
        dropped_dup = int(len(frame) - frame['date'].nunique())
        frame = frame.drop_duplicates(subset='date', keep=keep)
        warnings.append(f'date 存在重复日期：去重丢弃 {dropped_dup} 行（dup_policy={policy}）')

    frame = frame.reset_index(drop=True)
    original_columns = tuple(str(c) for c in df.columns)
    missing = tuple(col for col in SUPPORTED_NUMERIC_COLUMNS if col not in frame.columns)
    if missing:
        if cfg.quality.on_missing_column == 'raise':
            raise ValueError(f'缺少必需列：{list(missing)}')
        warnings.append(f'缺少列（按不可用处理）：{list(missing)}')
        for col in missing:
            frame[col] = np.nan

    dropped: dict[str, int] = {}
    for col in SUPPORTED_NUMERIC_COLUMNS:
        frame[col], dropped[col] = _clean_numeric(frame[col], col, cfg)
    for col, count in dropped.items():
        if count:
            warnings.append(f'{col}：{count} 个非法值已置为缺失')

    if cfg.quality.missing_policy == 'ffill':
        limit = cfg.quality.ffill_limit if cfg.quality.ffill_limit > 0 else None
        filled: list[str] = []
        for col in cfg.quality.ffill_whitelist:
            if col not in frame.columns:
                continue
            before = int(frame[col].isna().sum())
            frame[col] = frame[col].ffill(limit=limit)
            after = int(frame[col].isna().sum())
            if after < before:
                filled.append(f'{col}(+{before - after})')
        if filled:
            warnings.append(f'按 ffill 白名单填充：{", ".join(filled)}')

    if cfg.quality.winsorize_enabled:
        frame = _winsorize(frame, cfg)
        warnings.append(
            f'已启用因果缩尾（window={cfg.quality.winsorize_window}, '
            f'{cfg.quality.winsorize_lower_q}—{cfg.quality.winsorize_upper_q}）'
        )

    return NormalizedInput(
        frame=frame,
        warnings=tuple(warnings),
        dropped_counts=dropped,
        original_columns=original_columns,
        missing_columns=missing,
    )


def _clean_numeric(series: pd.Series, name: str, cfg: SentimentConfig) -> tuple[pd.Series, int]:
    values = pd.to_numeric(series, errors='coerce').astype('float64')
    values = values.replace([np.inf, -np.inf], np.nan)
    invalid = values.isna() & series.notna()
    if name in POSITIVE_COLUMNS:
        invalid |= values <= 0
    if name in NON_NEGATIVE_COLUMNS:
        invalid |= values < 0
    if name == 'volume' and cfg.quality.zero_volume_policy == 'nan':
        invalid |= values == 0
    dropped = int(invalid.sum())
    return values.mask(invalid), dropped


def _winsorize(frame: pd.DataFrame, cfg: SentimentConfig) -> pd.DataFrame:
    """因果缩尾：只用截至当日（含）的窗口分位，不触碰未来数据。"""
    window = cfg.quality.winsorize_window
    lower_q = cfg.quality.winsorize_lower_q
    upper_q = cfg.quality.winsorize_upper_q
    out = frame.copy()
    for col in SUPPORTED_NUMERIC_COLUMNS:
        series = out[col]
        lower = series.rolling(window, min_periods=2).quantile(lower_q)
        upper = series.rolling(window, min_periods=2).quantile(upper_q)
        out[col] = series.clip(lower=lower, upper=upper)
    return out


def profile_columns(normalized: NormalizedInput, *, min_samples: int | None = None) -> AvailabilityReport:
    """逐列可用性画像：ok / missing / all_nan / invalid_values / insufficient_history。"""
    frame = normalized.frame
    statuses: dict[str, ColumnStatus] = {}
    for col in SUPPORTED_NUMERIC_COLUMNS:
        series = frame[col]
        valid = series.dropna()
        dropped = int(normalized.dropped_counts.get(col, 0))
        if col in normalized.missing_columns:
            status = codes.STATUS_MISSING
        elif valid.empty:
            status = codes.STATUS_ALL_NAN
        elif min_samples is not None and len(valid) < min_samples:
            status = codes.STATUS_INSUFFICIENT_HISTORY
        elif dropped:
            status = codes.STATUS_INVALID_VALUES
        else:
            status = codes.STATUS_OK
        statuses[col] = ColumnStatus(
            name=col,
            status=status,
            valid_count=int(len(valid)),
            total_count=int(len(series)),
            first_valid=None if valid.empty else pd.Timestamp(frame['date'].iloc[valid.index[0]]),
            last_valid=None if valid.empty else pd.Timestamp(frame['date'].iloc[valid.index[-1]]),
            dropped_count=dropped,
        )
    unavailable = tuple(name for name, status in statuses.items() if status.valid_count == 0)
    return AvailabilityReport(columns=statuses, unavailable=unavailable)
