"""中债10年期国债到期收益率（东方财富数据中心镜像）。

来源：东方财富数据中心报表 RPTA_WEB_TREASURYYIELD，字段 EMM00166466。
口径：中债国债收益率曲线 10 年期到期收益率（%）。

中债官网（yield.chinabond.com.cn）直连接口当前返回空结果，因此使用固定镜像，
并在 docs/DATA_SOURCES.md 中记录验证结果。禁止在未更新文档的情况下更换来源。
"""

from __future__ import annotations

from datetime import date, datetime

import requests

from ..fetch import SourceError, get

API_URL = 'https://datacenter.eastmoney.com/api/data/get'
REPORT_NAME = 'RPTA_WEB_TREASURYYIELD'
FIELD = 'EMM00166466'  # 中国10年期国债到期收益率
PAGE_SIZE = 500


def parse_payload(payload: dict, field: str = FIELD) -> list[tuple[date, float]]:
    result = payload.get('result') or {}
    rows = result.get('data') or []
    out: list[tuple[date, float]] = []
    for row in rows:
        raw_date = str(row.get('SOLAR_DATE') or '')[:10]
        value = row.get(field)
        if not raw_date or value in (None, ''):
            continue
        try:
            out.append((datetime.strptime(raw_date, '%Y-%m-%d').date(), float(value)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda item: item[0])
    return out


def fetch(session: requests.Session, start: date, end: date) -> list[tuple[date, float]]:
    """抓取 [start, end] 区间内的中债10年期收益率，按页拉取。"""
    merged: dict[date, float] = {}
    page = 1
    pages = 1
    while page <= pages:
        response = get(
            session,
            API_URL,
            params={
                'type': REPORT_NAME,
                'sty': 'ALL',
                'st': 'SOLAR_DATE',
                'sr': '-1',
                'p': str(page),
                'ps': str(PAGE_SIZE),
                'filter': f"(SOLAR_DATE>='{start.isoformat()}')",
            },
            headers={'Referer': 'https://data.eastmoney.com/'},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(f'中债镜像返回非 JSON: {exc}') from exc
        if not payload.get('success', True) and not payload.get('result'):
            raise SourceError(f'中债镜像返回错误: {payload.get("message")}')
        result = payload.get('result') or {}
        pages = int(result.get('pages') or 1)
        rows = parse_payload(payload)
        if not rows and page == 1:
            raise SourceError('中债镜像未返回数据')
        for day, value in rows:
            if start <= day <= end:
                merged[day] = value
        page += 1
    if not merged:
        raise SourceError('中债镜像未返回区间内数据')
    return sorted(merged.items())
