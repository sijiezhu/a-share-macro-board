"""采集幂等性、失败保留旧值与状态判定测试（不触网）。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from dateutil.relativedelta import relativedelta

from macroboard import collect as collect_module
from macroboard import db
from macroboard.config import LOOKBACK_DAYS, SERIES
from macroboard.sources import mx

FRIDAY = date(2026, 9, 18)


def _mx_payload(rows: list[tuple[str, float]]) -> dict:
    """妙想返回的最小 payload（单指标列，rawTable 形态与线上一致）。"""
    return {
        'data': {
            'data': {
                'searchDataResultDTO': {
                    'dataTableDTOList': [
                        {
                            'rawTable': {
                                '325898': [str(value) for _day, value in rows],
                                'headName': [f'{day}(日)' for day, _value in rows],
                            }
                        }
                    ]
                }
            }
        }
    }


def _job(metrics, payload_or_error):
    def fetch():
        if isinstance(payload_or_error, Exception):
            raise payload_or_error
        return collect_module.JobOutcome(payload_or_error)

    return collect_module.Job('test', tuple(metrics), fetch)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'test.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, jobs, *, today=FRIDAY, full=False):
        with mock.patch.object(collect_module, 'build_jobs', return_value=jobs):
            return collect_module.collect(self.db_path, today=today, full=full)

    def test_repeated_collection_does_not_duplicate_rows(self):
        jobs = [_job(['us10y'], {'us10y': [(date(2026, 9, 17), 4.94), (date(2026, 9, 18), 5.0)]})]
        self._run(jobs)
        self._run(jobs)
        conn = db.connect(self.db_path)
        self.assertEqual(db.count_observations(conn, 'us10y'), 2)
        conn.close()

    def test_updated_value_overwrites_same_date(self):
        self._run([_job(['us10y'], {'us10y': [(date(2026, 9, 18), 5.0)]})])
        self._run([_job(['us10y'], {'us10y': [(date(2026, 9, 18), 5.01)]})])
        conn = db.connect(self.db_path)
        latest = db.latest_observation(conn, 'us10y')
        self.assertEqual(latest['value'], 5.01)
        self.assertEqual(db.count_observations(conn, 'us10y'), 1)
        conn.close()

    def test_failure_keeps_previous_value_and_marks_error(self):
        self._run([_job(['xauusd'], {'xauusd': [(date(2026, 9, 17), 4368.1)]})])
        results = self._run([_job(['xauusd'], collect_module.SourceError('HTTP 503'))])
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].status, 'failed')

        conn = db.connect(self.db_path)
        status = db.get_statuses(conn)['xauusd']
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['obs_date'], '2026-09-17')
        self.assertAlmostEqual(status['value'], 4368.1)
        latest = db.latest_observation(conn, 'xauusd')
        self.assertEqual(latest['obs_date'], '2026-09-17')
        errors = db.recent_errors(conn)
        self.assertEqual(len(errors), 1)
        self.assertIn('HTTP 503', errors[0]['message'])
        conn.close()

    def test_missing_mx_key_marks_unconfigured(self):
        job = collect_module.Job(
            '情绪', ('adv_count',), lambda: collect_module.JobOutcome(), requires_mx=True
        )
        with (
            mock.patch.object(collect_module, 'build_jobs', return_value=[job]),
            mock.patch.object(collect_module.mx, 'api_key_from_env', return_value=None),
        ):
            results = collect_module.collect(self.db_path, today=FRIDAY)
        self.assertEqual(results[0].status, 'unconfigured')
        conn = db.connect(self.db_path)
        self.assertEqual(db.get_statuses(conn)['adv_count']['status'], 'unconfigured')
        conn.close()

    def test_run_is_recorded_with_counts(self):
        self._run([_job(['us10y'], {'us10y': [(date(2026, 9, 18), 5.0)]})])
        conn = db.connect(self.db_path)
        run = db.last_run(conn)
        self.assertEqual(run['ok_count'], 1)
        self.assertEqual(run['fail_count'], 0)
        self.assertIsNotNone(run['finished_at'])
        conn.close()

    def test_evaluate_status_window(self):
        spec = SERIES['us10y']
        self.assertEqual(collect_module.evaluate_status(spec, date(2026, 9, 18), date(2026, 9, 19)), 'normal')
        self.assertEqual(collect_module.evaluate_status(spec, date(2026, 9, 10), date(2026, 9, 19)), 'stale')

    def test_only_filter_skips_other_metrics(self):
        jobs = [
            _job(['us10y'], {'us10y': [(date(2026, 9, 18), 5.0)]}),
            _job(['xauusd'], {'xauusd': [(date(2026, 9, 18), 4348.15)]}),
        ]
        with mock.patch.object(collect_module, 'build_jobs', return_value=jobs):
            results = collect_module.collect(self.db_path, today=FRIDAY, only=['xauusd'])
        self.assertEqual([item.metric for item in results], ['xauusd'])


class IdempotencyEdgeTests(unittest.TestCase):
    def test_upsert_ignores_none_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / 'x.sqlite3')
            db.init_db(conn)
            inserted = db.upsert_observations(conn, 'us10y', [(date(2026, 9, 18), None)], 'src', db.utcnow())
            self.assertEqual(inserted, 0)
            conn.close()


SENTIMENT_METRICS = (
    'amount',
    'volume',
    'turnover_rate',
    'margin_net_buy',
    'margin_turnover',
    'limit_up_count',
    'limit_down_count',
)


class SentimentInputJobTests(unittest.TestCase):
    """A股情绪输入任务必须接入编排，且单个指标失败不影响其它指标。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'test.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, jobs):
        with mock.patch.object(collect_module, 'build_jobs', return_value=jobs):
            return collect_module.collect(self.db_path, today=FRIDAY, full=False)

    def test_build_jobs_wires_sentiment_inputs(self):
        jobs = collect_module.build_jobs(session=object(), today=FRIDAY, full=False, api_key='test')
        by_name = {job.name: job for job in jobs}
        self.assertIn('A股情绪输入', by_name)
        job = by_name['A股情绪输入']
        self.assertEqual(job.metrics, SENTIMENT_METRICS)
        self.assertTrue(job.requires_mx)
        for metric in job.metrics:
            with self.subTest(metric=metric):
                self.assertIn(metric, SERIES)
                self.assertIn(metric, LOOKBACK_DAYS)

    def test_build_jobs_wires_sh_close(self):
        """上证指数是位置判定的输入：必须登记序列、回补窗口与采集作业。"""
        jobs = collect_module.build_jobs(session=object(), today=FRIDAY, full=False, api_key='test')
        by_name = {job.name: job for job in jobs}
        self.assertIn('上证指数', by_name)
        job = by_name['上证指数']
        self.assertEqual(job.metrics, ('sh_close',))
        self.assertTrue(job.requires_mx)
        self.assertIn('sh_close', SERIES)
        self.assertIn('sh_close', LOOKBACK_DAYS)
        self.assertEqual(SERIES['sh_close'].unit, '点')

    def test_sh_close_is_fetched_through_the_mx_query(self):
        with mock.patch.object(
            mx, 'query', return_value=_mx_payload([('2025-01-02', 3262.5607)])
        ) as patched:
            session = mock.MagicMock()
            fetched = mx.fetch_sh_close(session, 'key', date(2025, 1, 1), date(2025, 12, 31))
        self.assertEqual(fetched.rows, [(date(2025, 1, 2), 3262.5607)])
        self.assertIn('上证指数收盘价', patched.call_args.args[2])

    def test_partial_failure_marks_only_the_failed_metric(self):
        outcome = collect_module.JobOutcome(
            {
                'amount': [(date(2026, 9, 17), 2.1e12), (date(2026, 9, 18), 2.0e12)],
                'volume': [],
                'turnover_rate': [(date(2026, 9, 18), 1.49)],
            },
            {'volume': 'volume 抓取失败：妙想未返回数据'},
        )
        job = collect_module.Job('A股情绪输入', ('amount', 'volume', 'turnover_rate'), lambda: outcome)
        results = {item.metric: item for item in self._run([job])}
        self.assertTrue(results['amount'].ok)
        self.assertEqual(results['amount'].status, 'normal')
        self.assertTrue(results['turnover_rate'].ok)
        self.assertFalse(results['volume'].ok)
        self.assertEqual(results['volume'].status, 'missing')
        self.assertIn('volume 抓取失败', results['volume'].message)

    def test_failed_metric_keeps_previous_value(self):
        good = collect_module.Job(
            'A股情绪输入', ('amount',), lambda: collect_module.JobOutcome({'amount': [(date(2026, 9, 17), 2.1e12)]})
        )
        self._run([good])
        bad = collect_module.Job(
            'A股情绪输入',
            ('amount',),
            lambda: collect_module.JobOutcome({'amount': []}, {'amount': 'amount 抓取失败：超时'}),
        )
        self._run([bad])
        conn = db.connect(self.db_path)
        latest = db.latest_observation(conn, 'amount')
        status = db.get_statuses(conn)['amount']
        conn.close()
        self.assertEqual(latest['value'], 2.1e12)
        self.assertEqual(status['status'], 'missing')
        self.assertIn('超时', status['note'])

    def test_missing_api_key_marks_unconfigured(self):
        jobs = collect_module.build_jobs(session=object(), today=FRIDAY, full=False, api_key=None)
        by_name = {job.name: job for job in jobs}
        job = by_name['A股情绪输入']
        with mock.patch.object(collect_module.mx, 'api_key_from_env', return_value=None), mock.patch.object(
            collect_module, 'build_jobs', return_value=[job]
        ):
            results = collect_module.collect(self.db_path, today=FRIDAY, full=False, api_key=None)
        self.assertEqual({item.status for item in results}, {'unconfigured'})
        self.assertEqual(len(results), len(SENTIMENT_METRICS))


