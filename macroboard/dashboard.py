"""从本地数据库组装页面所需数据（只读，不做任何网络请求）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import db, indicators
from .config import SERIES, STATUS_LABELS


@dataclass
class Card:
    key: str
    title: str
    value_text: str
    obs_date: date | None
    source: str
    status: str
    change_text: str = ''
    sub_lines: list[tuple[str, str]] = field(default_factory=list)
    note: str = ''
    status_note: str = ''

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass
class SpreadView:
    series: list[tuple[date, float]]
    stats: indicators.PercentileStats | None
    pe_ttm: float | None
    cn10y_pct: float | None
    obs_date: date | None
    change_bp: float | None
    status: str


@dataclass
class DashboardData:
    last_run_at: str | None
    last_checked_at: str | None
    statuses: dict[str, dict]
    series: dict[str, list[tuple[date, float]]]
    cards: list[Card]
    spread: SpreadView
    alerts: list[str]
    chart_series: dict[str, list[tuple[date, float]]] = field(default_factory=dict)


def _latest(series: list[tuple[date, float]]) -> tuple[date, float] | None:
    return series[-1] if series else None


def _previous(series: list[tuple[date, float]]) -> tuple[date, float] | None:
    return series[-2] if len(series) >= 2 else None


def format_change(kind: str, change: float | None) -> str:
    if change is None:
        return ''
    if kind == 'bp':
        return f'{change:+.1f} BP'
    if kind == 'pct':
        return f'{change:+.2f}%'
    if kind == 'pp':
        return f'{change:+.2f} 个百分点'
    return f'{change:+.2f}'


def format_value(value: float | None, unit: str, digits: int = 2) -> str:
    if value is None:
        return '未接通'
    if unit == '%':
        return f'{value:.{digits}f}%'
    if unit == 'CNY/USD':
        return f'{value:.4f}'
    if unit == 'USD/金衡盎司':
        return f'{value:,.2f}'
    if unit == '倍':
        return f'{value:.2f} 倍'
    if unit == '点':
        return f'{value:,.2f}'
    if unit == '家':
        return f'{value:,.0f}'
    return f'{value:.{digits}f}'


def advance_ratio_series(
    adv: list[tuple[date, float]],
    dec: list[tuple[date, float]],
    flat: list[tuple[date, float]],
) -> list[tuple[date, float]]:
    """上涨家数占比（%）：上涨 /（上涨+下跌+平盘），分母含平盘。"""
    dec_map = dict(dec)
    flat_map = dict(flat)
    out: list[tuple[date, float]] = []
    for day, up in adv:
        down = dec_map.get(day)
        unchanged = flat_map.get(day)
        if down is None or unchanged is None:
            continue
        total = up + down + unchanged
        if total <= 0:
            continue
        out.append((day, up / total * 100.0))
    out.sort(key=lambda item: item[0])
    return out


def rolling_change(series: list[tuple[date, float]], lookback: int = 20) -> float | None:
    """最新收盘价相对 lookback 个交易日前收盘价的涨跌幅（%）。"""
    if len(series) < lookback + 1:
        return None
    latest = series[-1][1]
    base = series[-1 - lookback][1]
    if base == 0:
        return None
    return (latest / base - 1.0) * 100.0


def rolling_change_series(
    series: list[tuple[date, float]], lookback: int = 20
) -> list[tuple[date, float]]:
    """逐日的「近 lookback 个交易日涨跌幅」曲线（%），样本不足处不产出点。"""
    out: list[tuple[date, float]] = []
    for index in range(lookback, len(series)):
        base = series[index - lookback][1]
        if base == 0:
            continue
        out.append((series[index][0], (series[index][1] / base - 1.0) * 100.0))
    return out


def load_dashboard(db_path) -> DashboardData:
    conn = db.connect(db_path)
    db.init_db(conn)
    statuses = {key: dict(row) for key, row in db.get_statuses(conn).items()}
    series: dict[str, list[tuple[date, float]]] = {}
    for key in SERIES:
        series[key] = [
            (date.fromisoformat(row['obs_date']), float(row['value']))
            for row in db.observations_all(conn, key)
        ]
    last_run = db.last_run(conn)
    conn.close()

    cards = _build_cards(statuses, series)
    spread = _build_spread(statuses, series)
    alerts = _build_alerts(cards, spread)
    checked = [statuses[key].get('checked_at') for key in statuses if statuses[key].get('checked_at')]
    chart_series = {
        'us10y': series.get('us10y') or [],
        'cn10y': series.get('cn10y') or [],
        'usdcny': series.get('usdcny') or [],
        'xauusd': series.get('xauusd') or [],
        'adv_ratio': advance_ratio_series(
            series.get('adv_count') or [],
            series.get('dec_count') or [],
            series.get('flat_count') or [],
        ),
        'hs300_20d': rolling_change_series(series.get('hs300_close') or [], 20),
        'spread': spread.series,
    }
    return DashboardData(
        last_run_at=(last_run['finished_at'] or last_run['started_at']) if last_run else None,
        last_checked_at=max(checked) if checked else None,
        statuses=statuses,
        series=series,
        cards=cards,
        spread=spread,
        alerts=alerts,
        chart_series=chart_series,
    )


def _status_of(statuses: dict[str, dict], metric: str) -> str:
    row = statuses.get(metric)
    if not row:
        return 'missing'
    return str(row.get('status') or 'missing')


def _card_from_series(
    metric: str,
    series: dict[str, list[tuple[date, float]]],
    statuses: dict[str, dict],
) -> Card:
    spec = SERIES[metric]
    rows = series.get(metric) or []
    latest = _latest(rows)
    previous = _previous(rows)
    change = None
    if latest and previous:
        if spec.change_kind == 'bp':
            change = indicators.change_in_bp(latest[1], previous[1])
        elif spec.change_kind == 'pct':
            change = indicators.change_in_pct(latest[1], previous[1])
        else:
            change = indicators.change_in_pp(latest[1], previous[1])
    return Card(
        key=metric,
        title=spec.label,
        value_text=format_value(latest[1] if latest else None, spec.unit),
        obs_date=latest[0] if latest else None,
        source=spec.source,
        status=_status_of(statuses, metric),
        change_text=format_change(spec.change_kind, change),
        note=spec.note,
        status_note=str((statuses.get(metric) or {}).get('note') or ''),
    )


def _build_cards(
    statuses: dict[str, dict], series: dict[str, list[tuple[date, float]]]
) -> list[Card]:
    cards = [
        _card_from_series('us10y', series, statuses),
        _card_from_series('cn10y', series, statuses),
        _card_from_series('usdcny', series, statuses),
        _card_from_series('xauusd', series, statuses),
    ]

    # A股情绪参考：两项，不生成综合评分
    ratio_rows = advance_ratio_series(
        series.get('adv_count') or [],
        series.get('dec_count') or [],
        series.get('flat_count') or [],
    )
    latest_ratio = _latest(ratio_rows)
    prev_ratio = _previous(ratio_rows)
    ratio_change = (
        indicators.change_in_pp(latest_ratio[1], prev_ratio[1]) if latest_ratio and prev_ratio else None
    )
    hs300_rows = series.get('hs300_close') or []
    hs300_change = rolling_change(hs300_rows, 20)
    breadth_status = _status_of(statuses, 'adv_count')
    hs300_status = _status_of(statuses, 'hs300_close')
    cards.append(
        Card(
            key='sentiment',
            title='A股市场情绪参考',
            value_text=(
                f'{latest_ratio[1]:.2f}%' if latest_ratio else '未接通'
            ),
            obs_date=latest_ratio[0] if latest_ratio else None,
            source='东方财富妙想数据（沪深A股涨跌家数、沪深300收盘价）',
            status=_worse_status(breadth_status, hs300_status),
            change_text=format_change('pp', ratio_change),
            sub_lines=[
                (
                    '沪深A股上涨家数占比',
                    f'{latest_ratio[1]:.2f}%' if latest_ratio else '未接通',
                ),
                (
                    '沪深300近20个交易日涨跌幅',
                    f'{hs300_change:+.2f}%' if hs300_change is not None else '样本不足',
                ),
            ],
            note='情绪参考，非官方情绪指数，不生成综合评分。',
            status_note=str((statuses.get('adv_count') or {}).get('note') or ''),
        )
    )

    spread = _build_spread(statuses, series)
    cards.append(
        Card(
            key='spread',
            title='沪深300股债利差',
            value_text=(
                f'{indicators.to_percentage_points(spread.stats.value):.2f} 个百分点'
                f'（{indicators.to_bp(spread.stats.value):,.0f} BP）'
                if spread.stats
                else '未接通'
            ),
            obs_date=spread.obs_date,
            source='沪深300 PE-TTM（妙想） + 中债10年期国债收益率',
            status=spread.status,
            change_text=format_change('bp', spread.change_bp),
            sub_lines=[
                (
                    '百分位',
                    f'{spread.stats.percentile:.1f}%（{spread.stats.window_label}）'
                    if spread.stats and spread.stats.percentile is not None
                    else '样本不足，不显示百分位',
                ),
            ],
            status_note=str((statuses.get('hs300_pe_ttm') or {}).get('note') or ''),
        )
    )
    return cards


_STATUS_SEVERITY = {
    'normal': 0,
    'stale': 1,
    'insufficient': 2,
    'missing': 3,
    'failed': 4,
    'unconfigured': 5,
}


def _worse_status(*statuses: str) -> str:
    return max(statuses, key=lambda item: _STATUS_SEVERITY.get(item, 0))


def _build_spread(
    statuses: dict[str, dict], series: dict[str, list[tuple[date, float]]]
) -> SpreadView:
    pe_rows = series.get('hs300_pe_ttm') or []
    yield_rows = series.get('cn10y') or []
    spread_series = indicators.build_spread_series(pe_rows, yield_rows)
    stats = indicators.percentile_stats(spread_series)
    latest = _latest(spread_series)
    previous = _previous(spread_series)
    change_bp = indicators.change_in_bp(latest[1], previous[1]) if latest and previous else None

    pe_lookup = dict(pe_rows)
    yield_lookup = dict(yield_rows)
    obs_date = latest[0] if latest else None
    base_status = _worse_status(_status_of(statuses, 'hs300_pe_ttm'), _status_of(statuses, 'cn10y'))
    status = base_status
    if stats is None:
        status = 'missing'
    elif stats.insufficient and base_status == 'normal':
        status = 'insufficient'

    return SpreadView(
        series=spread_series,
        stats=stats,
        pe_ttm=pe_lookup.get(obs_date) if obs_date else None,
        cn10y_pct=yield_lookup.get(obs_date) if obs_date else None,
        obs_date=obs_date,
        change_bp=change_bp,
        status=status,
    )


def _build_alerts(cards: list[Card], spread: SpreadView) -> list[str]:
    alerts: list[str] = []
    for card in cards:
        if card.status in {'unconfigured', 'failed', 'missing'}:
            detail = f'（{card.status_note}）' if card.status_note else ''
            alerts.append(f'{card.title}：{card.status_label}{detail}')
    if spread.stats and spread.stats.insufficient:
        alerts.append('股债利差：有效样本不足，不显示百分位')
    elif any(card.status == 'stale' for card in cards):
        stale_titles = [card.title for card in cards if card.status == 'stale']
        alerts.append('以下指标为休市沿用（非当日数据）：' + '、'.join(stale_titles))
    return alerts
