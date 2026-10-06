"""分量注册表：默认（规格书）配置与推荐的多尺度画像。

默认画像的方向分量 = 252 日单尺度，数值与规格书一致；
PROFILE_MULTISCALE_SUGGESTED 把快变量（涨跌停比、家数占比）同时看短尺度与中尺度，
属于待评审的建议值，不会自动生效。

参与度指数是固定口径（不随窗口旋钮变化）：
  参与度指数 = 0.5 × 量比得分（阈值映射）+ 0.5 × 两融交易额 20 日分位
两融交易额 = 融资买入额 + 融资偿还额（无符号），所以参与度轴不表达多空方向；
净买入（有符号）仍照常采集与展示，但不进入指数合成。
换手率与成交额也照常采集与展示（换手率另用于恐慌抛售得分），同样不参与指数合成。
"""

from __future__ import annotations

from .config import ComponentSpec, ScaleSpec, fixed_window_component

# 输出列命名规则：
#   direct  : 每个尺度一列 {name}_pct_{window}；多尺度时另有混合列 {name}_pct
#   reverse : 混合列 {name}_reverse_pct（与规格书一致，不带窗口后缀）；多尺度时另有 {name}_reverse_pct_{window}
#   计数列  : {name}_pct_n / {name}_reverse_pct_n（主尺度的有效样本数）


def value_column(name: str, sign: str) -> str:
    return f'{name}_pct' if sign == 'direct' else f'{name}_reverse_pct'


def scale_column(name: str, sign: str, window: int) -> str:
    return f'{value_column(name, sign)}_{window}'


def count_column(name: str, sign: str) -> str:
    return f'{value_column(name, sign)}_n'


def score_column(name: str, sign: str = 'direct') -> str:
    """阈值映射分量的取值列（`level_map` 分量不使用分位，因此不叫 `_pct`）。"""
    return f'{name}_score' if sign == 'direct' else f'{name}_reverse_score'


def score_count_column(name: str, sign: str = 'direct') -> str:
    return f'{score_column(name, sign)}_n'


def price_ma_column(window: int) -> str:
    """价格位置用的均线列名（窗口可配，与 `sentiment_pct_{window}` 同一命名风格）。"""
    return f'price_ma_{int(window)}'


def emitted_columns(component: ComponentSpec) -> tuple[str, ...]:
    """该分量实际输出的列（顺序固定）。"""
    if component.transform == 'level_map':
        return (
            score_column(component.name, component.sign),
            score_count_column(component.name, component.sign),
        )
    multi = len(component.scales) > 1
    columns: list[str] = []
    if component.sign == 'direct':
        columns.extend(scale_column(component.name, component.sign, s.window) for s in component.scales)
        if multi:
            columns.append(value_column(component.name, component.sign))
    else:
        columns.append(value_column(component.name, component.sign))
        if multi:
            columns.extend(
                scale_column(component.name, component.sign, s.window) for s in component.scales
            )
    columns.append(count_column(component.name, component.sign))
    return tuple(columns)


def primary_scale(component: ComponentSpec) -> ScaleSpec:
    """主尺度：权重最大者，并列时取窗口最大者。"""
    return max(component.scales, key=lambda s: (s.weight, s.window))


def value_series_column(component: ComponentSpec) -> str:
    """该分量进入指数合成时使用的取值列（与 `FeatureBundle.values` 同一序列）。

    - `level_map`：阈值映射列；
    - 反向或多尺度：混合列；
    - 单尺度 direct：主尺度列。
    """
    if component.transform == 'level_map':
        return score_column(component.name, component.sign)
    if component.sign == 'reverse' or len(component.scales) > 1:
        return value_column(component.name, component.sign)
    return scale_column(component.name, component.sign, primary_scale(component).window)


def _spec(
    name: str,
    source: str,
    group: str,
    scales: tuple[ScaleSpec, ...],
    *,
    sign: str = 'direct',
    availability_lag: int = 0,
    horizon: str = 'mid',
    transform: str = 'percentile',
    level_map: tuple[tuple[float, float], ...] = (),
    in_index: bool = True,
) -> ComponentSpec:
    return ComponentSpec(
        name=name,
        source=source,
        group=group,  # type: ignore[arg-type]
        sign=sign,  # type: ignore[arg-type]
        scales=scales,
        availability_lag=availability_lag,
        horizon=horizon,  # type: ignore[arg-type]
        transform=transform,  # type: ignore[arg-type]
        level_map=level_map,
        in_index=in_index,
    )


