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

多张曲线**同列**（`build_stacked_figure`）时共用一条时间轴，因此必须共用一种分辨率
（由 `unified_resolution` 按各序列的整体跨度选定）；悬停跨子图由 plotly 的
`hoversubplots='axis'` 承担，各子图仍然只画自己的真实观测。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import plotly.graph_objects as go

from .config import (
    CHART_MONTHLY_UNTIL_DAYS,
    CHART_RECENT_FULL_DAYS,
    CHART_WEEKLY_UNTIL_DAYS,
)
from .sentiment import codes
from .sentiment.labels import POSITION_LABELS

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

# 四象限阈值（与 macroboard/sentiment 的默认阈值一致，仅用于绘图参考线）
QUADRANT_LOW = 40.0
QUADRANT_HIGH = 60.0

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
    hline_values: tuple[float, ...] = ()
    # 固定纵轴范围（如 0—100 的分数：纵轴位置本身有含义，不能被自适应拉伸）；
    # None = 按数据自适应（价格、收益率这类）。
    y_range: tuple[float, float] | None = None


# 长线指标：宏观利率/汇率/金价，用于观察大周期环境。
LONG_TERM_CHART_SPECS: tuple[ChartSpec, ...] = (
    ChartSpec('us10y', '美国10年期国债收益率', '%', '#6ba8ff', 2),
    ChartSpec('cn10y', '中债10年期国债收益率', '%', '#5eead4', 2),
    ChartSpec('usdcny', '美元兑人民币（在岸即期）', 'CNY/USD', '#f5b13d', 4),
    ChartSpec('xauusd', '现货黄金参考价 (LBMA)', 'USD/金衡盎司', '#ffc857', 2),
)

# 短线指标：A股市场宽度与近期动量。
SHORT_TERM_CHART_SPECS: tuple[ChartSpec, ...] = (
    ChartSpec('adv_ratio', '沪深A股上涨家数占比', '%', '#ff4d87', 2),
    ChartSpec('hs300_20d', '沪深300近20个交易日涨跌幅', '%', '#b28dff', 2, zero_line=True),
)

# 全部曲线规格（长线在前、短线在后），供不区分分组时按原顺序遍历使用。
CHART_SPECS: tuple[ChartSpec, ...] = LONG_TERM_CHART_SPECS + SHORT_TERM_CHART_SPECS

SPREAD_SPEC = ChartSpec('spread', '沪深300股债利差', '个百分点', '#5eead4', 2)

# 情绪指数的三条曲线（0—100，用 40/60 阈值线标出四象限边界）
SENTIMENT_SPECS: tuple[ChartSpec, ...] = (
    ChartSpec(
        'sentiment_index', '综合情绪指数', '分', '#b28dff', 1,
        hline_values=(40.0, 60.0), y_range=(0.0, 100.0),
    ),
    ChartSpec(
        'participation_index', '参与度指数', '分', '#5eead4', 1,
        hline_values=(40.0, 60.0), y_range=(0.0, 100.0),
    ),
    ChartSpec(
        'direction_index', '方向指数', '分', '#ffc857', 1,
        hline_values=(40.0, 60.0), y_range=(0.0, 100.0),
    ),
)

# 上证指数：高位/低位判定的标的（当日收盘 vs 60 日均线），放在情绪曲线之上同列展示。
# 纵轴按数据自适应——它是指数点位，不是 0—100 的分数，也不带 40/60 阈值线。
SH_INDEX_SPEC = ChartSpec('sh_close', '上证指数', '点', '#8ab4ff', 2)

# 情绪区块同列展示的四条曲线：上证指数在最上，三条指数曲线在下。
SENTIMENT_CHART_SPECS: tuple[ChartSpec, ...] = (SH_INDEX_SPEC, *SENTIMENT_SPECS)


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


