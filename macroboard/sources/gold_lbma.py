"""XAU/USD 现货黄金日度参考价（LBMA Gold Price PM 定盘价）。

来源：LBMA 官方公开 JSON（https://prices.lbma.org.uk/json/gold_pm.json）。
口径：现货黄金定盘价，报价时点 15:00 伦敦时间（Europe/London），
单位美元/金衡盎司（v[0] 为美元价）。非期货价。
"""

from __future__ import annotations

from datetime import date, datetime

import requests

from ..fetch import SourceError, get

JSON_URL = 'https://prices.lbma.org.uk/json/gold_pm.json'


def parse_payload(payload: object) -> list[tuple[date, float]]:
    if not isinstance(payload, list):
        raise SourceError('LBMA 响应结构异常')
    out: list[tuple[date, float]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get('d') or '')
        values = row.get('v')
        if not raw_date or not isinstance(values, list) or not values:
            continue
        price = values[0]
        if price in (None, ''):
            continue
        try:
            out.append((datetime.strptime(raw_date[:10], '%Y-%m-%d').date(), float(price)))
        except (ValueError, TypeError):
            continue
    if not out:
        raise SourceError('LBMA 未解析出任何记录')
    out.sort(key=lambda item: item[0])
    return out


def fetch(session: requests.Session, start: date, end: date) -> list[tuple[date, float]]:
    response = get(session, JSON_URL)
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError(f'LBMA 返回非 JSON: {exc}') from exc
    rows = [item for item in parse_payload(payload) if start <= item[0] <= end]
    if not rows:
        raise SourceError('LBMA 未返回区间内数据')
    return rows
