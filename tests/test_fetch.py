"""HTTP 辅助层：重试、超时口径与退避抖动（不联网，全部用假 session）。"""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

import requests

from macroboard import fetch
from macroboard.fetch import SourceError, get, request


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}')


class FakeSession:
    """按脚本返回响应或抛异常，并记录每次调用的参数。"""

    def __init__(self, outcomes: list[object]):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({'method': method, 'url': url, **kwargs})
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


class RequestRetryTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        session = FakeSession(
            [requests.ConnectTimeout('连接超时'), requests.ConnectTimeout('连接超时'), FakeResponse(200)]
        )
        with mock.patch.object(fetch.time, 'sleep'):
            response = request(session, 'GET', 'https://example.invalid/x', attempts=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.calls), 3)

    def test_raises_source_error_after_all_attempts(self):
        session = FakeSession([requests.ConnectTimeout('连接超时')] * 4)
        with mock.patch.object(fetch.time, 'sleep'):
            with self.assertRaises(SourceError) as ctx:
                request(session, 'GET', 'https://example.invalid/x', attempts=4)
        self.assertIn('ConnectTimeout', str(ctx.exception))
        self.assertEqual(len(session.calls), 4)
        self.assertNotIn('connect_timeout', str(ctx.exception))  # 消息里不带凭证/URL 细节以外的内容

    def test_default_timeout_splits_connect_and_read(self):
        """建连与读取分开限时：海外源晚间建连被黑洞时，不该把预算耗在一次连接等待上。"""
        session = FakeSession([FakeResponse(200)])
        with mock.patch.object(fetch.time, 'sleep'):
            request(session, 'GET', 'https://example.invalid/x')
        self.assertEqual(session.calls[0]['timeout'], (10.0, 30.0))

    def test_explicit_timeout_is_passed_through(self):
        session = FakeSession([FakeResponse(200)])
        with mock.patch.object(fetch.time, 'sleep'):
            request(session, 'GET', 'https://example.invalid/x', timeout=(3.0, 8.0))
        self.assertEqual(session.calls[0]['timeout'], (3.0, 8.0))
        scalar = FakeSession([FakeResponse(200)])
        with mock.patch.object(fetch.time, 'sleep'):
            get(scalar, 'https://example.invalid/x', timeout=60.0)
        self.assertEqual(scalar.calls[0]['timeout'], 60.0)

    def test_backoff_grows_with_jitter(self):
        session = FakeSession([requests.ConnectTimeout('x')] * 3)
        with (
            mock.patch.object(fetch.time, 'sleep') as sleep,
            mock.patch.object(fetch.random, 'uniform', return_value=0.25),
        ):
            with self.assertRaises(SourceError):
                request(session, 'GET', 'https://example.invalid/x', attempts=3, backoff=2.0, jitter=0.5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.25, 4.25])

    def test_jitter_can_be_disabled_for_deterministic_waits(self):
        session = FakeSession([requests.ConnectTimeout('x')] * 2)
        with mock.patch.object(fetch.time, 'sleep') as sleep:
            with self.assertRaises(SourceError):
                request(session, 'GET', 'https://example.invalid/x', attempts=2, backoff=2.0, jitter=0.0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2.0])

    def test_status_handling_is_unchanged(self):
        """5xx 重试、4xx 也重试（与改动前一致）；本次只动超时与退避，不动状态码策略。"""
        server = FakeSession([FakeResponse(503), FakeResponse(200)])
        with mock.patch.object(fetch.time, 'sleep'):
            self.assertEqual(request(server, 'GET', 'https://example.invalid/x').status_code, 200)
        self.assertEqual(len(server.calls), 2)
        client = FakeSession([FakeResponse(404)] * 3)
        with mock.patch.object(fetch.time, 'sleep'):
            with self.assertRaises(SourceError) as ctx:
                request(client, 'GET', 'https://example.invalid/x', attempts=3)
        self.assertIn('404', str(ctx.exception))
        self.assertEqual(len(client.calls), 3)


class GoldSourceRetryBudgetTests(unittest.TestCase):
    def test_gold_uses_a_short_connect_timeout_and_more_attempts(self):
        """LBMA 是本项目唯一走海外链路的源：建连超时要短、尝试次数要多，口径不变。"""
        from macroboard.sources import gold_lbma

        with mock.patch.object(gold_lbma, 'get', return_value=FakeResponse(200)) as fake_get:
            fake_get.return_value.json = lambda: []  # type: ignore[attr-defined]
            with self.assertRaises(SourceError):  # 空 payload -> 未解析出记录
                gold_lbma.fetch(mock.Mock(), date(2026, 9, 1), date(2026, 9, 27))
        kwargs = fake_get.call_args.kwargs
        self.assertEqual(kwargs['attempts'], gold_lbma.REQUEST_ATTEMPTS)
        self.assertEqual(kwargs['timeout'], gold_lbma.REQUEST_TIMEOUT)
        self.assertLess(kwargs['timeout'][0], 10.0)
        self.assertGreaterEqual(kwargs['attempts'], 5)
        self.assertEqual(
            kwargs['headers'],
            {'Origin': gold_lbma.ORIGIN, 'Referer': gold_lbma.REFERER},
        )


if __name__ == '__main__':
    unittest.main()