def unified_resolution(option: str, series_list: Sequence[list[tuple[date, float]]]) -> str:
    """同列多图共用时间轴时的统一分辨率：按全部序列的**整体跨度**判定。

    同一条时间轴上不能混用不同分辨率，所以粒度只能选一个：取跨度最大的序列
    所对应的粒度（跨度越大粒度越粗）。这里只用各序列的首末日期，不代表任何
    合成或补齐——每条曲线仍各自重采样，各自只保留真实观测。
    """
    days = [day for series in series_list for day, _value in series]
    if not days:
        return effective_resolution(option, [])
    return effective_resolution(option, [(min(days), 0.0), (max(days), 0.0)])


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


def _line_trace(
    sampled: list[tuple[date, float]],
    spec: ChartSpec,
    *,
    show_hover: bool = True,
    yaxis: str = 'y',
) -> go.Scatter:
    """一条曲线的 trace（单图与同列多图共用同一套样式与悬停文案）。"""
    return go.Scatter(
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
        xaxis='x',
        yaxis=yaxis,
    )


def _hline_shape(figure: go.Figure, yref: str, value: float, *, line: dict, layer=None) -> None:
    """横跨整幅宽度的水平线（形状参数与 plotly 的 add_hline 一致）。"""
    figure.add_shape(
        type='line',
        xref='x domain',
        x0=0,
        x1=1,
        yref=yref,
        y0=value,
        y1=value,
        line=line,
        layer=layer,
    )


def _reference_lines(
    figure: go.Figure, spec: ChartSpec, *, row: int | None = None, yref: str | None = None
) -> None:
    """零线与阈值参考线；单图用 add_hline，同列多图时用显式 shape 落在各自的 y 轴上。"""
    if yref is not None:
        if spec.zero_line:
            _hline_shape(figure, yref, 0.0, line={'color': 'rgba(255,255,255,.25)', 'width': 1})
        for value in spec.hline_values:
            _hline_shape(
                figure,
                yref,
                value,
                line={'color': 'rgba(255,255,255,.18)', 'width': 1, 'dash': 'dot'},
                layer='below',
            )
        return
    placement = {} if row is None else {'row': row, 'col': 1}
    if spec.zero_line:
        figure.add_hline(
            y=0, line={'color': 'rgba(255,255,255,.25)', 'width': 1}, **placement
        )
    for value in spec.hline_values:
        figure.add_hline(
            y=value,
            line={'color': 'rgba(255,255,255,.18)', 'width': 1, 'dash': 'dot'},
            layer='below',
            **placement,
        )


def _boundaries(
    figure: go.Figure,
    span: tuple[date, date] | None,
    boundaries: list[tuple[date, str]] | None,
    *,
    xref: str = 'x',
    y_domain: tuple[float, float] = (0.0, 1.0),
    annotate: bool = True,
) -> None:
    """「近密远疏」的分辨率切换位置；竖线高度用纸面坐标 `y_domain` 给出。

    同列多图共用一条时间轴时传 (0, 1)：切换位置是整条轴上的位置，
    一条竖线纵穿所有图（与悬停竖线一致），标签只标一次。
    """
    if not boundaries or span is None:
        return
    first, last = span
    for position, label in boundaries:
        if first < position < last:
            # 注意：不要用 add_vline，plotly 在日期坐标轴上会计算标注位置并抛 TypeError。
            figure.add_shape(
                type='line',
                x0=position,
                x1=position,
                y0=y_domain[0],
                y1=y_domain[1],
                xref=xref,
                yref='paper',
                line={'color': 'rgba(255,255,255,.22)', 'width': 1, 'dash': 'dot'},
                layer='below',
            )
            if annotate:
                figure.add_annotation(
                    x=position,
                    y=y_domain[1],
                    xref=xref,
                    yref='paper',
                    text=label,
                    showarrow=False,
                    xanchor='left',
                    yanchor='bottom',
                    font={'size': 9, 'color': '#8b909b'},
                )


