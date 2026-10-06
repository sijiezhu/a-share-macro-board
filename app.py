"""A股投资信息看板（单页；行情库只读，访问计数写入独立的本地统计库）。

启动：streamlit run app.py
数据：先运行 `python update_data.py --full` 完成首次采集。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

from macroboard import charts, indicators, visits
from macroboard.charts import (
    LONG_TERM_CHART_SPECS,
    RESOLUTION_OPTIONS,
    SENTIMENT_CHART_SPECS,
    SENTIMENT_SPECS,
    SH_INDEX_SPEC,
    SHORT_TERM_CHART_SPECS,
    SPREAD_SPEC,
)
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
from macroboard.dashboard import Card, DashboardData, load_dashboard
from macroboard.env import load_env_file
from macroboard.sentiment import codes
from macroboard.sentiment_view import (
    ComponentValueInfo,
    SentimentView,
    build_sentiment_view,
    component_frame,
    component_label,
    field_frame,
    render_context,
    state_reference_frame,
)

MANUAL_REFRESH_COOLDOWN = timedelta(minutes=30)

# 页面按观察周期分两组：短线（A股情绪与市场宽度）在上，长线（宏观与估值）在下。
# 两组的卡片与曲线各自归属，时间范围、曲线粒度互不影响。
SHORT_TERM_CARD_KEYS: tuple[str, ...] = ('sentiment',)
LONG_TERM_CARD_KEYS: tuple[str, ...] = ('us10y', 'cn10y', 'usdcny', 'xauusd', 'spread')

# 「短线指标：A股市场宽度」区块（A股市场情绪参考卡片、上涨家数占比、沪深300近20个交易日
# 涨跌幅）暂时下线：数据仍在采集、代码与控件逻辑保留，需要重新上线时把这里改成 True
# （恢复后该区块仍独立取时间范围，默认 1 个月）。
SHOW_BREADTH_BLOCK = False

RESOLUTION_NOTE = (
    '自动：1 年以内日线、1~3 年周线、3 年以上月线，一个视图内只使用一种分辨率，'
    '时间轴与密度匹配。「近密远疏」保留为可选项，并在图上标出日线→周线→月线的切换位置。'
    '所有模式只保留区间内最后一个真实观测，不做平均、不插值。'
)

# 手动「立即采集一次」按钮默认关闭：页面可能被局域网访问（见 deploy/README.md），
# 而这个按钮会让任何能打开页面的人触发采集、消耗妙想调用次数。
# 需要临时开启时设置环境变量 MACROBOARD_ENABLE_MANUAL_REFRESH=1 并重启服务。
MANUAL_REFRESH_ENABLED = os.environ.get('MACROBOARD_ENABLE_MANUAL_REFRESH', '').strip().lower() in {
    '1',
    'true',
    'yes',
    'on',
}

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


def render_charts_section(
    data: DashboardData,
    *,
    specs: tuple[charts.ChartSpec, ...],
    days: int | None,
    option: str,
) -> None:
    """一组指标的变化曲线：一个视图内使用统一分辨率，每组独立取时间范围。"""
    reference = beijing_today()
    rows = [specs[index : index + 2] for index in range(0, len(specs), 2)]
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


def render_card_grid(cards: list[Card], *, per_row: int = 2) -> None:
    """卡片按每行固定列数排布，不足一行时只占用左侧列。"""
    for start in range(0, len(cards), per_row):
        chunk = cards[start : start + per_row]
        columns = st.columns(per_row)[: len(chunk)]
        for column, card in zip(columns, chunk, strict=True):
            render_card(column, card)


def render_range_controls(prefix: str, *, default_range: str) -> tuple[int | None, str]:
    """一组指标的时间范围与曲线粒度控件；不同分组用不同 key，互不影响。"""
    labels = list(CHART_RANGE_OPTIONS)
    left, middle, right = st.columns([1, 1, 4])
    with left:
        range_label = st.selectbox(
            '时间范围',
            labels,
            index=labels.index(default_range),
            key=f'{prefix}-range',
        )
    with middle:
        resolution_label = st.selectbox(
            '曲线粒度',
            list(RESOLUTION_OPTIONS),
            index=0,
            key=f'{prefix}-resolution',
        )
    with right:
        st.caption(RESOLUTION_NOTE)
    return CHART_RANGE_OPTIONS[range_label], resolution_label


def render_spread_section(data: DashboardData, *, days: int | None, option: str) -> None:
    st.markdown('#### 沪深300股债利差')
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


def resolve_db_path() -> Path:
    """数据库路径：环境变量 `MACROBOARD_DB` 优先（便于测试或指向另一份库）。"""
    override = os.environ.get('MACROBOARD_DB')
    return Path(override) if override else DEFAULT_DB_PATH


def record_visit_once(visit_db_path: Path) -> None:
    """一次页面会话只记 1 人次：Streamlit 每次交互都会重跑脚本，用 session_state 去重。

    统计写在独立的本地文件里（默认 `<行情库目录>/visits.sqlite3`），行情库仍只读；
    落库内容含客户端 IP（供后台核对，页面不展示，见 macroboard/visits.py 与
    docs/DATA_SOURCES.md 第六节）；
    统计失败（磁盘只读、库损坏、接口变更等）只影响统计展示，不打断页面。
    """
    if not visits.logging_enabled() or st.session_state.get('visit-recorded'):
        return
    st.session_state['visit-recorded'] = True
    try:
        visits.record_visit(
            visit_db_path,
            ip=st.context.ip_address,
            user_agent=st.context.headers.get('User-Agent'),
        )
    except Exception as exc:  # 统计不是页面主功能：任何失败都只记录原因，不让整页报错
        st.session_state['visit-error'] = f'{type(exc).__name__}: {exc}'


def render_visit_stats(visit_db_path: Path) -> None:
    """页脚：今日 / 近 7 日 / 累计访问人次与匿名独立访客（可展开看口径与明细）。"""
    if not visits.logging_enabled():
        st.caption('访问统计：已关闭（环境变量 MACROBOARD_VISIT_LOG=0）。')
        return
    record_error = st.session_state.get('visit-error')
    if record_error:
        st.caption(f'注意：本次访问没有计入统计（{record_error}）。')
    stats = visits.load_stats(visit_db_path)
    if stats is None:
        st.caption('访问统计：未接通（本地统计库无法读取）。')
        return

    st.markdown(
        f'**访问统计**：今日 {stats.today_visits:,} 人次'
        f'（独立访客 {stats.today_visitors:,}）· '
        f'近 {stats.window_days} 日 {stats.window_visits:,} 人次 · '
        f'累计 {stats.total_visits:,} 人次（独立访客 {stats.total_visitors:,}）'
    )
    with st.expander(f'访问统计口径与近 {stats.history_days} 天明细'):
        first_row = st.columns(3)
        with first_row[0]:
            st.metric('今日访问人次', f'{stats.today_visits:,}')
        with first_row[1]:
            st.metric('今日独立访客', f'{stats.today_visitors:,}')
        with first_row[2]:
            st.metric(f'近 {stats.window_days} 日访问人次', f'{stats.window_visits:,}')
        second_row = st.columns(3)
        with second_row[0]:
            st.metric('累计访问人次', f'{stats.total_visits:,}')
        with second_row[1]:
            st.metric('累计独立访客', f'{stats.total_visitors:,}')
        with second_row[2]:
            st.metric('最近一次访问', visits.to_beijing_text(stats.last_at))
        if stats.daily:
            st.dataframe(
                [
                    {
                        '日期（北京时间）': item.day.isoformat(),
                        '访问人次': item.visits,
                        '独立访客': item.visitors,
                    }
                    for item in stats.daily
                ],
                hide_index=True,
                width='stretch',
            )
            st.caption(f'只列出近 {stats.history_days} 天内有访问的日期，不补零、不估计。')
        else:
            st.caption(f'近 {stats.history_days} 天内还没有访问记录。')
        st.caption(
            '口径：一次页面会话（打开或刷新页面）计 1 人次，会话内切换控件不会重复计数；'
            '独立访客按「SHA-256(随机盐 + 客户端 IP + User-Agent) 前 16 位」指纹去重，'
            '指纹相同只代表同一 IP + 同一浏览器，不是身份识别。'
            '统计库另存每条记录的客户端 IP（本机文件、权限 600，页面不显示；'
            'User-Agent 只以指纹形式参与，不保存原文）。自然日按北京时间划分。'
            '已知限制：WebSocket 断开或服务重启会新建会话，同一个人可能被计成多次；'
            '换浏览器或换设备会被算成不同访客。首次访问时间 '
            f'{visits.to_beijing_text(stats.first_at)}（北京时间）。'
            '来源与字段见 docs/DATA_SOURCES.md「访问统计（页面人次，非行情数据）」。'
        )


def _composition_text(members: list[ComponentValueInfo], index_value: float | None) -> str:
    """把「权重 × 分量最新值 = 指数」的算式写成一行（缺失分量显式写"不可用"）。"""
    parts: list[str] = []
    usable = 0
    for info in members:
        if info.weight_share is None or info.value is None:
            parts.append(f'{info.label} 不可用')
            continue
        usable += 1
        parts.append(f'{info.weight_share:.2f} × {info.label} {info.value:.1f}')
    if usable == 0:
        return '指数不可用：分量全部缺失'
    total = '不可用' if index_value is None else f'{index_value:.1f}'
    return ' + '.join(parts) + f' = {total}'


def render_index_components(view: SentimentView, snapshot: dict) -> None:
    """折叠显示参与度/方向指数用到的每个分量的最新取值、口径与权重。

    分量的逐项明细只在需要核对时看，默认收起，与「情绪指标口径与输入数据」一致；
    指数本身的读数与覆盖率在折叠区外，不受影响。
    """
    components = [c for c in view.components if c.in_index]
    if not components:
        return
    with st.expander('指数分量（进入计算的最新取值）'):
        for group, title, index_key in (
            ('participation', '参与度指数', 'participation_index'),
            ('direction', '方向指数', 'direction_index'),
        ):
            members = [c for c in components if c.group == group]
            if not members:
                continue
            for column, info in zip(st.columns(len(members)), members, strict=True):
                with column:
                    st.metric(
                        info.label,
                        '不可用' if info.value is None else f'{info.value:.1f} / 100',
                    )
                    if info.value is None:
                        st.caption('该分量当前不可用（字段缺失或样本不足）')
                    else:
                        st.caption(
                            f'取值日期 {info.value_date.isoformat()}'
                            + (f' · 组内权重 {info.weight_share:.0%}' if info.weight_share else '')
                        )
                    method = view.profile.get(info.name, '')
                    if method:
                        st.caption(
                            f'口径：{method}' + (' · T+1 滞后使用' if info.availability_lag else '')
                        )
            index_value = snapshot.get(index_key)
            st.caption(
                f'{title} = {_composition_text(members, None if index_value is None else float(index_value))}'
                '（权重按当日可用分量重归一；分量为 0—100 的分数或分位）'
            )


def _latest_text(value: object, *, digits: int = 1) -> str:
    """最新读数的数字文案；缺失时返回「不可用」，不用 0 或前一日值顶替。"""
    return '不可用' if value is None else f'{float(value):.{digits}f}'


def _latest_metric(snapshot: dict, key: str, *, digits: int = 1) -> str:
    """「x.x / 100」形式的最新读数，缺失时整个读数标为不可用。"""
    value = snapshot.get(key)
    return '不可用' if value is None else f'{float(value):.{digits}f} / 100'


def _latest_gap_text(snapshot: dict) -> str:
    """最新交易日「指数读数为空」时的说明；有读数就返回空串。

    覆盖率不满不等于读数不可用：两融 T+1 公布，当日多半缺该分量，
    参与度会按可用分量重归一退化成量比得分独占（权重 100%）——
    这是每日常态，由卡片上的「可用分量覆盖率」体现，不在这里告警。
    只有可用分量少到算不出指数（低于 min_index_coverage）时才提示，并点名缺了什么。
    """
    gaps: list[str] = []
    for label, key, group in (
        ('参与度指数', 'participation_index', 'participation'),
        ('方向指数', 'direction_index', 'direction'),
    ):
        if snapshot.get(key) is not None:
            continue
        names = [*(snapshot.get(f'{group}_missing') or ())]
        detail = '（缺 ' + '、'.join(component_label(name) for name in names) + '）' if names else ''
        gaps.append(f'{label}{detail}')
    if not gaps:
        return ''
    return (
        f"最新交易日 {snapshot.get('as_of')} 的读数不可用：" + '、'.join(gaps)
        + '，等待下次采集补齐。'
    )


def _panic_breakdown_text(snapshot: dict) -> str:
    """恐慌分的命中 / 覆盖率 / 置信度；任一字段缺失时整行标为不可用。"""
    coverage = snapshot.get('panic_coverage')
    confidence = snapshot.get('panic_confidence')
    hits = snapshot.get('panic_hits')
    if coverage is None or confidence is None or hits is None:
        return '命中与覆盖率：不可用（分量缺失）'
    return (
        f'命中 {hits} / 可评估 {float(coverage) * 5:.0f}'
        f' · 覆盖率 {float(coverage):.0%}'
        f' · 置信度 {float(confidence):.0%}'
    )


def render_sentiment_section(data: DashboardData, *, days: int | None, option: str) -> None:
    """A股市场情绪指标：指数、状态、恐慌分、反转观察、量价状态与曲线。"""
    view = build_sentiment_view(data.series, data.statuses)
    ctx = render_context(view)
    snapshot = view.snapshot

    if not view.available:
        st.info(view.reason or '情绪指标未接通。')
    else:
        gap_text = _latest_gap_text(snapshot)
        if gap_text:
            st.warning(gap_text)
        top_left, top_right = st.columns([2, 3])
        with top_left, st.container(border=True):
            st.markdown('<div class="card-title">四象限状态</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="card-value">{ctx["state_label"]}</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="card-sub">参与度 {_latest_text(snapshot.get("participation_index"))}'
                f'（{ctx["participation_band_text"]}） · '
                f'方向 {_latest_text(snapshot.get("direction_index"))}'
                f'（{ctx["direction_band_text"]}）</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="muted">{ctx["position_text"]}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="muted">判定阈值 40 / 60：参与度与方向都高＝放量上行，'
                f'参与度高而方向低＝放量急跌，都低＝缩量回落，'
                f'参与度低而方向高＝缩量回暖，其余为中性震荡。<br>'
                f'同一象限在高位与低位含义不同：高位（上证指数收盘高于 60 日均线）'
                f'是趋势末段的警惕，低位则多是冰点与抛压宣泄；位置不可用时按四象限基础口径显示。<br>'
                f'数据日期 {view.as_of} · 已有读数 {view.used_rows} 个交易日</div>',
                unsafe_allow_html=True,
            )
        with top_right:
            points = view.quadrant_points
            if days is not None and points:
                cutoff = beijing_today() - timedelta(days=days)
                points = [item for item in points if item[0] >= cutoff]
            if points:
                st.plotly_chart(
                    charts.build_quadrant_figure(points),
                    width='stretch',
                    key='chart-quadrant',
                )
                shown = points[-10:]
                st.caption(
                    f'四象限位置：横轴参与度、纵轴方向（0—100，虚线为 40/60 阈值）· '
                    f'点形表示价格位置：● 高位（上证指数收盘 > 60 日均线）、○ 低位、◇ 位置未知 · '
                    f'最近 {len(shown)} 个交易日，箭头由过去指向未来 · '
                    f'{shown[0][0].isoformat()} → {shown[-1][0].isoformat()}'
                )
            else:
                st.caption('四象限位置：样本不足')

        left, middle, right = st.columns(3)
        with left:
            st.metric(
                '综合情绪指数',
                _latest_metric(snapshot, 'sentiment_index'),
            )
            st.caption(
                f"数据日期 {view.as_of} · 过去 {view.window} 个交易日分位"
                + (
                    f" · 情绪分位 {snapshot['sentiment_pct']:.1f}%"
                    if snapshot.get('sentiment_pct') is not None
                    else ' · 情绪分位样本不足'
                )
            )
        with middle:
            st.metric('参与度指数', _latest_metric(snapshot, 'participation_index'))
            st.caption(_coverage_text(snapshot, 'participation'))
        with right:
            st.metric('方向指数', _latest_metric(snapshot, 'direction_index'))
            st.caption(_coverage_text(snapshot, 'direction'))

        render_index_components(view, snapshot)

        row = st.columns(3)
        with row[0]:
            st.metric('恐慌抛售得分', _latest_metric(snapshot, 'panic_score', digits=0))
            st.caption(_panic_breakdown_text(snapshot))
            st.caption(f"触发条件：{ctx['panic_text']}")
        with row[1]:
            st.metric('冰点反转观察', '满足' if snapshot['reversal_watch'] else '未触发')
            st.caption(f"满足条件：{ctx['reversal_text']}")
            st.caption(f"门控：{ctx['block_reason_text']} · 已有读数 {view.used_rows} 个交易日")
        with row[2]:
            st.metric('量价状态', ctx['macd_volume_label'])
            divergence = snapshot.get('macd_bullish_divergence')
            st.caption(
                '底背离：'
                + ('是' if divergence is True else '否' if divergence is False else '不可用')
            )
            st.caption(f"数据质量：{ctx['quality_label']}")

    reference = beijing_today()
    # 上证指数取自库内原始序列（不参与任何合成，也不做位置判定以外的加工）；
    # 三条指数曲线来自 sentiment_view 的组装结果。
    series_by_key = {
        **{spec.key: view.curves.get(spec.key) or [] for spec in SENTIMENT_SPECS},
        SH_INDEX_SPEC.key: data.series.get(SH_INDEX_SPEC.key) or [],
    }
    sliced_by_spec = [
        (spec, charts.slice_range(series_by_key[spec.key], days, now=reference))
        for spec in SENTIMENT_CHART_SPECS
    ]
    # 四张图同列、共用一条时间轴，因此只能有一种分辨率（按各序列的整体跨度判定），
    # 不能各自按自己的跨度选粒度——这正是「一个视图内统一分辨率」的要求。
    resolution = charts.unified_resolution(option, [sliced for _spec, sliced in sliced_by_spec])
    boundaries = (
        charts.tiered_boundaries(reference) if resolution == charts.RESOLUTION_TIERED else None
    )
    curves = [
        (spec, sliced, charts.resample(sliced, resolution, now=reference))
        for spec, sliced in sliced_by_spec
    ]
    panels = [(spec, sampled, boundaries) for spec, _sliced, sampled in curves]
    # 四张图都铺价格位置底色：逐日位置按**日期精确对齐**到每张图自己画出来的点上
    # （位置与上证指数同一天，不做前向填充），再合并成连续背景带。各图点数与日期
    # 可以不同，所以底色按各自的观测日分段，切换点落在该图自己的点上。
    position_by_day = dict(view.positions)
    bands = {
        spec.key: charts.position_bands(
            [
                (day, position_by_day.get(day, codes.POS_UNKNOWN))
                for day, _value in sampled
            ]
        )
        for spec, _sliced, sampled in curves
    }

    with st.container(border=True):
        if not any(sampled for _spec, _sliced, sampled in curves):
            st.markdown('**上证指数 / 综合情绪 / 参与度 / 方向指数**')
            st.caption('样本不足')
        else:
            st.plotly_chart(
                charts.build_stacked_figure(panels, bands=bands),
                width='stretch',
                key='chart-sentiment-curves',
            )
            st.caption(
                f'{resolution} · 一个视图内统一分辨率 · {len(panels)} 张图同列并共用一条时间轴：'
                f'光标停在任意一张图上，一条竖线纵穿 {len(panels)} 张图，'
                f'提示框同时给出 {len(panels)} 条曲线在该日期的取值'
            )
            starts = {sampled[0][0] for _spec, _sliced, sampled in curves if sampled}
            st.caption(
                '；'.join(
                    f'{spec.title} {len(sampled)}/{len(sliced)} 点'
                    f' · 起点 {sampled[0][0].isoformat()} → 最新 {sampled[-1][0].isoformat()}'
                    for spec, sliced, sampled in curves
                    if sampled
                )
                + (
                    '（起点不同＝该序列确有更长的历史；缺口不补齐、不插值）'
                    if len(starts) > 1
                    else ''
                )
            )
            st.caption(
                '四张图的底色都＝价格位置（与曲线同粒度，每段取区间内最后一个真实观测）：'
                '红底＝高位（上证指数当日收盘 > 60 日均线）、绿底＝低位（不高于均线）；'
                '无底色＝位置未知（均线未满 60 个交易日或当日无上证指数观测）。'
                '悬停时竖线纵穿四张图，横线只在光标所在的那张图内'
                '（各图纵轴刻度不同，横线跨图会被误读）。位置只描述状态，不是买卖提示。'
            )

    with st.expander('情绪指标口径与输入数据'):
        st.markdown(
            f"""
