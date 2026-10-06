"""展示层文案：把算法层的中性码值映射为中文。

算法层（`codes.py` / `registry.py` / `features.py` / `scoring.py`）不出现中文，
所有对外文案集中在本文件，便于统一口径与评审。
文案只描述状态，不含买卖建议、目标价或仓位提示。
"""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from . import codes

STATE_LABELS: dict[str, str] = {
    # 位置未知（缺少上证指数或 60 日均线样本不足）时的四象限基础文案
    codes.STATE_GREED: '贪婪/主升',
    codes.STATE_PANIC: '恐慌抛售',
    codes.STATE_COLD: '缩量阴跌',
    codes.STATE_THAW: '温和回暖',
    codes.STATE_NEUTRAL: '中性震荡',
    codes.STATE_UNAVAILABLE: '不可用（数据不足）',
    # 高位：上证指数收盘 > 60 日均线
    codes.STATE_GREED_HIGH: '高位放量上涨（贪婪/高潮）',
    codes.STATE_PANIC_HIGH: '高位放量急跌（上升趋势的终止预警）',
    codes.STATE_COLD_HIGH: '高位缩量回落（滞涨转弱）',
    codes.STATE_THAW_HIGH: '高位缩量上涨（量能未跟上）',
    # 低位：上证指数收盘 <= 60 日均线
    codes.STATE_GREED_LOW: '低位放量上涨（启动/修复）',
    codes.STATE_PANIC_LOW: '低位放量急跌（恐慌抛售、冰点特征）',
    codes.STATE_COLD_LOW: '低位缩量阴跌（清淡型冰点）',
    codes.STATE_THAW_LOW: '低位缩量回暖（温和修复）',
}

POSITION_LABELS: dict[str, str] = {
    codes.POS_HIGH: '高位',
    codes.POS_LOW: '低位',
    codes.POS_UNKNOWN: '位置未知',
}

BAND_LABELS: dict[str, str] = {
    codes.BAND_HIGH: '高',
    codes.BAND_MID: '中',
    codes.BAND_LOW: '低',
    codes.BAND_UNAVAILABLE: '不可用',
}

PANIC_REASON_LABELS: dict[str, str] = {
    codes.PANIC_TURNOVER_PCT_HIGH: '换手率分位 > 80（放量换手）',
    codes.PANIC_RETURN_20D_LOW: '20 日收益 < -8%',
    codes.PANIC_VOLUME_RATIO_HIGH: '量比 > 1.5',
    codes.PANIC_HIST_NEGATIVE_FALLING: 'MACD 柱为负且较前一日下降',
    codes.PANIC_DOWN_RATIO_HIGH: '下跌家数占比 > 70%',
}

REV_REASON_LABELS: dict[str, str] = {
    codes.REV_PANIC_SCORE: '恐慌抛售得分 > 60',
    codes.REV_HIST_SHRINKING: 'MACD 负柱连续 2 日缩短',
    codes.REV_VOLUME_SHRUNK: '成交量较 20 日峰值萎缩 > 30%',
    codes.REV_DIRECTION_REBOUND: '方向指数较 5 日前回升',
}

REV_BLOCK_REASON_LABELS: dict[str, str] = {
    codes.REV_BLOCK_PANIC_COVERAGE_LOW: '恐慌条件覆盖率 < 0.6，门控未放行',
    codes.REV_BLOCK_PANIC_SCORE_MISSING: '恐慌抛售得分不可用',
    codes.REV_BLOCK_CONDITION_DATA_MISSING: '有观察条件缺少数据',
}

MACD_VOL_STATE_LABELS: dict[str, str] = {
    codes.MACD_VOL_GOLDEN_SURGE: '放量金叉',
    codes.MACD_VOL_GOLDEN_SHRINK: '缩量金叉',
    codes.MACD_VOL_DEAD_SURGE: '放量死叉',
    codes.MACD_VOL_DEAD_SHRINK: '缩量死叉',
    codes.MACD_VOL_GOLDEN_UNKNOWN: '金叉（量比不可用）',
    codes.MACD_VOL_DEAD_UNKNOWN: '死叉（量比不可用）',
    codes.MACD_VOL_DIVERGENCE: '底背离',
    codes.MACD_VOL_NEUTRAL: '中性',
    codes.MACD_VOL_UNAVAILABLE: '不可用（缺少价格或成交量数据）',
}

DATA_QUALITY_LABELS: dict[str, str] = {
    codes.DQ_OK: '正常',
    codes.DQ_PARTIAL: '部分指标不可用',
    codes.DQ_INSUFFICIENT: '样本不足',
}

COLUMN_STATUS_LABELS: dict[str, str] = {
    codes.STATUS_OK: '可用',
    codes.STATUS_MISSING: '缺少该列',
    codes.STATUS_ALL_NAN: '列内无有效值',
    codes.STATUS_INSUFFICIENT_HISTORY: '历史样本不足',
    codes.STATUS_INVALID_VALUES: '存在非法值',
}


def describe_state(code: str) -> str:
    """状态码 -> 中文文案。未知码值直接报错，避免静默显示错口径。"""
    try:
        return STATE_LABELS[code]
    except KeyError as exc:
        raise ValueError(f'未知状态码：{code!r}') from exc


def describe_position(code: str) -> str:
    """价格位置码 -> 中文文案。未知码值直接报错，避免静默显示错口径。"""
    return describe_code(POSITION_LABELS, code)


def describe_code(mapping: Mapping[str, str], code: str) -> str:
    """通用码值 -> 文案映射，未知码值报错。"""
    try:
        return mapping[code]
    except KeyError as exc:
        raise ValueError(f'未知码值：{code!r}') from exc


def label_series(values: pd.Series, mapping: Mapping[str, str]) -> pd.Series:
    """整列码值 -> 文案（缺失值保持缺失）。"""
    labels = values.map(mapping)
    missing = values.notna() & labels.isna()
    if missing.any():
        unknown = sorted({str(v) for v in values[missing].unique()})
        raise ValueError(f'未知码值：{unknown}')
    return labels


def label_history(codes_seq: object, mapping: Mapping[str, str]) -> str:
    """把原因码元组渲染成一行中文，供页面折叠区使用。"""
    items = tuple(codes_seq) if codes_seq is not None else ()
    if not items:
        return '无'
    return '；'.join(mapping.get(str(code), str(code)) for code in items)