def build_figure(
    sampled: list[tuple[date, float]],
    spec: ChartSpec,
    *,
    height: int = 240,
    boundaries: list[tuple[date, str]] | None = None,
    show_hover: bool = True,
) -> go.Figure:
    figure = go.Figure()
    figure.add_trace(_line_trace(sampled, spec, show_hover=show_hover))
    _reference_lines(figure, spec)
    _boundaries(
        figure, (sampled[0][0], sampled[-1][0]) if sampled else None, boundaries
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
            'range': list(spec.y_range) if spec.y_range else None,
        },
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)',
        font={'color': '#c9cdd6', 'size': 11},
        showlegend=False,
        hovermode='x unified',
    )
    return figure


def _stack_vertical_spacing(rows: int) -> float:
    """堆叠各图的行间距：够放图名，又不把每张图压得太扁。"""
    if rows <= 1:
        return 0.0
    return min(0.12, max(0.06, 0.3 / (rows - 1)))


def _stack_domains(rows: int, spacing: float) -> list[tuple[float, float]]:
    """自上而下各图的纵向范围（纸面坐标 0—1），口径同 make_subplots 的 vertical_spacing。"""
    if rows <= 0:
        return []
    height = (1.0 - spacing * (rows - 1)) / rows
    return [
        (1.0 - (index + 1) * height - index * spacing, 1.0 - index * (height + spacing))
        for index in range(rows)
    ]


def position_bands(positions: Sequence[tuple[date, str]]) -> list[tuple[date, date, str]]:
    """逐日价格位置 -> 连续背景带 (起, 止, 位置码)，相邻同位置合并。

    传进来的必须是**画在图上那些点**的 (日期, 位置码)（与曲线同日期、同粒度），
    因此这里只做合并和分段，不补日期、不外推：一段从该位置第一个观测日起、
    到下一个不同位置的观测日止（最后一段止于最后一个观测日）。位置未知不铺底色。
    """
    if not positions:
        return []
    last_day = positions[-1][0]
    bands: list[tuple[date, date, str]] = []
    start, code = positions[0]
    for day, current in positions[1:]:
        if current != code:
            if code in POSITION_BAND_FILL:
                bands.append((start, day, code))
            start, code = day, current
    if code in POSITION_BAND_FILL:
        bands.append((start, last_day, code))
    return bands


def _stack_axis_id(row: int) -> str:
    """第 row 张图（从 1 起、自上而下）的 y 轴名。"""
    return 'y' if row == 1 else f'y{row}'


