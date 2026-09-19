"""指标定义、数据源说明与运行参数。

每个序列固定：来源、接口、字段、单位、历史覆盖与休市容忍窗口。任何口径变更
都必须同时修改本文件和 `docs/DATA_SOURCES.md`，不允许在采集代码里静默切换。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

APP_NAME = 'A股投资信息看板'
APP_TAGLINE = '宏观与估值观察 · 不构成投资建议 · 不输出买卖指令'

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / 'data' / 'macroboard.sqlite3'
DEFAULT_ENV_FILE = ROOT / '.env'

# 百分位参数
PERCENTILE_WINDOW_YEARS = 5
PERCENTILE_MIN_SAMPLES = 252

# 状态取值 → 中文标签
STATUS_LABELS = {
    'normal': '正常',
    'stale': '休市沿用',
    'failed': '更新失败',
    'missing': '无数据',
    'unconfigured': '未接通（缺少授权）',
    'insufficient': '样本不足',
}


@dataclass(frozen=True)
class SeriesSpec:
    """一个被采集并落库的原始序列。"""

    key: str
    label: str
    unit: str
    source: str
    source_detail: str
    change_kind: str = 'abs'  # bp（基点） | pct（百分比） | pp（百分点） | abs
    max_lag_days: int = 4  # 数据日期落后于今天的容忍天数（覆盖周末与长假）
    note: str = ''


SERIES: dict[str, SeriesSpec] = {
    'us10y': SeriesSpec(
        key='us10y',
        label='美国10年期国债收益率',
        unit='%',
        source='美国财政部 (U.S. Department of the Treasury)',
        source_detail='Daily Treasury Par Yield Curve Rates · 10 Yr（Par Yield，恒定期限名义收益率）',
        change_kind='bp',
        max_lag_days=4,
        note='非实际收益率（TIPS），非票面利率。日度完整序列。',
    ),
    'cn10y': SeriesSpec(
        key='cn10y',
        label='中债10年期国债收益率',
        unit='%',
        source='中债收益率曲线，经东方财富数据中心镜像',
        source_detail='东方财富数据中心 RPTA_WEB_TREASURYYIELD · 字段 EMM00166466（中国10年期国债到期收益率）',
        change_kind='bp',
        max_lag_days=10,
        note='中债官网直连接口当前未接通，改用固定镜像；已与 2024-01-15（2.5212%）等公开值核对。',
    ),
    'usdcny': SeriesSpec(
        key='usdcny',
        label='美元兑人民币（在岸即期）',
        unit='CNY/USD',
        source='新浪财经 在岸人民币行情',
        source_detail='fx_susdcny 日线（在岸人民币 / CNY），取日线收盘价',
        change_kind='pct',
        max_lag_days=4,
        note='数值上升表示人民币相对美元贬值。未使用中间价，也未使用离岸 USD/CNH。',
    ),
    'xauusd': SeriesSpec(
        key='xauusd',
        label='现货黄金参考价 (XAU/USD)',
        unit='USD/金衡盎司',
        source='LBMA (London Bullion Market Association)',
        source_detail='LBMA Gold Price PM 定盘价，报价时点 15:00 伦敦时间（Europe/London），美元/金衡盎司',
        change_kind='pct',
        max_lag_days=7,
        note='现货定盘参考价，不是期货价；周末与英国假日无报价。日线边界为伦敦交易日。',
    ),
    'adv_count': SeriesSpec(
        key='adv_count',
        label='沪深A股上涨家数',
        unit='家',
        source='东方财富妙想数据',
        source_detail='沪深A股(板块) 上涨家数',
        change_kind='abs',
        max_lag_days=10,
    ),
    'dec_count': SeriesSpec(
        key='dec_count',
        label='沪深A股下跌家数',
        unit='家',
        source='东方财富妙想数据',
        source_detail='沪深A股(板块) 下跌家数',
        change_kind='abs',
        max_lag_days=10,
    ),
    'flat_count': SeriesSpec(
        key='flat_count',
        label='沪深A股平盘家数',
        unit='家',
        source='东方财富妙想数据',
        source_detail='沪深A股(板块) 平盘家数',
        change_kind='abs',
        max_lag_days=10,
    ),
    'hs300_close': SeriesSpec(
        key='hs300_close',
        label='沪深300指数收盘点位',
        unit='点',
        source='东方财富妙想数据',
        source_detail='沪深300指数(399300.SZ) 收盘价',
        change_kind='pct',
        max_lag_days=10,
    ),
    'hs300_pe_ttm': SeriesSpec(
        key='hs300_pe_ttm',
        label='沪深300 PE-TTM',
        unit='倍',
        source='东方财富妙想数据',
        source_detail='沪深300指数(399300.SZ) 市盈率 PE-TTM（供应商计算口径，非成分股简单平均）',
        change_kind='pct',
        max_lag_days=12,
        note='用于股债利差，必须与国债收益率取同一数据日期。',
    ),
}

# 采集参数
LOOKBACK_DAYS = {
    'us10y': 400,        # 覆盖跨年比较
    'cn10y': 400,
    'usdcny': 400,
    'xauusd': 400,
    'a_share_sentiment': 30,
    'hs300_close': 120,
    'hs300_pe_ttm': 60,
}

# 首次补齐历史所需年限
# 股债利差与百分位需要 5 年窗口，因此估值与国债多补一年做余量；
# 其余指标同样回补 5 年，供页面绘制变化曲线。
SPREAD_BACKFILL_YEARS = 6
CHART_BACKFILL_YEARS = 5

# 页面曲线：近端保留全部观测，远端按周/月/季抽稀（只取该区间内最后一个真实观测，不做平均）
CHART_RECENT_FULL_DAYS = 90
CHART_WEEKLY_UNTIL_DAYS = 365
CHART_MONTHLY_UNTIL_DAYS = 3 * 365
CHART_RANGE_OPTIONS = {'1年': 365, '3年': 3 * 365, '5年': 5 * 365, '全部': None}

MX_ENV_VAR = 'MX_APIKEY'
