"""A股投资信息看板（单页，只读本地数据库）。

启动：streamlit run app.py
数据：先运行 `python update_data.py --full` 完成首次采集。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from macroboard import charts, indicators
from macroboard.charts import CHART_SPECS, RESOLUTION_OPTIONS, SPREAD_SPEC
from macroboard.collect import beijing_today, collect
from macroboard.config import (
    APP_NAME,
    APP_TAGLINE,
    CHART_RANGE_OPTIONS,
    DEFAULT_DB_PATH,
    DEFAULT_ENV_FILE,
    PERCENTILE_MIN_SAMPLES,
    PERCENTILE_WINDOW_YEARS,
)
from macroboard.dashboard import DashboardData, load_dashboard
from macroboard.env import load_env_file

MANUAL_REFRESH_COOLDOWN = timedelta(minutes=30)

st.set_page_config(page_title=APP_NAME, page_icon='📈', layout='wide')

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; max-width: 1180px;}
      .muted {color: #8b909b; font-size: .82rem; line-height: 1.5;}
      .card-title {font-size: .95rem; font-weight: 600; margin-bottom: .15rem;}
      .card-value {font-size: 1.55rem; font-weight: 650; letter-spacing: .01em;}
      .card-sub {font-size: .82rem; color: #a9aeb9;}
      .status-ok {color: #5eead4;}
      .status-warn {color: #f5b13d;}
      .status-bad {color: #ff6b8b;}
    </style>
    """,
    unsafe_allow_html=True,
)


def _status_class(status: str) -> str:
    if status == 'normal':
        return 'status-ok'
    if status in {'stale', 'insufficient'}:
        return 'status-warn'
    return 'status-bad'


def render_card(column, card) -> None:
    with column, st.container(border=True):
        st.markdown(f'<div class="card-title">{card.title}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card-value">{card.value_text}</div>', unsafe_allow_html=True)
        if card.change_text:
            st.markdown(f'<div class="card-sub">较上一有效观测 {card.change_text}</div>', unsafe_allow_html=True)
        for label, value in card.sub_lines:
            st.markdown(f'<div class="card-sub">{label}：{value}</div>', unsafe_allow_html=True)
        obs = card.obs_date.isoformat() if card.obs_date else '—'
        st.markdown(
            f'<div class="muted">数据日期 {obs} · 来源 {card.source}<br>'
            f'状态 <span class="{_status_class(card.status)}">{card.status_label}</span></div>',
            unsafe_allow_html=True,
        )


def prepare_curve(
    series: list[tuple],
    *,
    days: int | None,
    option: str,
    reference,
) -> tuple[list[tuple], list[tuple], str, list[tuple] | None]:
    """切片 → 解析粒度 → 重采样；仅在「近密远疏」模式下返回分段位置。"""
    sliced = charts.slice_range(series, days, now=reference)
    resolution = charts.effective_resolution(option, sliced)
    sampled = charts.resample(sliced, resolution, now=reference)
    boundaries = charts.tiered_boundaries(reference) if resolution == charts.RESOLUTION_TIERED else None
    return sliced, sampled, resolution, boundaries


def render_charts_section(data: DashboardData, *, days: int | None, option: str) -> None:
    """六类指标的变化曲线：一个视图内使用统一分辨率。"""
    reference = beijing_today()
    rows = [CHART_SPECS[index : index + 2] for index in range(0, len(CHART_SPECS), 2)]
    for pair in rows:
        columns = st.columns(len(pair))
        for column, spec in zip(columns, pair, strict=True):
            series = data.chart_series.get(spec.key) or []
            sliced, sampled, resolution, boundaries = prepare_curve(
                series, days=days, option=option, reference=reference
            )
            with column, st.container(border=True):
                st.markdown(f'**{spec.title}**')
                if not sampled:
                    st.caption('未接通')
                    continue
                figure = charts.build_figure(sampled, spec, boundaries=boundaries)
                st.plotly_chart(figure, width='stretch', key=f'chart-{spec.key}')
                shown = f'{len(sampled)}/{len(sliced)}' if len(sampled) != len(sliced) else f'{len(sampled)}'
                st.caption(f'最新 {charts.latest_text(sampled, spec)} · {resolution} · {shown} 点')