def build_stacked_figure(
    panels: Sequence[tuple[ChartSpec, list[tuple[date, float]], list[tuple[date, str]] | None]],
    *,
    height_per_panel: int = 150,
    bands: Mapping[str, Sequence[tuple[date, date, str]]] | None = None,
    empty_text: str = '样本不足',
) -> go.Figure:
    """多张曲线**同列**（纵向堆叠、共用一条时间轴）：悬停时同时给出各图的值。

    `panels` 为 (规格, 重采样后的序列, 该图的切换位置) 三元组，顺序即从上到下。
    各图必须由调用方按**同一种分辨率**重采样（见 `unified_resolution`）：
    共用一条时间轴就不能混用不同粒度。各图仍只画自己的真实观测，不做插值。
    纵轴范围逐图取自 `ChartSpec.y_range`（分数固定 0—100，指数点位自适应）。

    `bands` 是按图给背景带（键＝`ChartSpec.key`，值为 (起, 止, 位置码)），
    用于把价格位置铺成底色（见 `position_bands`）；没给的图不铺底色。

    各图共用**同一个 x 轴对象**（各 y 轴都 `anchor` 到它），而不是几个用 `matches`
    互相关联的轴。这不是等价写法：plotly 画悬停竖线时，线的高度取该 x 轴的
    「反向轴域」（`_counterDomain*`，即挂在该轴上的所有 y 轴域之并集）。共用同一个
    x 轴 → 并集就是所有图的总高度，一条竖线纵穿所有图；用 `matches` 的话每张图各有
    各的轴，竖线只会画在光标所在的那一张图里。

    悬停读数用 `hovermode='x unified'`：所有曲线的取值在一次悬停里一起给出，
    合并在一个提示框里（按图从上到下排列）。悬停竖线纵穿所有图（见上），
    悬停横线只画在光标所在的那一张图里（各图纵轴刻度不同，横线跨图会被误读）。
    """
    rows = len(panels)
    domains = _stack_domains(rows, _stack_vertical_spacing(rows))
    boundaries = next((bounds for _spec, _sampled, bounds in panels if bounds), None)
    figure = go.Figure()
    span: tuple[date, date] | None = None
    for index, (spec, sampled, _bounds) in enumerate(panels):
        domain = domains[index]
        figure.add_annotation(  # 图名（空图也要有名字）
            x=0.0,
            y=domain[1],
            xref='paper',
            yref='paper',
            text=spec.title,
            showarrow=False,
            xanchor='left',
            yanchor='bottom',
            font={'size': 11, 'color': '#c9cdd6'},
        )
        if not sampled:
            figure.add_annotation(
                x=0.5,
                y=sum(domain) / 2,
                xref='paper',
                yref='paper',
                text=empty_text,
                showarrow=False,
                font={'size': 11, 'color': '#8b909b'},
            )
            continue
        for start_day, end_day, code in (bands or {}).get(spec.key, ()):
            figure.add_shape(  # 位置底色：铺满这张图的整个高度，压在曲线下面
                type='rect',
                x0=start_day,
                x1=end_day,
                y0=domain[0],
                y1=domain[1],
                xref='x',
                yref='paper',
                fillcolor=POSITION_BAND_FILL[code],
                line={'width': 0},
                layer='below',
            )
        figure.add_trace(_line_trace(sampled, spec, yaxis=_stack_axis_id(index + 1)))
        _reference_lines(figure, spec, yref=_stack_axis_id(index + 1))
        first, last = sampled[0][0], sampled[-1][0]
        span = (first, last) if span is None else (min(span[0], first), max(span[1], last))
    _boundaries(figure, span, boundaries)  # 切换位置是整条轴上的位置：一条线纵穿所有图

    tick_span = max((span_days(sampled) for _spec, sampled, _bound in panels), default=0)
    yaxes: dict[str, dict] = {}
    for index, (spec, _sampled, _bound) in enumerate(panels):
        yaxes['yaxis' if index == 0 else f'yaxis{index + 1}'] = {
            'anchor': 'x',
            'domain': list(domains[index]),
            'gridcolor': 'rgba(255,255,255,.08)',
            'zeroline': False,
            'ticksuffix': '' if spec.unit != '%' else '%',
            'nticks': 4,
            'range': list(spec.y_range) if spec.y_range else None,
            # 悬停横线：只画在光标所在的那张图里（各图纵轴刻度不同，横线跨图会误导），
            # 长度取该 y 轴的反向轴域＝这张图的整幅宽度。
            'showspikes': True,
            'spikesnap': 'cursor',
            'spikemode': 'across',
            'spikethickness': 1,
            'spikecolor': 'rgba(255,255,255,.45)',
            'spikedash': 'dot',
            # 指数点位的刻度是四位数字，靠 automargin 自动加宽左边距，别让数字被裁掉
            'automargin': True,
        }
    figure.update_layout(
        height=max(height_per_panel * rows, 240),
        margin={'l': 4, 'r': 4, 't': 22, 'b': 4},
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)',
        font={'color': '#c9cdd6', 'size': 11},
        showlegend=False,
        hovermode='x unified',
        hoversubplots='axis',  # 悬停跨子图：同列的图一起给出同一日期的读数
        xaxis={
            'anchor': _stack_axis_id(rows),  # 共用的 x 轴画在最下面那张图的底部
            'domain': [0.0, 1.0],
            'showgrid': False,
            'showline': False,
            'ticks': 'outside',
            'nticks': 5,
            'tickformat': _tick_format(tick_span),
            'hoverformat': '%Y-%m-%d',
            # 悬停竖线：`spikesnap='cursor'` 让线停在光标所在的像素上（按数据点吸附的话，
            # 各图的数据点日期可能不同，各段线会错开一两个交易日）；`spikemode='across'`
            # 让线高度取该 x 轴的反向轴域并集＝三张图的总高度（见上面的说明）。
            'showspikes': True,
            'spikesnap': 'cursor',
            'spikemode': 'across',
            'spikethickness': 1,
            'spikecolor': 'rgba(255,255,255,.45)',
            'spikedash': 'dot',
        },
        **yaxes,
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


