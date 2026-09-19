"""曲线重采样与图表构建。

设计要点：**一个视图内只使用一种分辨率**。把日线/周线/月线混在同一张
线性时间轴上是误导的——近端日度点会被压缩到右侧很窄的一段，视觉上反而
比统一周线更差。因此默认按时间范围自动选择统一分辨率：

- 1 年以内 → 日线
- 1 ~ 3 年 → 周线
- 3 年以上 → 月线

另保留「近密远疏」模式（近 90 天日线、90 天~1 年周线、1~3 年月线、更早季度），
该模式会在图上标出分辨率切换位置；以及「原始日度」模式用于查看全部观测。

任何重采样都只保留区间内**最后一个真实观测**，不做平均、不插值。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import plotly.graph_objects as go

from .config import (
    CHART_MONTHLY_UNTIL_DAYS,
    CHART_RECENT_FULL_DAYS,
    CHART_WEEKLY_UNTIL_DAYS,
)

RESOLUTION_AUTO = '自动（按范围）'
RESOLUTION_DAILY = '日线'
RESOLUTION_WEEKLY = '周线'
RESOLUTION_MONTHLY = '月线'
RESOLUTION_TIERED = '近密远疏'
RESOLUTION_RAW = '原始日度'
RESOLUTION_OPTIONS = (
    RESOLUTION_AUTO,
    RESOLUTION_DAILY,
    RESOLUTION_WEEKLY,
    RESOLUTION_MONTHLY,
    RESOLUTION_TIERED,
    RESOLUTION_RAW,
)

# 自动模式的分辨率阈值（自然日）
DAILY_MAX_DAYS = 400
WEEKLY_MAX_DAYS = 3 * 365


@dataclass(frozen=True)
class ChartSpec:
    key: str
    title: str
    unit: str
    color: str
    decimals: int = 2
    zero_line: bool = False


CHART_SPECS: tuple[ChartSpec, ...] = (
    ChartSpec('us10y', '美国10年期国债收益率', '%', '#6ba8ff', 2),
    ChartSpec('cn10y', '中债10年期国债收益率', '%', '#5eead4', 2),
    ChartSpec('usdcny', '美元兑人民币（在岸即期）', 'CNY/USD', '#f5b13d', 4),
    ChartSpec('xauusd', '现货黄金参考价 (LBMA)', 'USD/金衡盎司', '#ffc857', 2),
    ChartSpec('adv_ratio', '沪深A股上涨家数占比', '%', '#ff4d87', 2),
    ChartSpec('hs300_20d', '沪深300近20个交易日涨跌幅', '%', '#b28dff', 2, zero_line=True),
)

SPREAD_SPEC = ChartSpec('spread', '沪深300股债利差', '个百分点', '#5eead4', 2)


def auto_resolution(span_in_days: int) -> str:
    """按视图跨度选择统一分辨率标签。"""
    if span_in_days <= DAILY_MAX_DAYS:
        return RESOLUTION_DAILY
    if span_in_days <= WEEKLY_MAX_DAYS:
        return RESOLUTION_WEEKLY
    return RESOLUTION_MONTHLY


def span_days(series: list[tuple[date, float]]) -> int:
    if len(series) < 2:
        return 0
    return (series[-1][0] - series[0][0]).days


def _daily_key(day: date) -> tuple:
    return ('d', day)


def _weekly_key(day: date) -> tuple:
    iso = day.isocalendar()
    return ('w', iso.year, iso.week)


def _monthly_key(day: date) -> tuple:
    return ('m', day.year, day.month)


def _quarterly_key(day: date) -> tuple:
    return ('q', day.year, (day.month - 1) // 3)


def _tiered_key(day: date, age_days: int) -> tuple:
    if age_days <= CHART_RECENT_FULL_DAYS:
        return _daily_key(day)
    if age_days <= CHART_WEEKLY_UNTIL_DAYS:
        return _weekly_key(day)
    if age_days <= CHART_MONTHLY_UNTIL_DAYS:
        return _monthly_key(day)
    return _quarterly_key(day)


def _reduce(series: list[tuple[date, float]], key_of) -> list[tuple[date, float]]:
    """按 key 归并，保留每个区间内最后一个真实观测。"""
    out: list[tuple[date, float]] = []
    last_key: tuple | None = None
    for day, value in series:
        key = key_of(day)
        if key == last_key and out:
            out[-1] = (day, value)
        else:
            out.append((day, value))
            last_key = key
    return out


def effective_resolution(option: str, series: list[tuple[date, float]]) -> str:
    """把「自动」解析为具体分辨率；其余选项原样返回。"""
    if option == RESOLUTION_AUTO:
        return auto_resolution(span_days(series))
    return option


def resample(
    series: list[tuple[date, float]],
    resolution: str,
    *,
    now: date | None = None,
) -> list[tuple[date, float]]:
    """按分辨率重采样。输入需按日期升序，输出保持升序且均为真实观测。"""
    if not series:
        return []
    if resolution in (RESOLUTION_RAW, RESOLUTION_DAILY):
        return list(series)
    if resolution == RESOLUTION_WEEKLY:
        return _reduce(series, _weekly_key)
    if resolution == RESOLUTION_MONTHLY:
        return _reduce(series, _monthly_key)
    if resolution == RESOLUTION_TIERED:
        reference = now or series[-1][0]
        return _reduce(series, lambda day: _tiered_key(day, (reference - day).days))
    raise ValueError(f'未知的曲线粒度选项：{resolution}')


def tiered_boundaries(now: date) -> list[tuple[date, str]]:
    """近密远疏模式下的分辨率切换位置（用于在图上标注）。"""
    return [
        (now - timedelta(days=CHART_RECENT_FULL_DAYS), f'{CHART_RECENT_FULL_DAYS}天：日线→周线'),
        (now - timedelta(days=CHART_WEEKLY_UNTIL_DAYS), '1年：周线→月线'),
        (now - timedelta(days=CHART_MONTHLY_UNTIL_DAYS), '3年：月线→季线'),
    ]


def slice_range(
    series: list[tuple[date, float]],
    days: int | None,
    *,
    now: date | None = None,
) -> list[tuple[date, float]]:
    """按时间范围截取（按自然日）；days=None 表示全部。"""
    if not series or days is None:
        return list(series)
    reference = now or series[-1][0]
    cutoff = reference - timedelta(days=days)
    return [(day, value) for day, value in series if day >= cutoff]


def _tick_format(span_in_days: int) -> str:
    if span_in_days <= 120:
        return '%-m/%-d'
    return '%Y-%m'


def build_figure(
    sampled: list[tuple[date, float]],
    spec: ChartSpec,
    *,
    height: int = 240,
    boundaries: list[tuple[date, str]] | None = None,
    show_hover: bool = True,
) -> go.Figure:
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=[day for day, _ in sampled],
            y=[value for _, value in sampled],
            mode='lines',
            line={'color': spec.color, 'width': 1.7},
            hovertemplate='%{x|%Y-%m-%d}<br>%{y:,.'
            + str(spec.decimals)
            + 'f} '
            + spec.unit
            + '<extra></extra>',
            hoverinfo='text' if show_hover else 'skip',
            name=spec.title,
        )
    )
    if spec.zero_line:
        figure.add_hline(y=0, line={'color': 'rgba(255,255,255,.25)', 'width': 1})
    if boundaries and sampled:
        first, last = sampled[0][0], sampled[-1][0]
        for position, label in boundaries:
            if first < position < last:
                # 注意：不要用 add_vline，plotly 在日期坐标轴上会计算标注位置并抛 TypeError。
                figure.add_shape(
                    type='line',
                    x0=position,
                    x1=position,
                    y0=0,
                    y1=1,
                    yref='paper',
                    line={'color': 'rgba(255,255,255,.22)', 'width': 1, 'dash': 'dot'},
                    layer='below',
                )
                figure.add_annotation(
                    x=position,
                    y=1,
                    yref='paper',
                    text=label,
                    showarrow=False,
                    xanchor='left',
                    yanchor='bottom',
                    font={'size': 9, 'color': '#8b909b'},
                )
    figure.update_layout(
        height=height,
        margin={'l': 4, 'r': 4, 't': 18, 'b': 4},
        xaxis={
            'showgrid': False,
            'showline': False,
            'ticks': 'outside',
            'nticks': 5,
            'tickformat': _tick_format(span_days(sampled)),
            'hoverformat': '%Y-%m-%d',
        },
        yaxis={
            'gridcolor': 'rgba(255,255,255,.08)',
            'zeroline': False,
            'ticksuffix': '' if spec.unit != '%' else '%',
            'nticks': 4,
        },
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)',
        font={'color': '#c9cdd6', 'size': 11},
        showlegend=False,
        hovermode='x unified',
    )
    return figure


def latest_text(sampled: list[tuple[date, float]], spec: ChartSpec) -> str:
    if not sampled:
        return '未接通'
    day, value = sampled[-1]
    if spec.unit == '%':
        body = f'{value:.{spec.decimals}f}%'
    elif spec.unit == 'CNY/USD':
        body = f'{value:.4f}'
    else:
        body = f'{value:,.{spec.decimals}f}'
    return f'{body} · {day.isoformat()}'
