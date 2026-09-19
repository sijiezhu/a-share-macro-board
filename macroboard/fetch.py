"""带有限重试的 HTTP 辅助函数：失败不返回空值，只抛 SourceError。"""

from __future__ import annotations

import time

import requests

USER_AGENT = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0 Safari/537.36'
)


class SourceError(RuntimeError):
    """数据源请求失败（已重试）。消息中不含任何凭证。"""


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'})
    return session


def request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 1.5,
    timeout: float = 30.0,
    **kwargs,
) -> requests.Response:
    """执行请求，失败重试 attempts 次后抛出 SourceError。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code >= 500:
                raise SourceError(f'HTTP {response.status_code}')
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001 - 统一转为 SourceError
            last_error = exc
            if attempt < attempts:
                time.sleep(backoff * attempt)
    raise SourceError(f'{type(last_error).__name__}: {last_error}')


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    return request(session, 'GET', url, **kwargs)


def post_json(
    session: requests.Session,
    url: str,
    payload: dict,
    *,
    headers: dict | None = None,
    **kwargs,
) -> dict:
    response = request(session, 'POST', url, json=payload, headers=headers, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError(f'响应不是合法 JSON: {exc}') from exc