_W252 = (ScaleSpec(252),)

# --- 参与度指数：固定口径，不随窗口旋钮变化 ------------------------------------

# 量比阈值映射锚点：量比 0.5 -> 0 分、1.0 -> 50 分、2.0 -> 100 分，中间分段线性，
# 两端截断。取值只由锚点决定：不取分位、不看历史样本。
VOLUME_RATIO_LEVEL_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.5, 0.0),
    (1.0, 50.0),
    (2.0, 100.0),
)
# 量比自带的 20 日均量窗口与两融交易额的分位窗口（交易日，约 1 个月）
VOLUME_MA_WINDOW: int = 20
MARGIN_TURNOVER_WINDOW: int = 20

# 参与度指数 = 0.5 × 量比得分 + 0.5 × 两融交易额 20 日分位（等权）。
# 只放无符号量：净买入（有符号）与方向指数同源，放进来会让两条轴不正交。
#
# `margin_turnover` 显式不使用 `availability_lag`（取值 0）：两融 T 日盘后不公布，
# 用 shift(1) 拿 T-1 的观测会把昨天的活跃度记到今天头上，读数与"数据日期"不符。
# 改为当日缺了就算缺——`compose_index` 按可用分量重归一，覆盖率 0.5 恰好通过
# `min_index_coverage`，于是当日两融未发布时参与度 = 量比得分（权重 100%）。
# 代价是最新交易日的读数会在两融发布后被重算（当日为量比口径，次日转为等权口径）。
PARTICIPATION_INDEX_COMPONENTS: tuple[ComponentSpec, ...] = (
    _spec(
        'volume_ratio',
        'volume_ratio',
        'participation',
        (ScaleSpec(VOLUME_MA_WINDOW, min_periods=VOLUME_MA_WINDOW),),
        transform='level_map',
        level_map=VOLUME_RATIO_LEVEL_ANCHORS,
    ),
    _spec(
        'margin_turnover',
        'margin_turnover',
        'participation',
        (ScaleSpec(MARGIN_TURNOVER_WINDOW),),
        availability_lag=0,
    ),
)

# --- 默认画像（规格书口径：方向分量 252 日单尺度） -----------------------------
DEFAULT_COMPONENTS: tuple[ComponentSpec, ...] = (
    # 换手率仍用于恐慌抛售得分（换手率分位）与页面展示，但不参与指数合成。
    _spec('turnover', 'turnover_rate', 'participation', _W252, in_index=False),
    _spec('amount', 'amount', 'participation', _W252, in_index=False),
    *PARTICIPATION_INDEX_COMPONENTS,
    _spec('return_20d', 'return', 'direction', _W252),
    _spec('macd_hist_norm', 'macd_hist_norm', 'direction', _W252),
    _spec('limit_up_down_ratio', 'limit_up_down_ratio', 'direction', _W252),
    _spec('up_ratio', 'up_ratio', 'direction', _W252),
    _spec('pcr', 'pcr', 'direction', _W252, sign='reverse'),
    _spec('iv', 'implied_volatility', 'direction', _W252, sign='reverse'),
)

# --- 推荐画像（待评审，不自动生效） -------------------------------------------

# 短窗口画像：全部 21 个交易日（约 1 个月）。
#
# 注意一个统计约束：`volume_ratio`（20 日均量）、`return_20d`（20 日收益）、
# `macd_hist_norm`（EMA12/26/9，等效平滑期约 17~20 日）本身就是 20 日量级的平滑量，
# 用 21 日窗口给它们做分位，窗口里只有 1~2 个互相独立的观测，
# 分位更多反映"相邻两天的微小差别"而不是"位置"。因此提供混合画像：
# 未平滑的日频分量用 21 日，自带 20 日平滑的分量用 63 日（≥ 3 倍平滑期）。
_W21 = (ScaleSpec(21),)
_W63 = (ScaleSpec(63),)

PROFILE_FAST: tuple[ComponentSpec, ...] = (
    _spec('turnover', 'turnover_rate', 'participation', _W21, horizon='short', in_index=False),
    _spec('amount', 'amount', 'participation', _W21, horizon='short', in_index=False),
    *PARTICIPATION_INDEX_COMPONENTS,
    _spec('return_20d', 'return', 'direction', _W21, horizon='short'),
    _spec('macd_hist_norm', 'macd_hist_norm', 'direction', _W21, horizon='short'),
    _spec('limit_up_down_ratio', 'limit_up_down_ratio', 'direction', _W21, horizon='short'),
    _spec('up_ratio', 'up_ratio', 'direction', _W21, horizon='short'),
    _spec('pcr', 'pcr', 'direction', _W21, sign='reverse', horizon='short'),
    _spec('iv', 'implied_volatility', 'direction', _W21, sign='reverse', horizon='short'),
)

