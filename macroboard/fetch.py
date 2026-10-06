"""带有限重试的 HTTP 辅助函数：失败不返回空值，只抛 SourceError。"""

from __future__ import annotations

import random
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
    jitter: float = 0.5,
    timeout: float | tuple[float, float] = (10.0, 30.0),
    **kwargs,
) -> requests.Response:
    """执行请求，失败重试 attempts 次后抛出 SourceError。

    超时口径（2026-09-27 改）
    - `timeout` 支持 requests 的 `(connect, read)` 二元组：建连与读取分开限时。
      默认 `(10.0, 30.0)`：国内站点 10 秒足够建连，读取预算与原先的 30 秒一致。
      海外站点（现货黄金走 LBMA 的 Cloudflare）在晚间链路拥塞时会出现瞬时 ConnectTimeout，
      单次 30 秒的建连等待会把整个重试预算耗在一次连接上 —— 收缩建连超时才有机会重试成功
      （2026-09-27 20:30 那轮 16/17 成功、仅黄金失败，事后实测建连只要 0.2 秒）。
    - `jitter`：退避加 0—jitter 秒均匀抖动，避免重试节奏与源站限流窗口对齐
      （妙想的 code=112 就是"连续快速查询"触发的）。抖动只影响等待时长，不影响任何输出数值；
      测试里传 `jitter=0` 可得到确定的等待序列。
    """
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
                # 退避抖动只用于错开重试节奏，不是安全用途，不需要密码学随机数（S311 / B311）
                wait = backoff * attempt + random.uniform(0.0, jitter)  # noqa: S311  # nosec B311
                time.sleep(wait)
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