# 四象限图的轴范围（参与度/方向都是 0—100 的分数）与象限文案距轴边的内缩量。
QUADRANT_AXIS: tuple[float, float] = (0.0, 100.0)
QUADRANT_LABEL_INSET = 6.0

QUADRANT_LABELS: tuple[tuple[float, float, str], ...] = (
    # (参与度下界, 方向下界, 文案) —— 与算法层的四象限命名一致，仅作图上标注。
    # 保持四象限级文案：一个象限内现在可能对应两个复合状态（高位/低位），
    # 位置信息由点形 + 图例承担，不要把这里改成 8 条。
    (QUADRANT_HIGH, QUADRANT_HIGH, '贪婪/主升'),
    (QUADRANT_HIGH, 0.0, '恐慌抛售'),
    (0.0, 0.0, '缩量阴跌'),
    (0.0, QUADRANT_HIGH, '温和回暖'),
)

# 价格位置 -> (图例名, 点形, 描边宽度)。空位置（未知）也画，只是换成空心菱形。
POSITION_MARKERS: dict[str, tuple[str, str, int]] = {
    codes.POS_HIGH: ('高位（收盘 > 60 日均线）', 'circle', 1),
    codes.POS_LOW: ('低位（收盘 ≤ 60 日均线）', 'circle-open', 2),
    codes.POS_UNKNOWN: ('位置未知（均线样本不足）', 'diamond-open', 2),
}

# 价格位置的背景底色：高位红、低位绿（低饱和，只作背景不抢曲线）。
# 位置未知的日期**不铺底色**——留白本身就是在说"这段没有位置可判定"。
POSITION_BAND_FILL: dict[str, str] = {
    codes.POS_HIGH: 'rgba(255,107,139,.13)',
    codes.POS_LOW: 'rgba(94,234,212,.11)',
}

# 由浅到深的全局渐变（与"箭头指向未来"同一语义：只表示新旧）。(r, g, b, alpha)
_GRADIENT_START = (178, 141, 255, 0.35)
_GRADIENT_END = (255, 200, 87, 0.95)


def _gradient_color(fraction: float) -> str:
    """窗口内位置（0—1）-> 渐变色，各通道按比例线性插值。"""
    ratio = min(max(float(fraction), 0.0), 1.0)
    start, end = _GRADIENT_START, _GRADIENT_END
    red, green, blue = (round(start[i] + (end[i] - start[i]) * ratio) for i in range(3))
    alpha = start[3] + (end[3] - start[3]) * ratio
    return f'rgba({red},{green},{blue},{alpha:.2f})'


def _position_of(point: tuple[date, float, float, str]) -> str:
    """位置码值；老数据缺该字段时按"位置未知"处理。"""
    return point[3] if len(point) > 3 and point[3] in POSITION_MARKERS else codes.POS_UNKNOWN