**构造方式（等权，缺失分量跳过并标注覆盖率）**

```
参与度指数 = 0.5 × 量比得分 + 0.5 × 两融交易额20日分位
量比得分   = 阈值映射：0.5 → 0、1.0 → 50、2.0 → 100（分段线性，两端截断）
方向指数   = 平均(20日收益分位, MACD柱分位, 涨跌停比分位, 上涨家数占比分位)
综合情绪   = 0.5 × 参与度指数 + 0.5 × 方向指数
恐慌抛售得分 = 100 × 命中条件数 ÷ 可评估条件数（可评估数 < 3 时标不可用）
```

- 分位窗口：方向指数与综合情绪自身的分位用过去 **{view.window} 个交易日**；
  参与度指数是固定口径（量比阈值映射 + 两融交易额 20 日分位），不随窗口旋钮变化。
- 参与度只用**无符号量**：两融交易额 = 融资买入额 + 融资偿还额（同日相加，T+1 可得），
  表达"杠杆资金有多活跃"；因此参与度轴不表达多空方向，与方向指数相互独立。
- 两融**不取前一日的值顶替**：当日尚未（完整）公布时该分量按缺失处理，参与度指数只由量比得分
  决定（权重 100%，覆盖率显示 0.5）。因此当日读数会在两融公布后按等权口径重算。
