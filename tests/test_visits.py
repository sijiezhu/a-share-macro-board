"""访问统计：计数、匿名去重、北京时间分日、路径解析与容错。"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

from macroboard import visits


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute('SELECT * FROM visits ORDER BY visit_id').fetchall()
    finally:
        conn.close()


class VisitStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / 'visits.sqlite3'
        # 2026-09-20 02:30 UTC = 北京时间 10:30
        self.now = datetime(2026, 9, 20, 2, 30, tzinfo=UTC)

    def test_counts_visits_and_unique_visitors(self) -> None:
        visits.record_visit(self.db, ip='192.168.1.66', user_agent='UA-1', now=self.now)
        visits.record_visit(self.db, ip='192.168.1.66', user_agent='UA-1', now=self.now)
        visits.record_visit(self.db, ip='192.168.1.77', user_agent='UA-2', now=self.now)

        stats = visits.load_stats(self.db, today=date(2026, 9, 20))

        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertEqual(stats.today_visits, 3)
        self.assertEqual(stats.today_visitors, 2)
        self.assertEqual(stats.window_visits, 3)
        self.assertEqual(stats.total_visits, 3)
        self.assertEqual(stats.total_visitors, 2)
        self.assertEqual(
            [(item.day.isoformat(), item.visits) for item in stats.daily],
            [('2026-09-20', 3)],
        )
        self.assertIsNotNone(stats.first_at)
        self.assertIsNotNone(stats.last_at)

    def test_same_visitor_is_stable_within_one_database(self) -> None:
        first = visits.record_visit(self.db, ip='10.0.0.5', user_agent='UA', now=self.now)
        second = visits.record_visit(self.db, ip='10.0.0.5', user_agent='UA', now=self.now)
        other = visits.record_visit(self.db, ip='10.0.0.6', user_agent='UA', now=self.now)

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), visits.FINGERPRINT_LENGTH)

    def test_client_ip_is_stored_for_backend_review(self) -> None:
        visits.record_visit(self.db, ip='203.0.113.9', user_agent='UA', now=self.now)

        rows = _rows(self.db)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['ip'], '203.0.113.9')
        self.assertEqual(rows[0]['day'], '2026-09-20')

    def test_user_agent_is_only_kept_as_fingerprint(self) -> None:
        visits.record_visit(self.db, ip='203.0.113.9', user_agent='SecretAgent/1.0', now=self.now)

        stored = ' '.join(str(cell) for row in _rows(self.db) for cell in row)

        self.assertNotIn('SecretAgent', stored)
        self.assertEqual(len(_rows(self.db)[0]['visitor']), visits.FINGERPRINT_LENGTH)

    def test_local_visit_stores_null_ip_but_is_still_counted(self) -> None:
        visits.record_visit(self.db, ip=None, user_agent='UA', now=self.now)

        self.assertIsNone(_rows(self.db)[0]['ip'])
        stats = visits.load_stats(self.db, today=date(2026, 9, 20))
        assert stats is not None
        self.assertEqual(stats.today_visits, 1)

    def test_ip_is_trimmed_bounded_and_blank_becomes_null(self) -> None:
        visits.record_visit(self.db, ip='  198.51.100.7  ', user_agent='UA', now=self.now)
        visits.record_visit(self.db, ip='9' * 100, user_agent='UA', now=self.now)
        visits.record_visit(self.db, ip='   ', user_agent='UA', now=self.now)

        self.assertEqual([row['ip'] for row in _rows(self.db)], ['198.51.100.7', '9' * 64, None])

    def test_non_string_ip_is_ignored(self) -> None:
        """接口变更或测试替身给出的非字符串一律记 NULL，不写坏数据库。"""
        self.assertIsNone(visits.normalize_ip(12345))
        visits.record_visit(self.db, ip=object(), user_agent='UA', now=self.now)

        self.assertIsNone(_rows(self.db)[0]['ip'])

    def test_old_database_without_ip_column_is_migrated(self) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(
                'CREATE TABLE visits ('
                'visit_id INTEGER PRIMARY KEY AUTOINCREMENT,'
                'at TEXT NOT NULL, day TEXT NOT NULL, visitor TEXT NOT NULL);'
            )
            conn.execute(
                'INSERT INTO visits (at, day, visitor) VALUES (?, ?, ?)',
                ('2026-09-19T01:00:00Z', '2026-09-19', 'oldfingerprint'),
            )
            conn.commit()
        finally:
            conn.close()

        visits.record_visit(self.db, ip='192.0.2.10', user_agent='UA', now=self.now)

        self.assertEqual([row['ip'] for row in _rows(self.db)], [None, '192.0.2.10'])
        stats = visits.load_stats(self.db, today=date(2026, 9, 20))
        assert stats is not None
        self.assertEqual(stats.total_visits, 2)

    def test_day_bucket_uses_beijing_time(self) -> None:
        # 2026-09-19 17:00 UTC = 北京时间 2026-09-20 01:00
        visits.record_visit(
            self.db, ip='10.0.0.1', user_agent='UA', now=datetime(2026, 9, 19, 17, 0, tzinfo=UTC)
        )
        # 2026-09-19 15:00 UTC = 北京时间 2026-09-19 23:00
        visits.record_visit(
            self.db, ip='10.0.0.2', user_agent='UA', now=datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
        )

        stats = visits.load_stats(self.db, today=date(2026, 9, 20))

        assert stats is not None
        self.assertEqual(stats.today_visits, 1)
        self.assertEqual(
            [item.day.isoformat() for item in stats.daily],
            ['2026-09-19', '2026-09-20'],
        )
        self.assertEqual(stats.total_visits, 2)

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        visits.record_visit(self.db, ip='10.0.0.1', user_agent='UA', now=datetime(2026, 9, 20, 2, 30))

        stats = visits.load_stats(self.db, today=date(2026, 9, 20))

        assert stats is not None
        self.assertEqual(stats.today_visits, 1)

    def test_window_and_history_windows_are_separate(self) -> None:
        visits.record_visit(
            self.db, ip='10.0.0.1', user_agent='UA', now=datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
        )
        visits.record_visit(self.db, ip='10.0.0.1', user_agent='UA', now=self.now)

        stats = visits.load_stats(
            self.db, today=date(2026, 9, 20), window_days=7, history_days=14
        )

        assert stats is not None
        self.assertEqual(stats.window_visits, 1)
        self.assertEqual(stats.today_visits, 1)
        self.assertEqual(stats.total_visits, 2)
        self.assertEqual([item.day.isoformat() for item in stats.daily], ['2026-09-20'])

    def test_missing_file_returns_zero_stats(self) -> None:
        stats = visits.load_stats(self.db, today=date(2026, 9, 20))

        assert stats is not None
        self.assertEqual(stats.total_visits, 0)
        self.assertEqual(stats.total_visitors, 0)
        self.assertEqual(stats.daily, ())
        self.assertIsNone(stats.first_at)
        self.assertIsNone(stats.last_at)

    def test_unreadable_file_is_reported_as_none(self) -> None:
        self.db.write_text('这不是 SQLite 数据库', encoding='utf-8')

        self.assertIsNone(visits.load_stats(self.db))

    def test_database_file_is_owner_only(self) -> None:
        visits.record_visit(self.db, ip='10.0.0.1', user_agent='UA', now=self.now)

        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_visit_db_defaults_next_to_market_db(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MACROBOARD_VISIT_DB', None)
            path = visits.resolve_visit_db_path('/data/macroboard.sqlite3')

        self.assertEqual(path, Path('/data/visits.sqlite3'))

    def test_visit_db_can_be_overridden(self) -> None:
        with mock.patch.dict(os.environ, {'MACROBOARD_VISIT_DB': '/var/lib/macroboard/other.sqlite3'}):
            path = visits.resolve_visit_db_path('/data/macroboard.sqlite3')

        self.assertEqual(path, Path('/var/lib/macroboard/other.sqlite3'))

    def test_logging_can_be_disabled(self) -> None:
        for value in ('0', 'false', 'no', 'off', 'OFF'):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {'MACROBOARD_VISIT_LOG': value}
            ):
                self.assertFalse(visits.logging_enabled())
        with mock.patch.dict(os.environ, {'MACROBOARD_VISIT_LOG': '1'}):
            self.assertTrue(visits.logging_enabled())

    def test_beijing_text_conversion(self) -> None:
        self.assertEqual(visits.to_beijing_text('2026-09-20T02:30:00Z'), '2026-09-20 10:30')
        self.assertEqual(visits.to_beijing_text(None), '—')
        self.assertEqual(visits.to_beijing_text('not-a-date'), '—')

    def test_visits_stay_when_market_database_is_rebuilt(self) -> None:
        """统计库与行情库分开：行情库删掉重建不影响历史访问记录。"""
        market_db = Path(self._tmp.name) / 'macroboard.sqlite3'
        market_db.write_bytes(b'')
        with mock.patch.dict(os.environ, {'MACROBOARD_VISIT_DB': ''}):
            visit_db = visits.resolve_visit_db_path(market_db)
            visits.record_visit(visit_db, ip='10.0.0.1', user_agent='UA', now=self.now)

        market_db.unlink()
        with mock.patch.dict(os.environ, {'MACROBOARD_VISIT_DB': ''}):
            rebuilt_path = visits.resolve_visit_db_path(market_db)
        stats = visits.load_stats(rebuilt_path, today=date(2026, 9, 20))

        assert stats is not None
        self.assertEqual(rebuilt_path, visit_db)
        self.assertEqual(stats.total_visits, 1)

    def test_history_ignores_out_of_window_days(self) -> None:
        visits.record_visit(
            self.db, ip='10.0.0.1', user_agent='UA', now=self.now - timedelta(days=40)
        )

        stats = visits.load_stats(self.db, today=date(2026, 9, 20), history_days=14)

        assert stats is not None
        self.assertEqual(stats.daily, ())
        self.assertEqual(stats.total_visits, 1)


if __name__ == '__main__':
    unittest.main()