def build_quadrant_figure(
    points: list[tuple[date, float, float, str]],
    *,
    low: float = QUADRANT_LOW,
    high: float = QUADRANT_HIGH,
    height: int = 380,
    max_points: int = 10,
) -> go.Figure:
    """四象限走势图：x = 参与度指数，y = 方向指数，点形 = 价格位置。

    只画最近 `max_points`（默认 10）个交易日的点，相邻点用箭头连接，
    **箭头由过去指向未来**，颜色随之由浅到深；只画真实观测，不做任何插值。
    `points` 为 (日期, 参与度, 方向, 价格位置码值) 升序序列。

    位置只用**点形**（实心 ● 高位 / 空心 ○ 低位 / ◇ 位置未知），颜色仍只表示"新旧"：
    一个视觉通道只承载一个含义，避免"深色=高位"这类会被误读的耦合。
    """
    recent = points[-max_points:] if max_points and max_points > 0 else list(points)
    figure = go.Figure()
    # 象限底色（先画背景，再叠数据）
    figure.add_shape(type='rect', x0=0, x1=low, y0=0, y1=low, fillcolor='rgba(120,140,180,.10)', line={'width': 0}, layer='below')
    figure.add_shape(type='rect', x0=high, x1=100, y0=0, y1=low, fillcolor='rgba(255,107,139,.12)', line={'width': 0}, layer='below')
    figure.add_shape(type='rect', x0=0, x1=low, y0=high, y1=100, fillcolor='rgba(94,234,212,.12)', line={'width': 0}, layer='below')
    figure.add_shape(type='rect', x0=high, x1=100, y0=high, y1=100, fillcolor='rgba(178,141,255,.12)', line={'width': 0}, layer='below')
    for value in (low, high):
        figure.add_hline(y=value, line={'color': 'rgba(255,255,255,.18)', 'width': 1, 'dash': 'dot'})
        # 用 add_shape 而不是 add_vline：后者在部分 plotly 版本会对坐标轴做日期推算而报错
        figure.add_shape(
            type='line',
            x0=value,
            x1=value,
            y0=0,
            y1=1,
            yref='paper',
            line={'color': 'rgba(255,255,255,.18)', 'width': 1, 'dash': 'dot'},
            layer='below',
        )

    if recent:
        order = list(range(len(recent)))
        # 全局渐变（浅 -> 深）按"点在窗口内的序号"取色，与位置无关
        gradient = [
            _gradient_color(index / max(len(recent) - 1, 1)) for index in order
        ]

        def marker_style(indices: list[int]) -> dict[str, object]:
            """逐点颜色/尺寸显式给出：拆成多条 trace 后仍保持全局渐变。"""
            return {
                'size': [6.0 + 0.5 * index for index in indices],
                'color': [gradient[index] for index in indices],
                'showscale': False,
            }

        for position, (name, symbol, line_width) in POSITION_MARKERS.items():
            indices = [index for index in order if _position_of(recent[index]) == position]
            figure.add_trace(
                go.Scatter(
                    x=[recent[index][1] for index in indices],
                    y=[recent[index][2] for index in indices],
                    mode='markers',
                    marker={
                        **marker_style(indices),
                        'symbol': symbol,
                        'line': {'color': 'rgba(27,29,35,.85)', 'width': line_width},
                    },
                    customdata=[
                        [recent[index][0].isoformat(), POSITION_LABELS.get(position, position)]
                        for index in indices
                    ],
                    hovertemplate=(
                        '%{customdata[0]}<br>参与度 %{x:.1f} · 方向 %{y:.1f}'
                        ' · %{customdata[1]}<extra></extra>'
                    ),
                    name=name,
                )
            )
        # 相邻交易日之间用箭头连接：从旧点指向新点（x/ax 为数据坐标）
        for index in range(len(recent) - 1):
            older = recent[index]
            newer = recent[index + 1]
            alpha = 0.35 + 0.6 * (index + 1) / max(len(recent) - 1, 1)
            figure.add_annotation(
                x=newer[1],
                y=newer[2],
                ax=older[1],
                ay=older[2],
                xref='x',
                yref='y',
                axref='x',
                ayref='y',
                text='',
                showarrow=True,
                arrowhead=2,
                arrowsize=1.0,
                arrowwidth=1.4,
                arrowcolor=f'rgba(178,141,255,{alpha:.2f})',
                standoff=7,
                startstandoff=2,
            )
        latest = recent[-1]
        figure.add_trace(
            go.Scatter(
                x=[latest[1]],
                y=[latest[2]],
                mode='markers',
                marker={'size': 14, 'color': 'rgba(255,200,87,0)', 'line': {'color': '#ffc857', 'width': 2}},
                hoverinfo='skip',
                name='最新',
            )
        )
        figure.add_annotation(
            x=latest[1],
            y=latest[2],
            text=f'最新 {latest[0].strftime("%m-%d")}',
            showarrow=False,
            font={'size': 10, 'color': '#ffc857'},
            xanchor='left' if latest[1] < 80 else 'right',
            yanchor='bottom',
            xshift=8,
            yshift=6,
        )
        oldest = recent[0]
        if len(recent) > 1:
            figure.add_annotation(
                x=oldest[1],
                y=oldest[2],
                text=f'{len(recent) - 1} 个交易日前 {oldest[0].strftime("%m-%d")}',
                showarrow=False,
                font={'size': 9, 'color': '#8b909b'},
                xanchor='right' if oldest[1] > 20 else 'left',
                yanchor='top',
                xshift=-8 if oldest[1] > 20 else 8,
                yshift=-6,
            )

    # 文案贴在各象限**外侧角**（远离原点的那一角）：横/纵坐标取轴边（0 或 100）向内缩
    # QUADRANT_LABEL_INSET，再用 anchor 让文字朝象限内侧展开。这里必须用轴边的绝对位置，
    # 不能写成「象限角坐标 + 偏移」——那样上/右三个象限的文案会落到坐标 154，超出 0—100
    # 的轴范围，图上只剩左下角「缩量阴跌」一条。
    for x0, y0, label in QUADRANT_LABELS:
        right, top = x0 >= 50, y0 >= 50
        figure.add_annotation(
            x=QUADRANT_AXIS[1] - QUADRANT_LABEL_INSET if right else QUADRANT_AXIS[0] + QUADRANT_LABEL_INSET,
            y=QUADRANT_AXIS[1] - QUADRANT_LABEL_INSET if top else QUADRANT_AXIS[0] + QUADRANT_LABEL_INSET,
            text=label,
            showarrow=False,
            font={'size': 10, 'color': '#8b909b'},
            xanchor='right' if right else 'left',
            yanchor='top' if top else 'bottom',
        )

    figure.update_layout(
        height=height,
        # 顶部留出图例的高度：图例在 y=1.02（绘图区之上），t 太小会被裁掉
        margin={'l': 4, 'r': 4, 't': 36 if recent else 18, 'b': 4},
        xaxis={
            'title': {'text': '参与度指数', 'font': {'size': 11}},
            'range': list(QUADRANT_AXIS),
            'gridcolor': 'rgba(255,255,255,.06)',
            'showline': False,
            'nticks': 5,
        },
        yaxis={
            'title': {'text': '方向指数', 'font': {'size': 11}},
            'range': list(QUADRANT_AXIS),
            'gridcolor': 'rgba(255,255,255,.06)',
            'showline': False,
            'nticks': 5,
        },
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)',
        font={'color': '#c9cdd6', 'size': 11},
        showlegend=bool(recent),
        legend={
            'orientation': 'h',
            'x': 0,
            'y': 1.02,
            'xanchor': 'left',
            'yanchor': 'bottom',
            'font': {'size': 9},
            'bgcolor': 'rgba(0,0,0,0)',
        },
        hovermode='closest',
    )
    return figure
