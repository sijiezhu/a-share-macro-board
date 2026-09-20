"""东方财富妙想数据客户端：A股情绪参考与沪深300估值。

固定查询语句、固定指标标签；解析时优先使用 rawTable 的原始数值，避免把
"倍""点""家"等展示单位带入计算。查询语句变更必须同步 docs/DATA_SOURCES.md。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import requests

from ..fetch import SourceError, post_json

API_URL = 'https://mkapi2.dfcfs.com/finskillshub/api/claw/query'

# 妙想对"结束日期落在当年中间"的区间会返回 code=112；自然年区间与"至今"稳定可用，
# 因此年度回补固定使用自然年，落地后再按目标区间过滤。
QUERY_HS300_PE_YEAR = '沪深300指数市盈率PE-TTM {year}年1月1日至{year}年12月31日每个交易日'
QUERY_HS300_CLOSE_YEAR = '沪深300指数收盘价 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_ADV_YEAR = '沪深A股上涨家数 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_DEC_YEAR = '沪深A股下跌家数 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_FLAT_YEAR = '沪深A股平盘家数 {year}年1月1日至{year}年12月31日每个交易日'

# A股情绪算法的输入字段（macroboard.sentiment）；查询语句变更必须同步 docs/DATA_SOURCES.md。
QUERY_AMOUNT_YEAR = '沪深两市A股成交额 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_VOLUME_YEAR = '沪深两市A股成交量 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_TURNOVER_YEAR = '沪深两市A股换手率 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_LIMIT_UP_YEAR = '沪深A股涨停家数 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_LIMIT_DOWN_YEAR = '沪深A股跌停家数 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_MARGIN_BUY_YEAR = '沪深两市融资买入额 {year}年1月1日至{year}年12月31日每个交易日'
QUERY_MARGIN_REPAY_YEAR = '沪深两市融资偿还额 {year}年1月1日至{year}年12月31日每个交易日'

_NUMBER_RE = re.compile(r'-?\d+(?:\.\d+)?')
_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')

# 妙想接口在短时间连续查询时会返回 code=112，稍等后即恢复（实测 30~60 秒内自愈）。
# 因此对可重试错误码做有限退避重试，并在两次查询之间保持最小间隔。
RETRYABLE_CODES = {112, 113}
RETRY_DELAYS = (5.0, 20.0)
MIN_QUERY_INTERVAL = 1.5
_last_query_at = 0.0


class MXNotConfigured(SourceError):
    """缺少 MX_APIKEY，指标未接通。"""


@dataclass
class FetchedSeries:
    """分段抓取结果：rows 为已合并的有效观测，warnings 记录失败分段的说明。"""

    rows: list[tuple[date, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def api_key_from_env() -> str | None:
    import os

    key = (os.environ.get('MX_APIKEY') or '').strip()
    return key or None


def query(session: requests.Session, api_key: str, text: str) -> dict:
    if not api_key:
        raise MXNotConfigured('缺少 MX_APIKEY')
    global _last_query_at
    last_code: object = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        elapsed = time.monotonic() - _last_query_at
        if elapsed < MIN_QUERY_INTERVAL:
            time.sleep(MIN_QUERY_INTERVAL - elapsed)
        payload = post_json(
            session,
            API_URL,
            {'toolQuery': text},
            headers={'Content-Type': 'application/json', 'apikey': api_key},
            timeout=60,
        )
        _last_query_at = time.monotonic()
        code = payload.get('code')
        if code in (0, None) or payload.get('success'):
            return payload
        last_code = code
        if code in RETRYABLE_CODES and attempt < len(RETRY_DELAYS):
            time.sleep(RETRY_DELAYS[attempt])
            continue
        break
    raise SourceError(f'妙想接口返回错误码 {last_code}')


def parse_number(raw: object) -> float | None:
    """把 '14.23倍' / '4,507.39点' / '3,937' 解析为浮点数。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(',', '')
    if not text:
        return None
    factor = 1.0
    if '万亿' in text:
        factor = 1e12
    elif '亿' in text:
        factor = 1e8
    elif '万' in text:
        factor = 1e4
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0)) * factor
    except ValueError:
        return None


def parse_date(raw: object) -> date | None:
    match = _DATE_RE.search(str(raw or ''))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def extract_series(payload: dict) -> list[tuple[date, float]]:
    """从妙想返回中提取 (日期, 数值) 序列，要求恰好一个指标列。"""
    inner = ((payload.get('data') or {}).get('data') or {})
    result = inner.get('searchDataResultDTO') or {}
    blocks = result.get('dataTableDTOList') or []
    if not blocks:
        raise SourceError('妙想返回中没有数据表')
    best: list[tuple[date, float]] = []
    for block in blocks:
        table = block.get('rawTable') or block.get('table') or {}
        if not isinstance(table, dict):
            continue
        heads = table.get('headName') or []
        keys = [key for key in table if key != 'headName']
        if len(keys) != 1 or not heads:
            continue
        values = table[keys[0]] or []
        rows: list[tuple[date, float]] = []
        for head, raw in zip(heads, values, strict=False):
            day = parse_date(head)
            value = parse_number(raw)
            if day is not None and value is not None:
                rows.append((day, value))
        if len(rows) > len(best):
            best = rows
    if not best:
        raise SourceError('妙想返回中没有可解析的单指标序列')
    best.sort(key=lambda item: item[0])
    return best


def fetch_series(session: requests.Session, api_key: str, text: str) -> list[tuple[date, float]]:
    return extract_series(query(session, api_key, text))