- 两融**完整性校验**：两融交易额与净买入来自**同一份**「融资买入额 + 融资偿还额」观测，
  源站只发布一部分时两者一起失真。因此当「两融交易额 ÷ 成交额」与前 20 个交易日该比值的
  中位数、**以及**两融交易额与其自身历史中位数**同时**低于 0.7 倍时，判定该日发布不完整，
  两个字段一并按缺失处理，不进入 20 日分位窗口（2026-09-24 实测：比值为前 20 日中位数的
  0.49，其余交易日最低 0.83）。窗口与阈值是配置项，阈值设 0 即关闭该校验。
- 口径 = **严格早于当日 + 中位秩**（与股债利差分位同一排名公式，但窗口按交易日滚动）。
- `min_periods` 按窗口比例推导（252 天窗口 → 120 个有效样本）；样本未满窗口时只是"位置估计"，
  不称为「{view.window} 日分位」。
- 分量口径：{'；'.join(f'{name}={text}' for name, text in view.profile.items())}。
- 换手率、成交额与融资净买入（有符号）照常采集与展示，但**不参与指数合成**
  （换手率另用于恐慌抛售得分）。
- 恐慌分只用**可评估**的条件（缺失条件不计入分母），并输出覆盖率与置信度；
  覆盖率低于 0.6 时不触发反转观察信号，页面会显示门控原因。
