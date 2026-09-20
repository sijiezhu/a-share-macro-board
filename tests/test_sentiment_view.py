"""情绪页面数据组装：字段适配、预热、缺失与文案（不触网、不读真实库）。"""

from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from macroboard import sentiment_view


def db_series(size: int = 400) -> dict[str, list[tuple[date, float]]]:
    """构造库内序列（SYNTHETIC FIXTURE，非真实行情）。"""
    start = date(2025, 1, 1)
    days = [start + timedelta(days=index) for index in range(size)]
    close = [3000.0 + 300.0 * math.sin(index / 30.0) + index * 0.5 for index in range(size)]
    volume = [2e8 * (1.0 + 0.3 * math.sin(index / 17.0)) for index in range(size)]
    up = [1500.0 + 700.0 * math.sin(index / 11.0) for index in range(size)]
    down = [1500.0 - 700.0 * math.sin(index / 11.0) for index in range(size)]
    series = {
        'hs300_close': list(zip(days, close, strict=True)),
        'volume': list(zip(days, volume, strict=True)),
        'amount': [(day, v * c) for day, v, c in zip(days, volume, close, strict=True)],
        'turnover_rate': [(day, 1.2 + 0.4 * math.sin(index / 13.0)) for index, day in enumerate(days)],
        'margin_net_buy': [(day, 2e8 * math.sin(index / 7.0)) for index, day in enumerate(days)],
        'adv_count': list(zip(days, up, strict=True)),
        'dec_count': list(zip(days, down, strict=True)),
        'limit_up_count': [(day, max(0.0, 30.0 * math.sin(index / 9.0))) for index, day in enumerate(days)],
        'limit_down_count': [(day, max(0.0, -30.0 * math.sin(index / 9.0))) for index, day in enumerate(days)],
    }
    return series


class InputFrameTests(unittest.TestCase):
    def test_maps_db_metrics_to_module_columns(self):
        frame = sentiment_view.build_input_frame(db_series(5))
        for column in ('close', 'volume', 'amount', 'turnover_rate', 'margin_net_buy',
                       'up_count', 'down_count', 'limit_up_count', 'limit_down_count'):
            with self.subTest(column=column):
                self.assertIn(column, frame.columns)
        self.assertNotIn('pcr', frame.columns)
        self.assertNotIn('implied_volatility', frame.columns)

    def test_frame_is_sorted_and_has_no_forward_fill(self):
        series = db_series(5)
        series['amount'] = series['amount'][:3]  # 成交额少两天
        frame = sentiment_view.build_input_frame(series)
        self.assertTrue(frame['date'].is_monotonic_increasing)
        self.assertTrue(frame['amount'].tail(2).isna().all())
        self.assertFalse(frame['close'].isna().any())

    def test_dates_are_aligned_by_union_without_resampling(self):
        days = [date(2025, 1, 2), date(2025, 1, 6)]  # 跳过 1/3~1/5（周末与周一）
        series = {
            'hs300_close': [(days[0], 3000.0), (days[1], 3010.0)],
            'volume': [(days[1], 2e8)],
        }
        frame = sentiment_view.build_input_frame(series)
        self.assertEqual(len(frame), 2)
        self.assertEqual(list(frame['date'].dt.date), days)

    def test_empty_series_returns_empty_frame(self):
        frame = sentiment_view.build_input_frame({})
        self.assertEqual(len(frame), 0)
        self.assertIn('date', frame.columns)