def render_spread_section(data: DashboardData, *, days: int | None, option: str) -> None:
    st.subheader('沪深300股债利差')
    spread = data.spread
    stats = spread.stats
    if stats is None or not spread.series:
        st.info('股债利差数据未接通：需要沪深300 PE-TTM 与中债10年期国债收益率的有效数据。')
        return

    reference = beijing_today()
    spread_points = [(day, indicators.to_percentage_points(value)) for day, value in spread.series]
    sliced, sampled, resolution, boundaries = prepare_curve(
        spread_points, days=days, option=option, reference=reference
    )
    st.plotly_chart(
        charts.build_figure(sampled, SPREAD_SPEC, height=340, boundaries=boundaries),
        width='stretch',
        key='chart-spread',
    )
    caption = f'曲线粒度 {resolution} · 显示 {len(sampled)}/{len(sliced)} 点'
    if resolution != charts.RESOLUTION_DAILY:
        caption += '；百分位始终用全部有效样本计算，与显示粒度无关'
    st.caption(caption)

    left, middle, right = st.columns(3)
    with left:
        st.metric('当前利差', f'{indicators.to_percentage_points(stats.value):.2f} 个百分点')
        st.caption(f'≈ {indicators.to_bp(stats.value):,.0f} BP')
    with middle:
        if stats.percentile is None:
            st.metric('百分位', '样本不足')
            st.caption(f'有效样本 {stats.sample_size} 个（<{PERCENTILE_MIN_SAMPLES}），不显示百分位')
        else:
            st.metric(stats.window_label, f'{stats.percentile:.1f}%')
            st.caption(f'有效样本 {stats.sample_size} 个')
    with right:
        pe_text = f'{spread.pe_ttm:.2f} 倍' if spread.pe_ttm else '—'
        y_text = f'{spread.cn10y_pct:.4f}%' if spread.cn10y_pct is not None else '—'
        st.metric('计算输入', f'PE {pe_text}')
        st.caption(f'中债10年期 {y_text} · 数据日期 {spread.obs_date}')

    st.caption(
        f'样本区间 {stats.sample_start} 至 {stats.sample_end}（窗口起点 {stats.window_start}）'
        f' · 百分位窗口 {"完整5年" if stats.window_full else "历史不足5年，按可用历史计算"}'
    )

    with st.expander('公式与数据口径'):
        st.markdown(
            f"""
**股债利差**

```
spread = 1 / PE_TTM - 中国10年期国债收益率
```

- 收益率统一按小数计算：PE=12.5、国债收益率=2.00% 时，spread = 0.08 - 0.02 = 0.06，
  即 6.00 个百分点 / 600 BP。
- 两个输入必须来自**同一个数据日期**：使用最近一个双方均有有效数据的共同日期
  （当前为 {spread.obs_date}）。PE 缺失、PE ≤ 0 或国债数据缺失的日期不计算，
  不做前向填充，也不做插值。

**百分位**

```
percentile = 100 ×（历史小于当前值的数量 + 0.5 × 历史等于当前值的数量）÷ 有效样本数量
```

- 默认窗口为过去 {PERCENTILE_WINDOW_YEARS} 年，且只使用**严格早于当前数据日期**的有效样本。
- 使用未做显示舍入的数值计算。
- 有效样本不足 {PERCENTILE_MIN_SAMPLES} 个时不显示百分位，标注“样本不足”。
- 历史不足 {PERCENTILE_WINDOW_YEARS} 年时标注为“可用历史百分位”，不称为
  “{PERCENTILE_WINDOW_YEARS}年百分位”。
- 这里的百分位是**股债利差的百分位**，不是 PE 的百分位。

**数据来源**

- 沪深300 PE-TTM：东方财富妙想数据（沪深300指数 399300.SZ），供应商计算口径，
  未用成分股 PE 简单平均替代。
- 中国10年期国债收益率：中债国债收益率曲线 10 年期到期收益率，经东方财富数据中心镜像。
- 详细来源、字段与验证结果见 `docs/DATA_SOURCES.md`。

> 历史百分位不代表未来上涨概率，也不是买卖信号。
            """
        )


def render_alerts(data: DashboardData) -> None:
    if not data.alerts:
        return
    for message in data.alerts:
        st.warning(message, icon='⚠️')


def main() -> None:
    try:
        load_env_file(DEFAULT_ENV_FILE)
    except PermissionError as exc:
        st.sidebar.warning(str(exc))

    data = load_dashboard(DEFAULT_DB_PATH)

    st.title(APP_NAME)
    st.caption(APP_TAGLINE)

    header_left, header_right = st.columns([3, 2])
    with header_left:
        if data.last_run_at:
            st.markdown(f'**最近采集时间**：{data.last_run_at}')
        else:
            st.markdown('**最近采集时间**：尚未采集（先运行 `python update_data.py --full`）')
        st.caption('采集成功不等于所有指标都已更新，请以下方每个指标的数据日期与状态为准。')
    with header_right:
        cooldown_ok = True
        if data.last_run_at:
            try:
                finished = datetime.fromisoformat(data.last_run_at.replace('Z', '+00:00'))
                cooldown_ok = datetime.now(finished.tzinfo) - finished > MANUAL_REFRESH_COOLDOWN
            except ValueError:
                cooldown_ok = True
        if st.button('立即采集一次', width='stretch'):
            if not cooldown_ok:
                st.info(f'距上次采集不足 {MANUAL_REFRESH_COOLDOWN.seconds // 60} 分钟，已跳过重复请求。')
            else:
                with st.spinner('正在采集数据…'):
                    collect(DEFAULT_DB_PATH)
                st.rerun()

    st.divider()
    render_alerts(data)

    cards = data.cards
    row1 = st.columns(2)
    row2 = st.columns(2)
    row3 = st.columns(2)
    columns = [row1[0], row1[1], row2[0], row2[1], row3[0], row3[1]]
    for column, card in zip(columns, cards, strict=True):
        render_card(column, card)

    st.divider()
    st.subheader('变化曲线')
    control_left, control_middle, control_right = st.columns([1, 1, 4])
    with control_left:
        range_label = st.selectbox('时间范围', list(CHART_RANGE_OPTIONS), index=2, key='chart-range')
    with control_middle:
        resolution_label = st.selectbox('曲线粒度', list(RESOLUTION_OPTIONS), index=0, key='chart-resolution')
    with control_right:
        st.caption(
            '自动：1 年以内日线、1~3 年周线、3 年以上月线，一个视图内只使用一种分辨率，'
            '时间轴与密度匹配。「近密远疏」保留为可选项，并在图上标出日线→周线→月线的切换位置。'
            '所有模式只保留区间内最后一个真实观测，不做平均、不插值。'
        )
    chart_days = CHART_RANGE_OPTIONS[range_label]
    render_charts_section(data, days=chart_days, option=resolution_label)

    st.divider()
    render_spread_section(data, days=chart_days, option=resolution_label)

    st.divider()
    st.caption(
        '本页面仅用于宏观与估值观察，展示真实来源数据与固定公式计算结果，'
        '不提供自动交易，不输出买卖指令。数据来源与字段见 docs/DATA_SOURCES.md，'
        f'采集脚本 update_data.py，数据文件 {DEFAULT_DB_PATH}。'
    )


main()