- 状态 = **四象限 × 价格位置**：位置按上证指数**当日收盘价与其 60 日均线**比较，
  收盘高于均线为高位、不高于为低位（恰好在均线上按低位处理，不声称"高于"）。
  均线满 60 个交易日才生效、含当日、不做前向填充；样本不足或当日无上证指数观测时
  位置显示为未知，状态退回四象限基础口径。
- 位置判定是**独立于指数之外的描述**：它不参与参与度/方向/综合情绪指数，
  也不影响恐慌分、覆盖率与数据质量。
- 反转头号观察信号是状态描述，不是买入信号；本页不输出任何买卖指令、目标价或仓位建议。
            """
        )
        st.markdown(
            """
**状态对照（四象限 × 价格位置）**
            """
        )
        st.dataframe(state_reference_frame(), hide_index=True, width='stretch')
        st.caption(
            '高位＝上证指数收盘高于 60 日均线，低位＝不高于；'
            '中性震荡与不可用不区分高低位（位置对它们不增加信息）。'
            '文案只描述状态，不构成任何买卖提示。'
        )
        st.markdown(
            """
**指数分量（参与度 / 方向，逐分量取值与口径）**
            """
        )
        st.dataframe(component_frame(view), hide_index=True, width='stretch')
        st.caption(
            '组内权重是「最新一日实际生效」的权重：不可用分量会被剔除后重归一，'
            '因此与配置里的固定权重可能不同；取值日期是当日指数实际用到的取值日期'
            '（T+1 分量的底层观测来自上一交易日）。'
        )
        st.markdown(
            """