class BackfillWindowTests(unittest.TestCase):
    """回补年限可配：--years N 覆盖默认的 full/增量窗口。"""

    def _captured_start(self, *, full: bool, years: int | None) -> date:
        captured: dict[str, date] = {}

        def fake_fetch(session, api_key, start, end):
            captured['start'] = start
            captured['end'] = end
            return {}

        jobs = collect_module.build_jobs(
            session=object(), today=FRIDAY, full=full, api_key='test', years=years
        )
        job = next(item for item in jobs if item.name == 'A股情绪输入')
        with mock.patch.object(collect_module.mx, 'fetch_a_share_activity', side_effect=fake_fetch):
            job.fetch()
        self.assertEqual(captured['end'], FRIDAY)
        return captured['start']

    def test_years_option_overrides_window(self):
        self.assertEqual(self._captured_start(full=False, years=2), date(2024, 9, 18))
        self.assertEqual(self._captured_start(full=True, years=2), date(2024, 9, 18))

    def test_default_windows_are_unchanged_without_years(self):
        from macroboard.config import CHART_BACKFILL_YEARS

        self.assertEqual(self._captured_start(full=False, years=None), FRIDAY - timedelta(days=400))
        self.assertEqual(
            self._captured_start(full=True, years=None),
            FRIDAY - relativedelta(years=CHART_BACKFILL_YEARS),
        )

    def test_invalid_years_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                collect_module.collect(Path(tmp) / 'x.sqlite3', today=FRIDAY, years=0)

    def test_cli_accepts_years_and_rejects_zero(self):
        from update_data import parse_args

        args = parse_args(['--years', '2', '--only', 'amount'])
        self.assertEqual(args.years, 2)
        self.assertEqual(args.only, ['amount'])
        with self.assertRaises(SystemExit):
            parse_args(['--years', '0'])

    def test_turnover_rate_spec_documents_total_share_capital(self):
        spec = SERIES['turnover_rate']
        self.assertIn('总股本', spec.source_detail)
        self.assertIn('总股本', spec.note)


if __name__ == '__main__':
    unittest.main()