class BuildViewTests(unittest.TestCase):
    def test_view_is_available_with_enough_history(self):
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        self.assertTrue(view.available)
        self.assertEqual(view.window, 252)
        self.assertGreater(view.used_rows, 0)
        self.assertEqual(view.as_of, date(2025, 1, 1) + timedelta(days=399))
        self.assertTrue(view.curves['sentiment_index'])
        self.assertTrue(view.curves['participation_index'])
        self.assertEqual(view.percentile_method, 'midrank_exclusive')
        self.assertIn('turnover', view.profile)

    def test_snapshot_carries_page_fields(self):
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        snapshot = view.snapshot
        for key in (
            'sentiment_index',
            'participation_index',
            'direction_index',
            'participation_coverage',
            'direction_coverage',
            'panic_score',
            'panic_coverage',
            'panic_confidence',
            'reversal_watch',
            'state_label',
            'macd_volume_state_label',
            'data_quality_label',
        ):
            with self.subTest(key=key):
                self.assertIn(key, snapshot)
        self.assertEqual(snapshot['direction_missing'], [])
        self.assertAlmostEqual(float(snapshot['direction_coverage']), 1.0, places=9)

    def test_short_history_is_marked_insufficient_not_neutral(self):
        view = sentiment_view.build_sentiment_view(db_series(80), {})
        self.assertFalse(view.available)
        self.assertEqual(view.status, 'insufficient')
        self.assertIn('样本不足', view.reason)
        self.assertEqual(view.snapshot['state_label'], '不可用（数据不足）')
        self.assertEqual(view.curves['sentiment_index'], [])

    def test_empty_library_is_missing(self):
        view = sentiment_view.build_sentiment_view({}, {})
        self.assertFalse(view.available)
        self.assertEqual(view.status, 'missing')
        self.assertIn('尚未采集', view.reason)
        self.assertEqual(view.used_rows, 0)

    def test_input_status_propagates(self):
        statuses = {'amount': {'status': 'stale'}, 'hs300_close': {'status': 'normal'}}
        view = sentiment_view.build_sentiment_view(db_series(400), statuses)
        self.assertEqual(view.status, 'stale')

    def test_fields_report_dates_and_status(self):
        series = db_series(400)
        statuses = {'amount': {'status': 'stale'}}
        view = sentiment_view.build_sentiment_view(series, statuses)
        by_column = {info.column: info for info in view.fields}
        self.assertEqual(by_column['close'].metric, 'hs300_close')
        self.assertEqual(by_column['up_count'].metric, 'adv_count')
        self.assertEqual(by_column['amount'].status, 'stale')
        self.assertEqual(by_column['close'].latest_date, date(2025, 1, 1) + timedelta(days=399))
        self.assertEqual(len(sentiment_view.field_frame(view)), len(view.fields))

    def test_not_connected_components_are_out_of_scope_not_missing(self):
        """未接通的分量直接从页面口径里剔除：不报缺失、覆盖率 100%、数据质量正常。"""
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        names = {component.name for component in sentiment_view.view_config().quality.components}
        self.assertNotIn('pcr', names)
        self.assertNotIn('iv', names)
        self.assertEqual(names, {'turnover', 'amount', 'volume_ratio', 'margin_net_buy',
                                 'return_20d', 'macd_hist_norm', 'limit_up_down_ratio', 'up_ratio'})
        self.assertAlmostEqual(float(view.snapshot['direction_coverage']), 1.0, places=9)
        self.assertEqual(view.snapshot['data_quality_label'], '正常')
        self.assertFalse(any('pcr' in note or 'iv' in note or '未接通' in note for note in view.notes))

    def test_dropping_optional_components_keeps_direction_value(self):
        """等权合成下，剔除未接通分量与"按可用分量重归一"数值等价。"""
        series = db_series(400)
        with_optional = sentiment_view.build_sentiment_view(series, {}, config=sentiment_view.default_config())
        without = sentiment_view.build_sentiment_view(series, {})
        self.assertAlmostEqual(
            float(with_optional.snapshot['direction_index']),
            float(without.snapshot['direction_index']),
            places=9,
        )

    def test_quadrant_points_pair_both_indices(self):
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        self.assertTrue(view.quadrant_points)
        last = view.quadrant_points[-1]
        self.assertEqual(last[0], view.as_of)
        self.assertAlmostEqual(last[1], float(view.snapshot['participation_index']), places=9)
        self.assertAlmostEqual(last[2], float(view.snapshot['direction_index']), places=9)
        self.assertTrue(all(0 <= point[1] <= 100 and 0 <= point[2] <= 100 for point in view.quadrant_points))

    def test_notes_explain_lag_and_window(self):
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        text = ' '.join(view.notes)
        self.assertIn('两融 T+1', text)
        self.assertIn('252', text)
        self.assertIn('中位秩', text)


class WordingTests(unittest.TestCase):
    def test_rendered_context_has_no_advice_language(self):
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        ctx = sentiment_view.render_context(view)
        forbidden = ('买入', '卖出', '建议', '目标价', '仓位', '必涨')
        for key, value in ctx.items():
            for word in forbidden:
                with self.subTest(key=key, word=word):
                    self.assertNotIn(word, str(value))

    def test_state_and_macd_text_helpers(self):
        self.assertEqual(sentiment_view.state_text('STATE_PANIC'), '恐慌抛售（放量急跌型冰点）')
        self.assertEqual(sentiment_view.state_text('STATE_UNKNOWN'), '不可用（数据不足）')
        self.assertEqual(sentiment_view.macd_state_text('MACD_VOL_NEUTRAL'), '中性')
        self.assertEqual(sentiment_view.macd_state_text('NOPE'), '未接通')

    def test_empty_snapshot_is_not_read_as_neutral(self):
        snapshot = sentiment_view._empty_snapshot()
        self.assertEqual(snapshot['state_label'], '不可用（数据不足）')
        self.assertNotIn('中性', str(snapshot['state_label']))


class LoaderTests(unittest.TestCase):
    def test_loader_reads_temp_database(self):
        import tempfile
        from pathlib import Path

        from macroboard import db

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.sqlite3'
            conn = db.connect(path)
            db.init_db(conn)
            db.upsert_observations(
                conn, 'amount', [(date(2026, 9, 18), 2.0e12)], 'test', db.utcnow()
            )
            db.set_status(
                conn,
                'amount',
                'normal',
                obs_date='2026-09-18',
                value=2.0e12,
                source='test',
                checked_at=db.utcnow(),
            )
            conn.close()
            series, statuses = sentiment_view.load_series_and_statuses(path)
        self.assertEqual(series['amount'], [(date(2026, 9, 18), 2.0e12)])
        self.assertEqual(statuses['amount']['status'], 'normal')


if __name__ == '__main__':
    unittest.main()
