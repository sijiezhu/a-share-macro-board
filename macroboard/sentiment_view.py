"""情绪指标的页面数据组装（只读数据库序列，不做任何网络请求）。

职责：
- 把库里的原始序列适配成 `macroboard.sentiment` 需要的输入表；
- 调用情绪模块，产出页面直接可用的快照、曲线与口径说明；
- 逐项标注数据日期、状态与「未接通」字段，不做任何建议性表述。

口径（与 docs/DATA_SOURCES.md 一致）：
- 只做同一日期对齐，不重采样、不前向填充；
- `adv_count`/`dec_count` → `up_count`/`down_count`；`hs300_close` → `close`；
- `pcr` / `implied_volatility` 未接通：不伪造，交给算法走「不可用」分支。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from . import db
from .config import SERIES, STATUS_LABELS
from .dashboard import worse_status
from .sentiment import compute_sentiment, default_config, sentiment_snapshot
from .sentiment.config import SentimentConfig
from .sentiment.labels import (
    BAND_LABELS,
    DATA_QUALITY_LABELS,
    MACD_VOL_STATE_LABELS,
    PANIC_REASON_LABELS,
    REV_BLOCK_REASON_LABELS,
    REV_REASON_LABELS,
    STATE_LABELS,
    label_history,
)

# 情绪模块输入列 -> 数据库指标
FIELD_MAP: tuple[tuple[str, str], ...] = (
    ('close', 'hs300_close'),
    ('volume', 'volume'),
    ('amount', 'amount'),
    ('turnover_rate', 'turnover_rate'),
    ('margin_net_buy', 'margin_net_buy'),
    ('up_count', 'adv_count'),
    ('down_count', 'dec_count'),
    ('limit_up_count', 'limit_up_count'),
    ('limit_down_count', 'limit_down_count'),
)

# 暂不纳入的分量：数据源未接通（妙想问法未命中），页面口径里直接不包含它们，
# 而不是把它们当作"缺失"反复提示。等权合成下，纳入/不纳入的数值等价——
# `compose_index` 会按可用分量重归一，所以方向指数 = 其余分量的等权平均。
# 以后接通了只要从这个元组里移除即可，无需改算法。
OPTIONAL_OFF_COMPONENTS: tuple[str, ...] = ('pcr', 'iv')


@dataclass(frozen=True)
class InputFieldInfo:
    column: str
    metric: str
    label: str
    unit: str
    latest_date: date | None
    status: str
    available: bool


@dataclass(frozen=True)
class SentimentView:
    available: bool
    status: str
    as_of: date | None
    reason: str
    snapshot: dict[str, object]
    curves: dict[str, list[tuple[date, float]]]
    quadrant_points: list[tuple[date, float, float]]
    used_rows: int
    total_rows: int
    fields: tuple[InputFieldInfo, ...]
    window: int
    profile: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    percentile_method: str = 'midrank_exclusive'
    notes: tuple[str, ...] = ()


def view_config() -> SentimentConfig:
    """页面使用的配置：沿用项目默认窗口，但剔除暂未接通的分量。

    等权合成下数值等价（方向指数 = 其余分量等权平均），区别只在于页面不再
    把 pcr / iv 报成"缺失"，覆盖率与数据质量也不会因此长期显示为"部分不可用"。
    """
    base = default_config()
    components = tuple(
        component
        for component in base.quality.components
        if component.name not in OPTIONAL_OFF_COMPONENTS
    )
    return base.replace(quality={'components': components})


def build_input_frame(series: Mapping[str, list[tuple[date, float]]]) -> pd.DataFrame:
    """库序列 -> 情绪模块输入表：按日期并集对齐，不重采样、不前向填充。"""
    columns: dict[str, pd.Series] = {}
    for column, metric in FIELD_MAP:
        rows = series.get(metric) or []
        if not rows:
            continue
        columns[column] = pd.Series({day: float(value) for day, value in rows})
    if not columns:
        return pd.DataFrame(columns=['date'])
    frame = pd.DataFrame(columns)
    frame.index = pd.to_datetime(frame.index)
    frame = frame.reset_index(names='date').sort_values('date', kind='stable')
    return frame.reset_index(drop=True)


def _field_infos(
    series: Mapping[str, list[tuple[date, float]]],
    statuses: Mapping[str, Mapping[str, object]],
) -> tuple[InputFieldInfo, ...]:
    infos: list[InputFieldInfo] = []
    for column, metric in FIELD_MAP:
        spec = SERIES.get(metric)
        rows = series.get(metric) or []
        status_row = statuses.get(metric) or {}
        infos.append(
            InputFieldInfo(
                column=column,
                metric=metric,
                label=spec.label if spec else metric,
                unit=spec.unit if spec else '',
                latest_date=rows[-1][0] if rows else None,
                status=str(status_row.get('status') or ('missing' if not rows else 'normal')),
                available=bool(rows),
            )
        )
    return tuple(infos)


def build_sentiment_view(
    series: Mapping[str, list[tuple[date, float]]],
    statuses: Mapping[str, Mapping[str, object]] | None = None,
    *,
    config: SentimentConfig | None = None,
) -> SentimentView:
    """组装情绪页面数据；任何一步数据不足都返回可解释的"未接通/样本不足"。"""
    cfg = config or view_config()
    statuses = statuses or {}
    fields = _field_infos(series, statuses)
    frame = build_input_frame(series)
    window = cfg.windows.sentiment_percentile_scales[0].window
    profile = {
        component.name: tuple(scale.window for scale in component.scales)
        for component in cfg.quality.components
    }
    if len(frame) == 0:
        return SentimentView(
            available=False,
            status='missing',
            as_of=None,
            reason='情绪指标所需的库内序列尚未采集，请先运行 update_data.py。',
            snapshot=_empty_snapshot(),
            curves={},
            quadrant_points=[],
            used_rows=0,
            total_rows=0,
            fields=fields,
            window=window,
            profile=profile,
            percentile_method=cfg.quality.percentile_method,
        )

    result = compute_sentiment(frame, cfg)
    snapshot = sentiment_snapshot(result, cfg)
    curves = {
        key: _curve(result, key)
        for key in ('sentiment_index', 'participation_index', 'direction_index')
    }
    quadrant_points = _quadrant_points(result)
    used_rows = int(result['sentiment_index'].notna().sum())
    available = used_rows > 0
    if available:
        input_status = worse_status(
            *[info.status for info in fields if info.available] or ['normal']
        )
        status = input_status
        reason = ''
    else:
        status = 'insufficient'
        reason = (
            f'样本不足：分位窗口 {window} 个交易日，当前可用数据不足，暂不显示情绪读数'
            '（预热期内不写"中性"，避免被误读）。'
        )
    notes = _notes(cfg, fields, used_rows)
    return SentimentView(
        available=available,
        status=status,
        as_of=None if snapshot.get('as_of') is None else date.fromisoformat(str(snapshot['as_of'])),
        reason=reason,
        snapshot=snapshot,
        curves=curves,
        quadrant_points=quadrant_points,
        used_rows=used_rows,
        total_rows=int(len(result)),
        fields=fields,
        window=window,
        profile=profile,
        percentile_method=cfg.quality.percentile_method,
        notes=notes,
    )


def _curve(frame: pd.DataFrame, column: str) -> list[tuple[date, float]]:
    if column not in frame.columns:
        return []
    rows = frame[['date', column]].dropna()
    return [(day.date(), float(value)) for day, value in zip(rows['date'], rows[column], strict=True)]


def _quadrant_points(frame: pd.DataFrame) -> list[tuple[date, float, float]]:
    """(日期, 参与度, 方向) 三元组，只保留两个指数都有效的交易日。"""
    columns = ('participation_index', 'direction_index')
    if any(column not in frame.columns for column in columns):
        return []
    rows = frame[['date', *columns]].dropna()
    return [
        (day.date(), float(participation), float(direction))
        for day, participation, direction in zip(
            rows['date'], rows[columns[0]], rows[columns[1]], strict=True
        )
    ]


def _empty_snapshot() -> dict[str, object]:
    return {
        'as_of': None,
        'state': 'STATE_UNAVAILABLE',
        'state_label': STATE_LABELS['STATE_UNAVAILABLE'],
        'panic_reasons': [],
        'panic_reasons_text': '无',
        'reversal_watch': False,
        'reversal_reasons': [],
        'reversal_reasons_text': '无',
        'reversal_block_reason_text': None,
        'macd_volume_state_label': '未接通',
        'data_quality_label': DATA_QUALITY_LABELS['DQ_INSUFFICIENT'],
    }


def _notes(
    cfg: SentimentConfig,
    fields: tuple[InputFieldInfo, ...],
    used_rows: int,
) -> tuple[str, ...]:
    notes: list[str] = []
    missing = [info.label for info in fields if not info.available]
    lagged = [
        component.name
        for component in cfg.quality.components
        if component.availability_lag
    ]
    if missing:
        notes.append('库中尚未采集到的字段：' + '、'.join(missing))
    if lagged:
        notes.append('按公布时点滞后使用的分量：' + '、'.join(lagged) + '（两融 T+1）')
    notes.append(
        f'方向指数由 {len([c for c in cfg.quality.components if c.group == "direction"])} 个分量等权合成，'
        f'参与度由 {len([c for c in cfg.quality.components if c.group == "participation"])} 个分量等权合成'
    )
    notes.append(
        f'已有情绪读数的交易日：{used_rows} 天'
        f'（分位窗口 {cfg.windows.sentiment_percentile_scales[0].window} 天）'
    )
    notes.append('分位口径：严格早于当日 + 中位秩（与股债利差分位同一排名公式）')
    return tuple(notes)


def render_context(view: SentimentView) -> dict[str, str]:
    """把快照里的码值渲染成页面文案（供 app.py 直接使用）。"""
    snapshot = view.snapshot
    return {
        'state_label': str(snapshot.get('state_label') or STATE_LABELS['STATE_UNAVAILABLE']),
        'participation_band_text': BAND_LABELS.get(str(snapshot.get('participation_band')), '—'),
        'direction_band_text': BAND_LABELS.get(str(snapshot.get('direction_band')), '—'),
        'panic_text': label_history(snapshot.get('panic_reasons'), PANIC_REASON_LABELS),
        'panic_missing_text': label_history(snapshot.get('panic_missing'), PANIC_REASON_LABELS),
        'reversal_text': label_history(snapshot.get('reversal_reasons'), REV_REASON_LABELS),
        'reversal_missing_text': label_history(snapshot.get('reversal_missing'), REV_REASON_LABELS),
        'block_reason_text': (
            REV_BLOCK_REASON_LABELS.get(str(snapshot['reversal_block_reason']), str(snapshot['reversal_block_reason']))
            if snapshot.get('reversal_block_reason')
            else '未触发门控'
        ),
        'macd_volume_label': str(snapshot.get('macd_volume_state_label') or '未接通'),
        'quality_label': str(snapshot.get('data_quality_label') or DATA_QUALITY_LABELS['DQ_INSUFFICIENT']),
    }


def state_text(code: str) -> str:
    return STATE_LABELS.get(code, STATE_LABELS['STATE_UNAVAILABLE'])


def field_frame(view: SentimentView) -> pd.DataFrame:
    """输入字段的数据日期/状态表（页面直接展示，逐项标注）。"""
    return pd.DataFrame(
        [
            {
                '输入字段': info.column,
                '库内指标': info.label,
                '数据日期': info.latest_date.isoformat() if info.latest_date else '—',
                '状态': STATUS_LABELS.get(info.status, info.status),
                '单位': info.unit,
            }
            for info in view.fields
        ]
    )


def macd_state_text(code: str) -> str:
    return MACD_VOL_STATE_LABELS.get(code, '未接通')


def load_series_and_statuses(db_path) -> tuple[dict[str, list[tuple[date, float]]], dict[str, dict]]:
    """只读打开数据库，取出全部序列与状态。"""
    conn = db.connect(db_path)
    db.init_db(conn)
    statuses = {key: dict(row) for key, row in db.get_statuses(conn).items()}
    series: dict[str, list[tuple[date, float]]] = {}
    for key in SERIES:
        series[key] = [
            (date.fromisoformat(row['obs_date']), float(row['value']))
            for row in db.observations_all(conn, key)
        ]
    conn.close()
    return series, statuses
