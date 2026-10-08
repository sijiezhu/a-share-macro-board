"""美元兑人民币在岸即期汇率（新浪财经 fx_susdcny 日线）。

口径：在岸人民币（CNY）即期，单位人民币/美元。取日线收盘价，数值上升表示
人民币相对美元贬值。不使用中间价（USDCNYC），不使用离岸 USD/CNH。

新浪日线字段顺序为：日期, 开盘, 最低, 最高, 收盘（经样本校验，取最后一个字段）。
"""

from __future__ import annotations

from datetime import date, datetime

import requests

from ..fetch import SourceError, get

JSONP_URL = 'https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20_d=/NewForexService.getDayKLine'
SYMBOL = 'fx_susdcny'
REFERER = 'https://finance.sina.com.cn/money/forex/hq/USDCNY.shtml'


def parse_daykline(text: str) -> list[tuple[date, float]]:
    start = text.find('("')
    if start == -1:
        raise SourceError('新浪日线响应格式异常')
    body = text[start + 2 :].rstrip('");\r\n\t ').lstrip('"\r\n\t ')
    out: list[tuple[date, float]] = []
    for chunk in body.split('|'):
        fields = chunk.split(',')
        if len(fields) < 5:
            continue
        try:
            day = datetime.strptime(fields[0].strip('"\r\n\t '), '%Y-%m-%d').date()
            close = float(fields[4].strip('"\r\n\t '))
        except ValueError:
            continue
        out.append((day, close))
    if not out:
        raise SourceError('新浪日线未解析出任何记录')
    out.sort(key=lambda item: item[0])
    return out


def fetch(session: requests.Session, start: date, end: date) -> list[tuple[date, float]]:
    response = get(
        session,
        JSONP_URL,
        params={'symbol': SYMBOL},
        headers={'Referer': REFERER},
    )
    rows = [item for item in parse_daykline(response.text) if start <= item[0] <= end]
    if not rows:
        raise SourceError('新浪未返回区间内的在岸人民币数据')
    return rows