PROFILE_FAST_MIXED: tuple[ComponentSpec, ...] = (
    _spec('turnover', 'turnover_rate', 'participation', _W21, horizon='short', in_index=False),
    _spec('amount', 'amount', 'participation', _W21, horizon='short', in_index=False),
    *PARTICIPATION_INDEX_COMPONENTS,
    _spec('return_20d', 'return', 'direction', _W63, horizon='short'),
    _spec('macd_hist_norm', 'macd_hist_norm', 'direction', _W63, horizon='short'),
    _spec('limit_up_down_ratio', 'limit_up_down_ratio', 'direction', _W21, horizon='short'),
    _spec('up_ratio', 'up_ratio', 'direction', _W21, horizon='short'),
    _spec('pcr', 'pcr', 'direction', _W21, sign='reverse', horizon='short'),
    _spec('iv', 'implied_volatility', 'direction', _W21, sign='reverse', horizon='short'),
)


def with_percentile_window(
    components: tuple[ComponentSpec, ...],
    window: int,
) -> tuple[ComponentSpec, ...]:
    """把分量的尺度统一替换成单一窗口（做窗口敏感性实验用）。

    参与度指数的两个分量是固定口径（量比阈值映射、两融交易额 20 日分位），
    因此跳过 `fixed_window_component`，窗口旋钮只作用于方向指数与不参与指数的输入。
    """
    from dataclasses import replace

    return tuple(
        component
        if fixed_window_component(component)
        else replace(component, scales=(ScaleSpec(window),))
        for component in components
    )


def config_for_profile(
    components: tuple[ComponentSpec, ...] = PROFILE_FAST,
    *,
    percentile_window: int | None = None,
    method: str | None = None,
):
    """把画像变成完整配置：`percentile_window` 同时作用于分量与综合情绪自身的分位。

    用法：`compute_sentiment(df, config_for_profile(PROFILE_FAST, percentile_window=21))`
    """
    from dataclasses import replace

    from .config import SentimentConfig, SentimentQuality, SentimentWindows

    if percentile_window is None:
        percentile_window = max(scale.window for component in components for scale in component.scales)
    quality_kwargs = {'components': components}
    if method is not None:
        quality_kwargs['percentile_method'] = method
    windows = replace(
        SentimentWindows(), sentiment_percentile_scales=(ScaleSpec(int(percentile_window)),)
    )
    return SentimentConfig(
        windows=windows,
        quality=SentimentQuality(**quality_kwargs),  # type: ignore[arg-type]
    )


# --- 窗口旋钮：三个层级，越往下越细 -------------------------------------------

# 1) 预设名 -> 交易日数
WINDOW_PRESETS: dict[str, int] = {
    '1个月': 21,
    '1个季度': 63,
    '半年': 126,
    '1年': 252,
}

# 与分量自带平滑期（20 日收益 / EMA12-26-9）相称的最短窗口。
# 量比不再取分位（改为阈值映射），因此不在这个名单里。
_SMOOTHED_MIN_WINDOW = 63
_SMOOTHED_COMPONENTS = ('return_20d', 'macd_hist_norm')


def resolve_window(window: int | str) -> int:
    """窗口可以是交易日数，也可以是 WINDOW_PRESETS 里的名字。"""
    if isinstance(window, str):
        try:
            return int(WINDOW_PRESETS[window])
        except KeyError as exc:
            raise ValueError(
                f'未知窗口预设：{window!r}；可用：{sorted(WINDOW_PRESETS)} 或直接给交易日数'
            ) from exc
    value = int(window)
    if value < 2:
        raise ValueError(f'窗口必须 >= 2 个交易日，收到 {value}')
    return value