**输入字段与数据日期**
            """
        )
        st.dataframe(field_frame(view), hide_index=True, width='stretch')
        st.caption('表中为参与合成的全部输入字段；没有采集到的字段不会参与计算，也不会被臆造。')
        for note in view.notes:
            st.caption(f'· {note}')


def _coverage_text(snapshot: dict, group: str) -> str:
    """覆盖率只在低于 100% 时提示（暂未纳入的分量不算缺失，不再单独列名）。"""
    coverage = snapshot.get(f'{group}_coverage')
    if coverage is None:
        return ''
    percent = float(coverage)
    if percent >= 0.999:
        return '全部分量可用'
    return f'可用分量覆盖率 {percent:.0%}'


def main() -> None:
    try:
        load_env_file(DEFAULT_ENV_FILE)
    except PermissionError as exc:
        st.sidebar.warning(str(exc))

    db_path = resolve_db_path()
    visit_db_path = visits.resolve_visit_db_path(db_path)
    record_visit_once(visit_db_path)
    data = load_dashboard(db_path)

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
        if data.last_checked_at:
            st.markdown(f'**最近检查时间**：{data.last_checked_at}')
        if MANUAL_REFRESH_ENABLED:
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
                        collect(db_path)
                    st.rerun()
        else:
            st.caption(
                '手动采集按钮已关闭（采集由每天 15:01 的定时任务执行）。'
                '需要临时开启：设置环境变量 MACROBOARD_ENABLE_MANUAL_REFRESH=1 后重启服务。'
            )

    st.divider()
    render_alerts(data)

    cards_by_key = {card.key: card for card in data.cards}
    short_cards = [cards_by_key[key] for key in SHORT_TERM_CARD_KEYS if key in cards_by_key]
    long_cards = [cards_by_key[key] for key in LONG_TERM_CARD_KEYS if key in cards_by_key]

    st.subheader('短线指标：A股市场情绪')
    sentiment_days, sentiment_option = render_range_controls('sentiment', default_range='1年')
    render_sentiment_section(data, days=sentiment_days, option=sentiment_option)

    if SHOW_BREADTH_BLOCK:
        st.subheader('短线指标：A股市场宽度')
        st.caption(
            '沪深A股上涨家数占比（当日涨跌扩散度）与沪深300近20个交易日涨跌幅（约 1 个月动量）'
            '同为日频短线读数，默认按 1 个月看近端变化（可切到 3 个月 / 1 年或更长），'
            '不影响上面情绪曲线。'
        )
        breadth_days, breadth_option = render_range_controls('breadth', default_range='1个月')
        render_card_grid(short_cards)
        render_charts_section(
            data, specs=SHORT_TERM_CHART_SPECS, days=breadth_days, option=breadth_option
        )

    st.divider()
    st.subheader('长线指标：宏观经济与估值')
    st.caption(
        '利率、汇率、金价与股债利差属于长线指标，默认 5 年观察周期；'
        '时间范围与曲线粒度同样独立。'
    )
    long_days, long_option = render_range_controls('long', default_range='5年')
    render_card_grid(long_cards)
    render_charts_section(data, specs=LONG_TERM_CHART_SPECS, days=long_days, option=long_option)
    render_spread_section(data, days=long_days, option=long_option)

    st.divider()
    render_visit_stats(visit_db_path)
    st.caption(
        '本页面仅用于宏观与估值观察，展示真实来源数据与固定公式计算结果，'
        '不提供自动交易，不输出买卖指令。数据来源与字段见 docs/DATA_SOURCES.md，'
        f'采集脚本 update_data.py，数据文件 {db_path}。'
    )


main()
