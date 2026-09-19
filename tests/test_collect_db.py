"""采集幂等性、失败保留旧值与状态判定测试（不触网）。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from macroboard import collect as collect_module
from macroboard import db
from macroboard.config import SERIES

FRIDAY = date(2026, 9, 18)


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


if __name__ == '__main__':
    unittest.main()
