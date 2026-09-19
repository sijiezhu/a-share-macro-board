"""采集编排：抓取 → 落库 → 记录状态与错误，失败保留上次成功值。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import requests
from dateutil.relativedelta import relativedelta

from . import db
from .config import (
    CHART_BACKFILL_YEARS,
    DEFAULT_DB_PATH,
    SERIES,
    SPREAD_BACKFILL_YEARS,
    SeriesSpec,
)
from .fetch import SourceError, new_session
from .sources import cn_bond, fx_usdcny, gold_lbma, mx, us_treasury

BEIJING = ZoneInfo('Asia/Shanghai')


@dataclass
class MetricResult:
    metric: str
    ok: bool
    status: str
    rows: int
    message: str = ''

    @property
    def label(self) -> str:
        spec = SERIES.get(self.metric)
        return spec.label if spec else self.metric


@dataclass
class JobOutcome:
    """一次抓取的结果：各指标观测值 + 需要记录的告警（如个别分段失败）。"""

    series: dict[str, list[tuple[date, float]]] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)


@dataclass
class Job:
    name: str
    metrics: tuple[str, ...]
    fetch: Callable[[], JobOutcome]
    requires_mx: bool = False


def beijing_today() -> date:
    from datetime import datetime

    return datetime.now(BEIJING).date()


def _window(today: date, days: int) -> date:
    return today - timedelta(days=days)


def _backfill_start(today: date, years: int) -> date:
    return today - relativedelta(years=years)


def build_jobs(
    session: requests.Session,
    *,
    today: date,
    full: bool,
    api_key: str | None,
) -> list[Job]:
    """构造采集任务。full=True 时补齐股债利差所需的长历史。"""
    end = today

    def us10y() -> JobOutcome:
        start = _backfill_start(end, CHART_BACKFILL_YEARS) if full else _window(end, 400)
        return JobOutcome({'us10y': us_treasury.fetch(session, start, end)})

    def cn10y() -> JobOutcome:
        start = _backfill_start(end, SPREAD_BACKFILL_YEARS) if full else _window(end, 400)
        return JobOutcome({'cn10y': cn_bond.fetch(session, start, end)})

    def usdcny() -> JobOutcome:
        start = _backfill_start(end, CHART_BACKFILL_YEARS) if full else _window(end, 400)
        return JobOutcome({'usdcny': fx_usdcny.fetch(session, start, end)})

    def xauusd() -> JobOutcome:
        start = _backfill_start(end, CHART_BACKFILL_YEARS) if full else _window(end, 400)
        return JobOutcome({'xauusd': gold_lbma.fetch(session, start, end)})

    def breadth() -> JobOutcome:
        start = _backfill_start(end, CHART_BACKFILL_YEARS) if full else _window(end, 400)
        fetched = mx.fetch_a_share_breadth(session, api_key or '', start, end)
        return JobOutcome(
            {metric: item.rows for metric, item in fetched.items()},
            {metric: '; '.join(item.warnings) for metric, item in fetched.items() if item.warnings},
        )

    def hs300_close() -> JobOutcome:
        start = _backfill_start(end, CHART_BACKFILL_YEARS) if full else _window(end, 400)
        fetched = mx.fetch_hs300_close(session, api_key or '', start, end)
        warnings = {'hs300_close': '; '.join(fetched.warnings)} if fetched.warnings else {}
        return JobOutcome({'hs300_close': fetched.rows}, warnings)

    def hs300_pe() -> JobOutcome:
        start = _backfill_start(end, SPREAD_BACKFILL_YEARS) if full else _window(end, 400)
        fetched = mx.fetch_hs300_pe(session, api_key or '', start, end)
        warnings = {'hs300_pe_ttm': '; '.join(fetched.warnings)} if fetched.warnings else {}
        return JobOutcome({'hs300_pe_ttm': fetched.rows}, warnings)

    return [
        Job('美国国债', ('us10y',), us10y),
        Job('中债收益率', ('cn10y',), cn10y),
        Job('美元兑人民币', ('usdcny',), usdcny),
        Job('黄金', ('xauusd',), xauusd),
        Job('A股涨跌家数', ('adv_count', 'dec_count', 'flat_count'), breadth, requires_mx=True),
        Job('沪深300指数', ('hs300_close',), hs300_close, requires_mx=True),
        Job('沪深300估值', ('hs300_pe_ttm',), hs300_pe, requires_mx=True),
    ]


def evaluate_status(spec: SeriesSpec, latest_date: date, today: date) -> str:
    """按休市容忍窗口判断 正常 / 休市沿用。"""
    if (today - latest_date).days <= spec.max_lag_days:
        return 'normal'
    return 'stale'


def _record_success(
    conn,
    metric: str,
    rows: Sequence[tuple[date, float]],
    *,
    fetched_at: str,
    today: date,
    note: str | None = None,
) -> MetricResult:
    spec = SERIES[metric]
    db.upsert_observations(conn, metric, rows, spec.source, fetched_at)
    latest = db.latest_observation(conn, metric)
    if latest is None:
        db.set_status(conn, metric, 'missing', checked_at=fetched_at, source=spec.source)
        return MetricResult(metric, False, 'missing', 0, '采集成功但库中无有效观测')
    status = evaluate_status(spec, date.fromisoformat(latest['obs_date']), today)
    db.set_status(
        conn,
        metric,
        status,
        obs_date=latest['obs_date'],
        value=latest['value'],
        source=spec.source,
        note=note,
        checked_at=fetched_at,
    )
    return MetricResult(metric, True, status, len(rows), note or '')


def _record_failure(
    conn,
    metric: str,
    *,
    status: str,
    message: str,
    fetched_at: str,
) -> MetricResult:
    """失败时保留上次成功值及其原数据日期，只更新状态与错误日志。"""
    spec = SERIES[metric]
    previous = db.latest_observation(conn, metric)
    db.set_status(
        conn,
        metric,
        status,
        obs_date=previous['obs_date'] if previous else None,
        value=previous['value'] if previous else None,
        source=(previous['source'] if previous else None) or spec.source,
        note=message[:200],
        checked_at=fetched_at,
    )
    db.log_error(conn, metric, message, at=fetched_at)
    return MetricResult(metric, False, status, 0, message)


def collect(
    db_path=DEFAULT_DB_PATH,
    *,
    full: bool = False,
    only: Iterable[str] | None = None,
    today: date | None = None,
    session: requests.Session | None = None,
    api_key: str | None = None,
) -> list[MetricResult]:
    """执行一次采集。返回每个指标的结果，失败不抛出。"""
    conn = db.connect(db_path)
    db.init_db(conn)
    session = session or new_session()
    today = today or beijing_today()
    api_key = api_key if api_key is not None else mx.api_key_from_env()
    fetched_at = db.utcnow()
    run_id = db.start_run(conn, fetched_at)
    only_set = set(only) if only else None
    results: list[MetricResult] = []

    for job in build_jobs(session, today=today, full=full, api_key=api_key):
        metrics = tuple(m for m in job.metrics if only_set is None or m in only_set)
        if not metrics:
            continue
        if job.requires_mx and not api_key:
            for metric in metrics:
                results.append(
                    _record_failure(
                        conn,
                        metric,
                        status='unconfigured',
                        message='缺少 MX_APIKEY，指标未接通',
                        fetched_at=fetched_at,
                    )
                )
            continue
        try:
            outcome = job.fetch()
        except mx.MXNotConfigured as exc:
            for metric in metrics:
                results.append(
                    _record_failure(
                        conn, metric, status='unconfigured', message=str(exc), fetched_at=fetched_at
                    )
                )
            continue
        except (SourceError, requests.RequestException, ValueError) as exc:
            for metric in metrics:
                results.append(
                    _record_failure(
                        conn,
                        metric,
                        status='failed',
                        message=f'{type(exc).__name__}: {exc}',
                        fetched_at=fetched_at,
                    )
                )
            continue
        except Exception as exc:  # noqa: BLE001 - 采集失败不应中断整轮
            for metric in metrics:
                results.append(
                    _record_failure(
                        conn,
                        metric,
                        status='failed',
                        message=f'{type(exc).__name__}: {exc}',
                        fetched_at=fetched_at,
                    )
                )
            continue
        for metric in metrics:
            rows = outcome.series.get(metric) or []
            if not rows:
                results.append(
                    _record_failure(
                        conn,
                        metric,
                        status='missing',
                        message='数据源未返回该指标数据',
                        fetched_at=fetched_at,
                    )
                )
                continue
            results.append(
                _record_success(
                    conn,
                    metric,
                    rows,
                    fetched_at=fetched_at,
                    today=today,
                    note=outcome.warnings.get(metric),
                )
            )

    ok_count = sum(1 for item in results if item.ok)
    db.finish_run(conn, run_id, ok_count, len(results) - ok_count)
    conn.close()
    return results
