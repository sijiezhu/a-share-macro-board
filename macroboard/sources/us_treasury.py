"""美国10年期国债恒定期限收益率（官方日度 Par Yield Curve）。

来源：U.S. Department of the Treasury, Daily Treasury Par Yield Curve Rates。
字段：`10 Yr`（恒定期限名义收益率，非 TIPS 实际收益率，非票面利率）。
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

import requests

from ..fetch import SourceError, get

CSV_URL = (
    'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/'
    'daily-treasury-rates.csv/{year}/all'
)
COLUMN = '10 Yr'


def parse_csv(text: str, column: str = COLUMN) -> list[tuple[date, float]]:
    """解析财政部年度 CSV，返回 (日期, 收益率%) 升序序列。"""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or column not in reader.fieldnames:
        raise SourceError(f'财政部 CSV 缺少字段 {column!r}')
    out: list[tuple[date, float]] = []
    for row in reader:
        raw_date = (row.get('Date') or '').strip()
        raw_value = (row.get(column) or '').strip()
        if not raw_date or not raw_value or raw_value.upper() in {'N/A', 'NA', '.'}:
            continue
        try:
            day = datetime.strptime(raw_date, '%m/%d/%Y').date()
            out.append((day, float(raw_value)))
        except ValueError:
            continue
    out.sort(key=lambda item: item[0])
    return out


def fetch_year(session: requests.Session, year: int) -> list[tuple[date, float]]:
    url = CSV_URL.format(year=year)
    response = get(
        session,
        url,
        params={
            'type': 'daily_treasury_yield_curve',
            'field_tdr_date_value': str(year),
            'page': '',
            '_format': 'csv',
        },
    )
    return parse_csv(response.text)


def fetch(session: requests.Session, start: date, end: date) -> list[tuple[date, float]]:
    """按年抓取并合并，保留 [start, end] 区间内的观测。"""
    merged: dict[date, float] = {}
    errors: list[str] = []
    for year in range(start.year, end.year + 1):
        try:
            for day, value in fetch_year(session, year):
                if start <= day <= end:
                    merged[day] = value
        except SourceError as exc:
            errors.append(f'{year}: {exc}')
    if not merged:
        raise SourceError('美国财政部未返回任何数据' + (f'（{"; ".join(errors)}）' if errors else ''))
    return sorted(merged.items())