def config_for_window(
    window: int | str = 252,
    *,
    profile: str = 'spec',
    **quality_overrides: object,
):
    """一行换窗口：`config_for_window(63)` 或 `config_for_window('1个季度')`。

    `profile` 决定分量结构：
    - `'spec'`（默认）：全部单尺度，统一用 `window`；
    - `'fast'`：同 `'spec'` 的结构（保留这个名字便于语义化）；
    - `'mixed'`：未平滑分量用 `window`，自带 20 日平滑的三个分量用
      `max(window, 63)`，避免窗口比分量自身平滑期还短；
    - `'multiscale'`：使用 §3.4 的多尺度画像，此时 `window` 只作用于综合情绪分位。
    """
    from dataclasses import replace

    from .config import SentimentConfig, SentimentQuality, SentimentWindows

    size = resolve_window(window)
    try:
        base = PROFILE_PRESETS[profile]
    except KeyError as exc:
        raise ValueError(f'未知画像：{profile!r}；可用：{sorted(PROFILE_PRESETS)}') from exc
    if profile == 'multiscale':
        components = base
    elif profile == 'mixed':
        components = tuple(
            component
            if fixed_window_component(component)
            else replace(
                component,
                scales=(
                    ScaleSpec(
                        size
                        if component.name not in _SMOOTHED_COMPONENTS
                        else max(size, _SMOOTHED_MIN_WINDOW)
                    ),
                ),
            )
            for component in base
        )
    else:
        components = with_percentile_window(base, size)
    windows = replace(
        SentimentWindows(), sentiment_percentile_scales=(ScaleSpec(size),)
    )
    return SentimentConfig(
        windows=windows,
        quality=SentimentQuality(components=components, **quality_overrides),  # type: ignore[arg-type]
    )


def default_config():
    """项目默认配置：读取 `macroboard.config` 里的窗口常量。

    后期调整窗口只需要改 `macroboard/config.py` 里的一行常量，
    页面、脚本与测试走同一个入口。
    """
    from macroboard import config as project_config

    return config_for_window(
        getattr(project_config, 'SENTIMENT_PERCENTILE_WINDOW', 252),
        profile=getattr(project_config, 'SENTIMENT_WINDOW_PROFILE', 'spec'),
    )


PROFILE_MULTISCALE_SUGGESTED: tuple[ComponentSpec, ...] = (
    _spec('turnover', 'turnover_rate', 'participation',
          (ScaleSpec(63, 0.4), ScaleSpec(252, 0.6)), horizon='mixed', in_index=False),
    _spec('amount', 'amount', 'participation',
          (ScaleSpec(63, 0.4), ScaleSpec(252, 0.6)), horizon='mixed', in_index=False),
    *PARTICIPATION_INDEX_COMPONENTS,
    _spec('return_20d', 'return', 'direction',
          (ScaleSpec(63, 0.4), ScaleSpec(252, 0.6)), horizon='mixed'),
    _spec('macd_hist_norm', 'macd_hist_norm', 'direction',
          (ScaleSpec(126, 0.5), ScaleSpec(252, 0.5)), horizon='mixed'),
    _spec('limit_up_down_ratio', 'limit_up_down_ratio', 'direction',
          (ScaleSpec(63, 0.6), ScaleSpec(252, 0.4)), horizon='mixed'),
    _spec('up_ratio', 'up_ratio', 'direction',
          (ScaleSpec(63, 0.5), ScaleSpec(252, 0.5)), horizon='mixed'),
    _spec('pcr', 'pcr', 'direction', _W252, sign='reverse'),
    _spec('iv', 'implied_volatility', 'direction', _W252, sign='reverse'),
)

# 规格书要求的默认输出列（供契约测试使用）
SPEC_REQUIRED_COMPONENT_COLUMNS: tuple[str, ...] = (
    'turnover_pct_252',
    'amount_pct_252',
    'volume_ratio_score',
    'margin_turnover_pct_20',
    'return_20d_pct_252',
    'macd_hist_norm_pct_252',
    'limit_up_down_ratio_pct_252',
    'up_ratio_pct_252',
    'pcr_reverse_pct',
    'iv_reverse_pct',
)

# 2) 画像名 -> 分量结构（放在常量定义之后，供 config_for_window 使用）
PROFILE_PRESETS: dict[str, tuple[ComponentSpec, ...]] = {
    'spec': DEFAULT_COMPONENTS,          # 规格默认：全部单尺度（窗口由 config_for_window 决定）
    'fast': PROFILE_FAST,                # 全部同一窗口（短窗口时噪声最高）
    'mixed': PROFILE_FAST_MIXED,         # 自带 20 日平滑的分量用更长窗口
    'multiscale': PROFILE_MULTISCALE_SUGGESTED,  # 每个分量自带多尺度，忽略 window
}
