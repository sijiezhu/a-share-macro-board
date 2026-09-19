"""股债利差与百分位计算（纯函数，无 IO）。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from dateutil.relativedelta import relativedelta

from .config import PERCENTILE_MIN_SAMPLES, PERCENTILE_WINDOW_YEARS


def to_decimal(yield_pct: float) -> float:
    """百分比收益率转小数：2.0 (%) → 0.02。"""
    return float(yield_pct) / 100.0


def earnings_yield(pe_ttm: float | None) -> float | None:
    """盈利收益率 1/PE。PE 缺失、非正数或非法时返回 None。"""
    if pe_ttm is None:
        return None
    try:
        pe = float(pe_ttm)
    except (TypeError, ValueError):
        return None
    if pe <= 0:
        return None
    return 1.0 / pe


def equity_bond_spread(pe_ttm: float | None, yield_pct: float | None) -> float | None:
    """spread = 1 / PE_TTM - 中国10年期国债收益率（小数口径）。

    两个输入必须来自同一个数据日期；任一无效即返回 None，不做填充或插值。
    """
    ey = earnings_yield(pe_ttm)
    if ey is None or yield_pct is None:
        return None
    try:
        y = to_decimal(float(yield_pct))
    except (TypeError, ValueError):
        return None
    return ey - y


def to_bp(spread_decimal: float) -> float:
    """小数利差转基点：0.06 → 600。"""
    return spread_decimal * 10000.0


def to_percentage_points(spread_decimal: float) -> float:
    """小数利差转百分点：0.06 → 6.00。"""
    return spread_decimal * 100.0


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def build_spread_series(
    pe_series: Iterable[Sequence[object]],
    yield_series: Iterable[Sequence[object]],
) -> list[tuple[date, float]]:
    """按共同数据日期计算利差，不做前向填充、不做插值。

    输入为 (date, value) 序列。只有两个来源都存在该日期有效观测时才产出样本。
    """
    pe_map: dict[date, float] = {}
    for obs_date, value in pe_series:
        day = _as_date(obs_date)
        if day is not None and value is not None and float(value) > 0:
            pe_map[day] = float(value)
    out: list[tuple[date, float]] = []
    for obs_date, value in yield_series:
        day = _as_date(obs_date)
        if day is None or day not in pe_map or value is None:
            continue
        spread = equity_bond_spread(pe_map[day], float(value))
        if spread is not None:
            out.append((day, spread))
    out.sort(key=lambda item: item[0])
    return out


@dataclass(frozen=True)
class PercentileStats:
    value: float
    obs_date: date
    percentile: float | None
    sample_size: int
    window_full: bool
    window_start: date
    sample_start: date | None
    sample_end: date | None
    insufficient: bool

    @property
    def window_label(self) -> str:
        return f'{PERCENTILE_WINDOW_YEARS}年百分位' if self.window_full else '可用历史百分位'


def percentile_rank(current: float, history: Iterable[float]) -> float | None:
    """percentile = 100 × (小于当前值的数量 + 0.5 × 等于当前值的数量) ÷ 样本数。"""
    values = [float(v) for v in history]
    if not values:
        return None
    lower = sum(1 for v in values if v < current)
    equal = sum(1 for v in values if v == current)
    return 100.0 * (lower + 0.5 * equal) / len(values)


def percentile_stats(
    series: Sequence[tuple[date, float]],
    *,
    window_years: int = PERCENTILE_WINDOW_YEARS,
    min_samples: int = PERCENTILE_MIN_SAMPLES,
) -> PercentileStats | None:
    """基于过去 window_years 年、严格早于当前数据日期的有效样本计算百分位。"""
    if not series:
        return None
    current_date, current_value = series[-1]
    window_start = current_date - relativedelta(years=window_years)
    history = [
        (day, value)
        for day, value in series[:-1]
        if window_start <= day < current_date
    ]
    window_full = series[0][0] <= window_start
    sample_size = len(history)
    if sample_size < min_samples:
        return PercentileStats(
            value=current_value,
            obs_date=current_date,
            percentile=None,
            sample_size=sample_size,
            window_full=window_full,
            window_start=window_start,
            sample_start=history[0][0] if history else None,
            sample_end=history[-1][0] if history else None,
            insufficient=True,
        )
    rank = percentile_rank(current_value, [value for _, value in history])
    return PercentileStats(
        value=current_value,
        obs_date=current_date,
        percentile=rank,
        sample_size=sample_size,
        window_full=window_full,
        window_start=window_start,
        sample_start=history[0][0],
        sample_end=history[-1][0],
        insufficient=False,
    )


def change_in_bp(current: float | None, previous: float | None) -> float | None:
    """收益率/利差变动，单位基点。"""
    if current is None or previous is None:
        return None
    return (float(current) - float(previous)) * 100.0


def change_in_pct(current: float | None, previous: float | None) -> float | None:
    """价格、汇率等变动，单位百分比。"""
    if current is None or previous in (None, 0):
        return None
    return (float(current) / float(previous) - 1.0) * 100.0


def change_in_pp(current: float | None, previous: float | None) -> float | None:
    """比例指标变动，单位百分点。"""
    if current is None or previous is None:
        return None
    return float(current) - float(previous)
