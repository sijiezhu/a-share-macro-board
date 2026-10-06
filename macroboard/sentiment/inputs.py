"""输入校验、规范化与字段可用性画像（纯函数，无 IO、无网络）。

口径
- 只做无效值剔除，不做插值；默认也**不做前向填充**（`missing_policy='nan'`）。
- `volume == 0`（停牌）默认按缺失处理；`amount == 0` 视为真实极端缩量保留。
- `margin_net_buy` 是有符号流量，允许为负（不参与指数合成，仅采集与展示）。
- `margin_turnover`（融资买入额 + 融资偿还额）必须非负，负值视为非法值置为缺失。
- **两融完整性校验**：同一份「融资买入额 + 融资偿还额」观测若源站只发布了一部分，
  当日两个两融字段会一起偏低；命中校验的观测日把 `margin_turnover` 与 `margin_net_buy`
  **一并**置为缺失（同源双侧观测，不拆开、不取邻近值顶替），详见 `_exclude_incomplete_margin`。
- 缺失列默认按"不可用"处理（`on_missing_column='mark'`），不会抛异常。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import codes
from .config import SentimentConfig, derive_min_periods, validate_config

# 14 个输入字段中的 13 个数值列（date 单独处理）
SUPPORTED_NUMERIC_COLUMNS: tuple[str, ...] = (
    'close',
    'sh_close',
    'volume',
    'amount',
    'turnover_rate',
    'margin_net_buy',
    'margin_turnover',
    'up_count',
    'down_count',
    'limit_up_count',
    'limit_down_count',
    'pcr',
    'implied_volatility',
)

# 必须为正 / 必须非负的列（margin_net_buy 不在此列：允许为负）
POSITIVE_COLUMNS: tuple[str, ...] = ('close', 'sh_close', 'pcr', 'implied_volatility')
NON_NEGATIVE_COLUMNS: tuple[str, ...] = (
    'volume',
    'amount',
    'turnover_rate',
    'margin_turnover',
    'up_count',
    'down_count',
    'limit_up_count',
    'limit_down_count',
)

# 两融完整性校验作用的两列：同一份双侧观测解析出的两个字段，同源发布，必须成对剔除。
MARGIN_COMPLETENESS_COLUMNS: tuple[str, ...] = ('margin_turnover', 'margin_net_buy')


@dataclass(frozen=True, slots=True)
class MarginCompletenessDrop:
    """被两融完整性校验剔除的一个观测日（告警与页面据此说明原因）。"""

    day: pd.Timestamp
    ratio: float  # 当日 两融交易额 ÷ 成交额
    ratio_reference: float  # 前 window 个交易日该比值的中位数（严格早于当日）
    margin_reference: float  # 前 window 个交易日两融交易额的中位数（严格早于当日）
    threshold: float  # 触发阈值：两个比值同时 < threshold


@dataclass(frozen=True, slots=True)
class NormalizedInput:
    frame: pd.DataFrame
    warnings: tuple[str, ...]
    dropped_counts: Mapping[str, int]
    original_columns: tuple[str, ...]
    missing_columns: tuple[str, ...]
    margin_completeness: tuple[MarginCompletenessDrop, ...] = ()


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

    # 两融完整性校验放在 ffill 之后、winsorize 之前：判定是终局，
    # 不允许被白名单前向填充翻案（用上一日的值补当日，正是本项目对两融明确禁止的"顶替"）。
    frame, margin_completeness = _exclude_incomplete_margin(frame, cfg)
    if margin_completeness:
        for column in MARGIN_COMPLETENESS_COLUMNS:
            dropped[column] = dropped.get(column, 0) + len(margin_completeness)
        warnings.append(
            margin_completeness_warning(
                margin_completeness,
                window=int(cfg.quality.margin_completeness_window),
            )
        )

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
        margin_completeness=margin_completeness,
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


def _exclude_incomplete_margin(
    frame: pd.DataFrame,
    cfg: SentimentConfig,
) -> tuple[pd.DataFrame, tuple[MarginCompletenessDrop, ...]]:
    """两融完整性校验：比值与自身量级同时异常偏低的一日，两个两融字段整对置为缺失。

    口径理由（2026-09-27，实例见 `docs/DATA_SOURCES.md` 的两融条目）
    - 两融交易额与净买入来自**同一份**「融资买入额 + 融资偿还额」观测，源站只发布一部分时
      两者一起失真，因此成对剔除；不取邻近值顶替（与"不用 T−1 值顶替"同一条理由）。
    - 参照只用历史（`shift(1).rolling(...)`，严格早于当日），因此不引入未来函数。
    - **两条条件同时成立**才剔除，缺一不可：
        1) 相对成交额偏低 —— 杠杆活跃度相对大盘异常萎缩；
        2) 相对自身历史偏低 —— 两融交易额的量级也确实掉了。
      只留条件 1 会把「成交额骤增、两融尚未同步放大」的放量日（如恐慌放量）误判为发布不完整；
      只留条件 2 会在全市场杠杆活跃度整体降温时误伤（真实库实测有 0.57 的日子）。
    - `amount` 缺失或为 0 时不判定：没有分母就无从判断"占比是否异常"，
      宁可保留也不误剔除（`amount == 0` 在 §3.1 是真实的极端缩量，不是无效值）。
    - 判定是**相对**的而非绝对水平：整段区间都发布不完整时参照本身也低，不会命中；
      该情形靠 `docs/DATA_SOURCES.md` 的量级复核（成交额占比区间）人工发现。
    """
    quality = cfg.quality
    window = int(quality.margin_completeness_window)
    threshold = float(quality.margin_completeness_threshold)
    if threshold <= 0:  # 阈值 0 视为关闭校验
        return frame, ()
    amount = pd.to_numeric(frame['amount'], errors='coerce')
    margin = pd.to_numeric(frame['margin_turnover'], errors='coerce')
    # amount <= 0 的位置必须屏蔽，否则 0 除产生的 inf 会污染中位数参照
    ratio = (margin / amount.where(amount > 0)).replace([np.inf, -np.inf], np.nan)
    min_periods = derive_min_periods(window, cfg.windows.min_periods_ratio)
    ratio_reference = ratio.shift(1).rolling(window, min_periods=min_periods).median()
    margin_reference = margin.shift(1).rolling(window, min_periods=min_periods).median()
    hit = (
        ratio.notna()
        & ratio_reference.notna()
        & (ratio < ratio_reference * threshold)
        & margin_reference.notna()
        & (margin < margin_reference * threshold)
    )
    if not bool(hit.any()):
        return frame, ()
    drops = tuple(
        MarginCompletenessDrop(
            day=pd.Timestamp(frame['date'].iloc[position]),
            ratio=float(ratio.iloc[position]),
            ratio_reference=float(ratio_reference.iloc[position]),
            margin_reference=float(margin_reference.iloc[position]),
            threshold=threshold,
        )
        for position in np.flatnonzero(hit.to_numpy())
    )
    out = frame.copy()
    for column in MARGIN_COMPLETENESS_COLUMNS:
        out[column] = out[column].mask(hit)
    return out, drops


def margin_completeness_warning(
    drops: tuple[MarginCompletenessDrop, ...],
    *,
    window: int,
    limit: int = 3,
) -> str:
    """把剔除结果写成一条中文说明（日期 + 比值 + 参照），供告警与页面复用。"""
    detail = '；'.join(
        f'{item.day.strftime("%Y-%m-%d")}（两融交易额÷成交额 = {item.ratio:.4f}，'
        f'低于前 {window} 个交易日中位数 {item.ratio_reference:.4f} 的 {item.threshold:g} 倍，'
        f'且两融交易额低于自身中位数 {item.margin_reference:.4g} 的 {item.threshold:g} 倍）'
        for item in drops[:limit]
    )
    tail = f' 等共 {len(drops)} 天' if len(drops) > limit else ''
    return (
        f'两融完整性校验剔除 {len(drops)} 个观测日：{detail}{tail}'
        '（同一份双侧观测，margin_turnover 与 margin_net_buy 一并按缺失处理）。'
    )


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
