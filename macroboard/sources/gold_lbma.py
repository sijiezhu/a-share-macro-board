"""XAU/USD 现货黄金日度参考价（LBMA Gold Price PM 定盘价）。

来源：LBMA 官方公开 JSON（https://prices.lbma.org.uk/json/limited/gold_pm.json）。
口径：现货黄金定盘价，报价时点 15:00 伦敦时间（Europe/London），
单位美元/金衡盎司（v[0] 为美元价）。非期货价。
"""

from __future__ import annotations

from datetime import date, datetime

import requests

from ..fetch import SourceError, get

JSON_URL = 'https://prices.lbma.org.uk/json/limited/gold_pm.json'
REFERER = 'https://www.lbma.org.uk/'
ORIGIN = 'https://www.lbma.org.uk'

# 采集参数：LBMA 是本项目唯一走海外链路（Cloudflare）的数据源，晚间国际链路拥塞时
# 会出现瞬时 ConnectTimeout（2026-09-27 20:30 那轮 16/17 成功、只有黄金失败，
# 事后实测 DNS 与 443 建连正常、连续 10 次抓取 1.2—2.3 秒）。
# 因此把建连超时压到 8 秒、尝试次数提到 5 次、退避 2 秒：一次失败的建连只损失 8 秒
# 而不是 30 秒，重试预算用在"多试几次"而不是"死等一次"上。
# 最坏耗时：连接型失败 ≈ 5×8 + (2+4+6+8) = 60 秒（改动前 ≈ 3×30 + 3 ≈ 93 秒）；
# 读取型失败 ≈ 5×20 + 20 = 120 秒，说明对端确实不可用，交给本轮记 failed、下次采集自愈。
# 口径不变：仍然只取 LBMA Gold Price PM 定盘价，未改用期货价或其它源。
REQUEST_ATTEMPTS = 5
REQUEST_BACKOFF = 2.0
REQUEST_TIMEOUT = (8.0, 20.0)


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
    response = get(
        session,
        JSON_URL,
        headers={'Origin': ORIGIN, 'Referer': REFERER},
        attempts=REQUEST_ATTEMPTS,
        backoff=REQUEST_BACKOFF,
        timeout=REQUEST_TIMEOUT,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError(f'LBMA 返回非 JSON: {exc}') from exc
    rows = [item for item in parse_payload(payload) if start <= item[0] <= end]
    if not rows:
        raise SourceError('LBMA 未返回区间内数据')
    return rows