def fetch_a_share_breadth(
    session: requests.Session, api_key: str, start: date, end: date
) -> dict[str, FetchedSeries]:
    """沪深A股上涨/下跌/平盘家数。分母含平盘，不需要额外剔除停牌。"""
    return {
        'adv_count': fetch_year_series(session, api_key, QUERY_ADV_YEAR, start, end),
        'dec_count': fetch_year_series(session, api_key, QUERY_DEC_YEAR, start, end),
        'flat_count': fetch_year_series(session, api_key, QUERY_FLAT_YEAR, start, end),
    }


def fetch_hs300_close(
    session: requests.Session, api_key: str, start: date, end: date
) -> FetchedSeries:
    """按自然年分段抓取沪深300收盘价。"""
    return fetch_year_series(session, api_key, QUERY_HS300_CLOSE_YEAR, start, end)


def fetch_year_series(
    session: requests.Session,
    api_key: str,
    year_template: str,
    start: date,
    end: date,
) -> FetchedSeries:
    """按自然年分段抓取，容许个别年份失败但要求最新年份必须有数据。"""
    merged: dict[date, float] = {}
    warnings: list[str] = []
    end_year_ok = False
    for year in range(start.year, end.year + 1):
        chunk_start = max(start, date(year, 1, 1))
        chunk_end = min(end, date(year, 12, 31))
        text = year_template.format(year=year)
        try:
            rows = fetch_series(session, api_key, text)
        except SourceError as exc:
            warnings.append(f'{year}年抓取失败（{exc}）')
            continue
        kept = 0
        for day, value in rows:
            if chunk_start <= day <= chunk_end:
                merged[day] = value
                kept += 1
        if year == end.year:
            end_year_ok = kept > 0
            if not end_year_ok:
                warnings.append(f'{year}年（最新年份）未返回数据')
    if not merged:
        raise SourceError('妙想未返回数据' + (f'（{"; ".join(warnings)}）' if warnings else ''))
    if not end_year_ok:
        raise SourceError('妙想未返回最新年份数据：' + '; '.join(warnings))
    return FetchedSeries(rows=sorted(merged.items()), warnings=warnings)


def fetch_hs300_pe(
    session: requests.Session, api_key: str, start: date, end: date
) -> FetchedSeries:
    """按自然年分段查询沪深300 PE-TTM，避免单次返回过大或触发区间限制。"""
    return fetch_year_series(session, api_key, QUERY_HS300_PE_YEAR, start, end)


def parse_pe_value(raw: object) -> float | None:
    value = parse_number(raw)
    if value is None or value <= 0:
        return None
    return value


def build_margin_net_buy(
    buy_rows: Sequence[tuple[date, float]],
    repay_rows: Sequence[tuple[date, float]],
) -> list[tuple[date, float]]:
    """融资净买入 = 同日融资买入额 − 同日融资偿还额。

    只保留两侧都有观测的日期：不做前向填充、不做插值（与股债利差同口径），
    因此缺单侧的交易日会被整条丢弃，而不是用另一侧顶替。
    """
    repay = {day: float(value) for day, value in repay_rows}
    out: list[tuple[date, float]] = []
    for day, buy in buy_rows:
        other = repay.get(day)
        if other is None:
            continue
        out.append((day, float(buy) - other))
    out.sort(key=lambda item: item[0])
    return out


def fetch_margin_net_buy(
    session: requests.Session,
    api_key: str,
    start: date,
    end: date,
) -> FetchedSeries:
    """两融净买入：分别抓融资买入额与融资偿还额后按同日相减。"""
    buy = fetch_year_series(session, api_key, QUERY_MARGIN_BUY_YEAR, start, end)
    repay = fetch_year_series(session, api_key, QUERY_MARGIN_REPAY_YEAR, start, end)
    rows = build_margin_net_buy(buy.rows, repay.rows)
    warnings = [*buy.warnings, *repay.warnings]
    if not rows:
        raise SourceError('融资买入额与融资偿还额没有共同数据日期')
    dropped = len(buy.rows) - len(rows)
    if dropped > 0:
        warnings.append(f'{dropped} 个交易日只有单侧两融数据，已整条跳过（不做前向填充）')
    return FetchedSeries(rows=rows, warnings=warnings)


def fetch_a_share_activity(
    session: requests.Session,
    api_key: str,
    start: date,
    end: date,
) -> dict[str, FetchedSeries]:
    """A股情绪算法的原始输入：成交额/成交量/换手率/涨跌停家数/融资净买入。

    单个指标失败只影响它自己（返回空序列并带告警，编排层据此标为 missing），
    全部指标都失败才抛 SourceError。
    """
    plan = (
        ('amount', QUERY_AMOUNT_YEAR),
        ('volume', QUERY_VOLUME_YEAR),
        ('turnover_rate', QUERY_TURNOVER_YEAR),
        ('limit_up_count', QUERY_LIMIT_UP_YEAR),
        ('limit_down_count', QUERY_LIMIT_DOWN_YEAR),
    )
    out: dict[str, FetchedSeries] = {}
    for metric, template in plan:
        try:
            out[metric] = fetch_year_series(session, api_key, template, start, end)
        except SourceError as exc:
            out[metric] = FetchedSeries(rows=[], warnings=[f'{metric} 抓取失败：{exc}'])
    try:
        out['margin_net_buy'] = fetch_margin_net_buy(session, api_key, start, end)
    except SourceError as exc:
        out['margin_net_buy'] = FetchedSeries(rows=[], warnings=[f'margin_net_buy 抓取失败：{exc}'])
    if not any(item.rows for item in out.values()):
        details = '; '.join(warning for item in out.values() for warning in item.warnings)
        raise SourceError(f'A股情绪输入全部失败：{details}')
    return out
