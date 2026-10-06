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

import numpy as np
import pandas as pd

from . import db
from .config import SERIES, STATUS_LABELS
from .dashboard import worse_status
from .sentiment import (
    MarginCompletenessDrop,
    analyze,
    codes,
    default_config,
    margin_completeness_warning,
    sentiment_snapshot,
)
from .sentiment.config import ComponentSpec, SentimentConfig
from .sentiment.labels import (
    BAND_LABELS,
    DATA_QUALITY_LABELS,
    MACD_VOL_STATE_LABELS,
    PANIC_REASON_LABELS,
    POSITION_LABELS,
    REV_BLOCK_REASON_LABELS,
    REV_REASON_LABELS,
    STATE_LABELS,
    label_history,
)
from .sentiment.registry import value_series_column
from .sentiment.scoring import STATE_COMBINATIONS

# 情绪模块输入列 -> 数据库指标
FIELD_MAP: tuple[tuple[str, str], ...] = (
    ('close', 'hs300_close'),
    ('sh_close', 'sh_close'),
    ('volume', 'volume'),
    ('amount', 'amount'),
    ('turnover_rate', 'turnover_rate'),
    ('margin_net_buy', 'margin_net_buy'),
    ('margin_turnover', 'margin_turnover'),
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

# 输入字段在指数里的用途（页面逐字段标注，避免把「采集了」误读成「参与了指数」）
FIELD_USAGE: Mapping[str, str] = {
    'close': '方向指数（20 日收益、MACD 柱）',
    'sh_close': '不参与指数（仅用于高位/低位判定：收盘 vs 60 日均线）',
    'volume': '参与度指数（量比得分，阈值映射）',
    'amount': '不参与指数（仅入库与展示）',
    'turnover_rate': '不参与指数（仍用于恐慌抛售得分的换手率分位）',
    'margin_net_buy': '不参与指数（有符号，保留采集与展示，供对照）',
    'margin_turnover': '参与度指数（20 日分位，T+1 滞后；融资买入 + 融资偿还，无符号）',
    'up_count': '方向指数（上涨家数占比）',
    'down_count': '方向指数（上涨家数占比）',
    'limit_up_count': '方向指数（涨跌停比）',
    'limit_down_count': '方向指数（涨跌停比）',
}

# 分量中文名（口径文案用；未登记的分量回退到英文名）
COMPONENT_LABELS: Mapping[str, str] = {
    'volume_ratio': '量比得分',
    'margin_turnover': '两融交易额分位',
    'margin_net_buy': '融资净买入分位',
    'turnover': '换手率分位',
    'amount': '成交额分位',
    'return_20d': '20 日收益分位',
    'macd_hist_norm': 'MACD 柱分位',
    'limit_up_down_ratio': '涨跌停比分位',
    'up_ratio': '上涨家数占比分位',
    'pcr': '认沽认购比分位',
    'iv': '隐含波动率分位',
}


def component_label(name: str) -> str:
    return COMPONENT_LABELS.get(name, name)


def compose_formula_text(title: str, components: list[ComponentSpec]) -> str:
    """按配置生成合成公式文案（权重按组内总和归一化，与实际计算一致）。"""
    if not components:
        return f'{title}：无可用分量'
    total = sum(component.weight for component in components) or 1.0
    parts = [
        f'{component.weight / total:.2f} × {component_label(component.name)}'
        f'（{component_profile_text(component)}）'
        for component in components
    ]
    return f'{title} = ' + ' + '.join(parts)


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
class ComponentValueInfo:
    """进入指数计算的一个分量及其最新取值（页面直接展示）。"""

    name: str
    label: str
    group: str
    method: str
    value: float | None
    value_date: date | None
    weight_share: float | None
    in_index: bool
    availability_lag: int


@dataclass(frozen=True)
class SentimentView:
    available: bool
    status: str
    as_of: date | None
    reason: str
    snapshot: dict[str, object]
    curves: dict[str, list[tuple[date, float]]]
    quadrant_points: list[tuple[date, float, float, str]]
    used_rows: int
    total_rows: int
    fields: tuple[InputFieldInfo, ...]
    window: int
    # 每个分量的当前口径文案（分位窗口或阈值映射锚点），供页面直接展示
    profile: Mapping[str, str] = field(default_factory=dict)
    # 参与指数计算的分量及其最新取值（含不参与指数的分量，用 in_index 区分）
    components: tuple[ComponentValueInfo, ...] = ()
    percentile_method: str = 'midrank_exclusive'
    notes: tuple[str, ...] = ()
    # 被两融完整性校验剔除的观测日（源站发布不完整），页面据此说明分量为何缺失
    margin_completeness: tuple[MarginCompletenessDrop, ...] = ()
    # 逐日价格位置 (日期, 位置码)，与上证指数系列同日；只做描述，不参与任何合成。
    # 页面拿它给上证指数那张图铺高位/低位底色（位置未知的日期不铺）。
    positions: list[tuple[date, str]] = field(default_factory=list)


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


def component_profile_text(component: ComponentSpec) -> str:
    """分量的当前口径文案：阈值映射写锚点，分位写窗口（多尺度写权重）。"""
    if component.transform == 'level_map':
        anchors = ' / '.join(f'{x:g}→{y:g}' for x, y in component.level_map)
        return f'阈值映射 {anchors}'
    return '+'.join(
        f'{scale.window}日×{scale.weight:g}' if scale.weight != 1.0 else f'{scale.window}日'
        for scale in component.scales
    )


def component_method_text(component: ComponentSpec) -> str:
    """分量口径的完整说明（含是否取分位与滞后），供表内逐行标注。"""
    if component.transform == 'level_map':
        text = f'{component_profile_text(component)}（不取分位，两端截断）'
    else:
        method = component.scales[0].method or 'midrank_exclusive'
        text = (
            f'{component_profile_text(component)}滚动分位'
            f'（{"严格早于当日 + 中位秩" if method == "midrank_exclusive" else "含当日 + 最大秩"}）'
        )
    if component.sign == 'reverse':
        text += '；反向（得分 = 100 − 分位）'
    if component.availability_lag:
        text += f'；T+{component.availability_lag} 滞后使用'
    return text


def _component_infos(
    result: pd.DataFrame,
    cfg: SentimentConfig,
) -> tuple[ComponentValueInfo, ...]:
    """从计算结果里取每个分量的最新取值与（最新一日实际生效的）组内权重。"""
    infos: list[ComponentValueInfo] = []
    if len(result) == 0 or 'date' not in result.columns:
        return ()
    last_pos = len(result) - 1
    dates = pd.to_datetime(result['date'])

    def column(component: ComponentSpec) -> pd.Series:
        name = value_series_column(component)
        if name not in result.columns:
            return pd.Series(np.nan, index=result.index, dtype='float64')
        return pd.to_numeric(result[name], errors='coerce')

    # 组内权重按当日可用分量重归一，与 compose_index 的实际计算一致
    shares: dict[str, float | None] = {}
    for group in ('participation', 'direction'):
        members = [
            component
            for component in cfg.quality.components
            if component.group == group and component.in_index
        ]
        usable = [
            component for component in members if not pd.isna(column(component).iloc[last_pos])
        ]
        total = sum(component.weight for component in usable)
        for component in members:
            shares[component.name] = (
                component.weight / total if component in usable and total > 0 else None
            )
    for component in cfg.quality.components:
        series = column(component)
        positions = np.flatnonzero(series.notna().to_numpy())
        pos = int(positions[-1]) if positions.size else None
        infos.append(
            ComponentValueInfo(
                name=component.name,
                label=component_label(component.name),
                group=component.group,
                method=component_method_text(component),
                value=None if pos is None else float(series.iloc[pos]),
                value_date=None if pos is None else pd.Timestamp(dates.iloc[pos]).date(),
                weight_share=shares.get(component.name),
                in_index=component.in_index,
                availability_lag=component.availability_lag,
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
        component.name: component_profile_text(component) for component in cfg.quality.components
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

    analysis = analyze(frame, cfg)
    result = analysis.frame
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
    notes = _notes(cfg, fields, used_rows, completeness=analysis.margin_completeness)
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
        components=_component_infos(result, cfg),
        percentile_method=cfg.quality.percentile_method,
        notes=notes,
        margin_completeness=analysis.margin_completeness,
        positions=_position_curve(result),
    )


def _curve(frame: pd.DataFrame, column: str) -> list[tuple[date, float]]:
    if column not in frame.columns:
        return []
    rows = frame[['date', column]].dropna()
    return [(day.date(), float(value)) for day, value in zip(rows['date'], rows[column], strict=True)]


def _position_curve(frame: pd.DataFrame) -> list[tuple[date, str]]:
    """逐日价格位置码（含 `POS_UNKNOWN`）：和曲线一样**不丢弃**位置未知的日期。

    位置未知是「均线还没满 60 个交易日 / 当日无上证指数观测」的如实标注，
    页面据此不铺底色，而不是拿别的日期的位置顶替。
    """
    if 'price_position' not in frame.columns or 'date' not in frame.columns:
        return []
    return [
        (day.date(), str(code))
        for day, code in zip(frame['date'], frame['price_position'], strict=True)
    ]


def _quadrant_points(frame: pd.DataFrame) -> list[tuple[date, float, float, str]]:
    """(日期, 参与度, 方向, 价格位置) 四元组，只保留两个指数都有效的交易日。

    位置不参与筛除：位置未知（老库缺 `sh_close`、或均线样本不足）的点照样画，
    标记为 `POS_UNKNOWN`，避免"位置缺失"被误读成"没有交易日"。
    """
    columns = ('participation_index', 'direction_index')
    if any(column not in frame.columns for column in columns):
        return []
    positions = frame['price_position'] if 'price_position' in frame.columns else None
    rows = frame[['date', *columns]].dropna()
    return [
        (
            day.date(),
            float(participation),
            float(direction),
            str(positions.loc[index]) if positions is not None else codes.POS_UNKNOWN,
        )
        for index, day, participation, direction in zip(
            rows.index, rows['date'], rows[columns[0]], rows[columns[1]], strict=True
        )
    ]


def _empty_snapshot() -> dict[str, object]:
    return {
        'as_of': None,
        'state': 'STATE_UNAVAILABLE',
        'state_label': STATE_LABELS['STATE_UNAVAILABLE'],
        'price_position': codes.POS_UNKNOWN,
        'price_position_label': POSITION_LABELS[codes.POS_UNKNOWN],
        'sh_close': None,
        'price_ma': None,
        'price_ma_window': None,
        'price_ma_gap': None,
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
    *,
    completeness: tuple[MarginCompletenessDrop, ...] = (),
) -> tuple[str, ...]:
    notes: list[str] = []
    missing = [info.label for info in fields if not info.available]
    lagged = [
        component.name
        for component in cfg.quality.components
        if component.availability_lag
    ]
    names = {component.name for component in cfg.quality.components}
    if missing:
        notes.append('库中尚未采集到的字段：' + '、'.join(missing))
    if lagged:
        notes.append(
            '按公布时点滞后使用的分量：'
            + '、'.join(component_label(name) for name in lagged)
            + '（t 日只用 ≤ t−1 的观测）'
        )
    if 'margin_turnover' in names:
        notes.append(
            '两融 T+1 公布：当日尚未（完整）发布时该分量按缺失处理（不取前一日的值顶替），'
            '参与度指数此时由量比得分独占（权重 100%），覆盖率标注为 0.5。'
        )
    if completeness:
        notes.append(
            margin_completeness_warning(
                tuple(completeness),
                window=int(cfg.quality.margin_completeness_window),
            )
            + '这些日期的参与度指数只由量比得分决定（覆盖率 0.5），'
            '源站补齐后会自动回到等权口径。'
        )
    index_components = [c for c in cfg.quality.components if c.in_index]
    participation = [c for c in index_components if c.group == 'participation']
    notes.append(compose_formula_text('参与度指数', participation))
    notes.append(
        f'方向指数由 {len([c for c in index_components if c.group == "direction"])} 个分量等权合成'
    )
    notes.append(
        '量比得分按阈值映射到 0—100（0.5→0、1.0→50、2.0→100，分段线性、两端截断），'
        '不取分位；换手率与成交额不参与指数。'
    )
    notes.append(
        f'已有情绪读数的交易日：{used_rows} 天'
        f'（分位窗口 {cfg.windows.sentiment_percentile_scales[0].window} 天）'
    )
    notes.append('分位口径：严格早于当日 + 中位秩（与股债利差分位同一排名公式）')
    return tuple(notes)


def position_text(snapshot: Mapping[str, object]) -> str:
    """位置行文案：把「收盘 / 均线 / 乖离」摊开写，并说明位置未知的原因。

    位置未知必须写明原因：否则页面会显示四象限基础文案，读者无从分辨
    这是"低位"还是"没有位置信息"。
    """
    code = str(snapshot.get('price_position') or codes.POS_UNKNOWN)
    window = snapshot.get('price_ma_window')
    window_text = f'{int(window)} 日' if isinstance(window, (int, float)) else '均线'
    level = snapshot.get('sh_close')
    average = snapshot.get('price_ma')
    gap = snapshot.get('price_ma_gap')
    if code == codes.POS_UNKNOWN or level is None or average is None:
        reason = '当日无上证指数观测' if level is None else f'上证指数 {window_text}均线样本不足'
        return f'位置：位置未知（{reason}，状态按四象限基础口径显示）'
    label = POSITION_LABELS.get(code, POSITION_LABELS[codes.POS_UNKNOWN])
    gap_text = f'（{float(gap):+.2%}）' if gap is not None else ''
    sign = '高于' if code == codes.POS_HIGH else '不高于'
    return f'位置：{label} · 上证指数 {float(level):,.1f} {sign} {window_text}均线 {float(average):,.1f}{gap_text}'


def state_reference_frame() -> pd.DataFrame:
    """位置 × 象限 -> 复合状态文案表（由 codes/labels 生成，页面与文档同一份口径）。"""
    rows = []
    for base, position, combined in STATE_COMBINATIONS:
        rows.append(
            {
                '位置': POSITION_LABELS.get(position, position),
                '四象限基础': STATE_LABELS.get(base, base),
                '组合状态': STATE_LABELS.get(combined, combined),
            }
        )
    return pd.DataFrame(rows)


def render_context(view: SentimentView) -> dict[str, str]:
    """把快照里的码值渲染成页面文案（供 app.py 直接使用）。"""
    snapshot = view.snapshot
    return {
        'state_label': str(snapshot.get('state_label') or STATE_LABELS['STATE_UNAVAILABLE']),
        'position_text': position_text(snapshot),
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
                '用途': FIELD_USAGE.get(info.column, '—'),
            }
            for info in view.fields
        ]
    )


def component_frame(view: SentimentView) -> pd.DataFrame:
    """分量取值表：进入指数计算的每个分量的最新取值、口径与组内权重。"""
    group_text = {'participation': '参与度指数', 'direction': '方向指数'}
    rows = []
    for info in view.components:
        rows.append(
            {
                '分量': info.label,
                '用途': group_text.get(info.group, info.group) if info.in_index else '不参与指数',
                '口径': info.method,
                '最新值': '—' if info.value is None else round(info.value, 1),
                '取值日期': info.value_date.isoformat() if info.value_date else '—',
                '组内权重': '—' if info.weight_share is None else f'{info.weight_share:.0%}',
            }
        )
    return pd.DataFrame(rows)


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
