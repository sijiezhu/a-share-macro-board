"""情绪页面数据组装：字段适配、预热、缺失与文案（不触网、不读真实库）。"""

from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from macroboard import sentiment_view
from macroboard.sentiment import codes, scoring


def db_series(size: int = 400) -> dict[str, list[tuple[date, float]]]:
    """构造库内序列（SYNTHETIC FIXTURE，非真实行情）。"""
    start = date(2025, 1, 1)
    days = [start + timedelta(days=index) for index in range(size)]
    close = [3000.0 + 300.0 * math.sin(index / 30.0) + index * 0.5 for index in range(size)]
    volume = [2e8 * (1.0 + 0.3 * math.sin(index / 17.0)) for index in range(size)]
    up = [1500.0 + 700.0 * math.sin(index / 11.0) for index in range(size)]
    down = [1500.0 - 700.0 * math.sin(index / 11.0) for index in range(size)]
    # 上证指数（位置口径）：与沪深300同向但幅度不同，保证窗口内出现高位与低位
    sh_close = [2900.0 + 400.0 * math.sin(index / 45.0) + index * 0.4 for index in range(size)]
    series = {
        'hs300_close': list(zip(days, close, strict=True)),
        'sh_close': list(zip(days, sh_close, strict=True)),
        'volume': list(zip(days, volume, strict=True)),
        'amount': [(day, v * c) for day, v, c in zip(days, volume, close, strict=True)],
        'turnover_rate': [(day, 1.2 + 0.4 * math.sin(index / 13.0)) for index, day in enumerate(days)],
        'margin_net_buy': [(day, 2e8 * math.sin(index / 7.0)) for index, day in enumerate(days)],
        'margin_turnover': [(day, 6e8 + 2e8 * math.sin(index / 7.0)) for index, day in enumerate(days)],
        'adv_count': list(zip(days, up, strict=True)),
        'dec_count': list(zip(days, down, strict=True)),
        'limit_up_count': [(day, max(0.0, 30.0 * math.sin(index / 9.0))) for index, day in enumerate(days)],
        'limit_down_count': [(day, max(0.0, -30.0 * math.sin(index / 9.0))) for index, day in enumerate(days)],
    }
    return series


def panic_tail(series: dict[str, list[tuple[date, float]]], tail: int = 30):
    """把尾部改成"放量急跌"：参与度高、方向低，稳定落在恐慌象限。

    默认 `db_series` 的尾部落在方向 40—60 的中性带，做不出复合状态，
    因此显式构造一段可判定的尾部：价格连续下跌（压 return_20d 与 MACD）
    + 放量（抬参与度）+ 跌停潮（压方向）。仍是合成样本。
    """
    size = len(series['hs300_close'])
    days = [day for day, _value in series['hs300_close']]
    adjusted = {key: list(rows) for key, rows in series.items()}
    start = size - tail
    peak = series['hs300_close'][start - 1][1]
    for index in range(start, size):
        step = index - start
        progress = step / max(tail - 1, 1)
        # 尾部累计下跌 ≈ 18%，且逐日走低（MACD 柱同步转负）
        price = peak * (1.0 - 0.18 * progress)
        surge = 1.0 + 3.0 * progress
        adjusted['hs300_close'][index] = (days[index], price)
        adjusted['volume'][index] = (days[index], 2e8 * surge)
        adjusted['amount'][index] = (days[index], 2e8 * surge * price)
        adjusted['margin_turnover'][index] = (days[index], 6e8 + 1.5e9 * progress)
        adjusted['turnover_rate'][index] = (days[index], 1.5 + 3.0 * progress)
        adjusted['adv_count'][index] = (days[index], 300.0)
        adjusted['dec_count'][index] = (days[index], 2400.0)
        adjusted['limit_up_count'][index] = (days[index], 2.0)
        adjusted['limit_down_count'][index] = (days[index], 40.0)
    return adjusted


class InputFrameTests(unittest.TestCase):
    def test_maps_db_metrics_to_module_columns(self):
        frame = sentiment_view.build_input_frame(db_series(5))
        for column in ('close', 'sh_close', 'volume', 'amount', 'turnover_rate', 'margin_net_buy',
                       'margin_turnover', 'up_count', 'down_count', 'limit_up_count',
                       'limit_down_count'):
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

    def test_positions_cover_every_row_with_a_code(self):
        """逐日价格位置：与输入表逐行对齐（含位置未知的预热期），供图上铺底色。"""
        series = db_series(400)
        view = sentiment_view.build_sentiment_view(series, {})
        self.assertEqual(len(view.positions), len(series['sh_close']))
        days = [day for day, _code in view.positions]
        self.assertEqual(days, [day for day, _value in series['sh_close']])
        seen = {code for _day, code in view.positions}
        self.assertTrue(seen <= set(codes.POSITION_CODES))
        # 合成样本里价格来回穿越均线：高位与低位都出现过，均线没算出来的那几天是未知
        self.assertIn(codes.POS_HIGH, seen)
        self.assertIn(codes.POS_LOW, seen)
        self.assertIn(codes.POS_UNKNOWN, seen)

    def test_positions_are_empty_when_the_library_is_empty(self):
        view = sentiment_view.build_sentiment_view({}, {})
        self.assertEqual(view.positions, [])

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
        self.assertEqual(names, {'turnover', 'amount', 'volume_ratio', 'margin_turnover',
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

    def test_incomplete_margin_day_is_explained_in_the_notes(self):
        """源站发布不完整的观测日：页面说明要写明日期与原因，且该日退化 100% 量比。"""
        series = db_series(400)
        series['amount'] = [(day, 1.0e12) for day, _value in series['amount']]
        series['margin_turnover'] = [(day, 1.7e11) for day, _value in series['margin_turnover']]
        series['margin_turnover'][-1] = (series['margin_turnover'][-1][0], 1.7e11 * 0.40)
        view = sentiment_view.build_sentiment_view(series, {})
        self.assertEqual(len(view.margin_completeness), 1)
        self.assertEqual(view.margin_completeness[0].day.date(), view.as_of)
        text = ' '.join(view.notes)
        self.assertIn('两融完整性校验', text)
        self.assertIn(view.as_of.isoformat(), text)
        self.assertAlmostEqual(float(view.snapshot['participation_coverage']), 0.5, places=9)
        self.assertIn('margin_turnover', tuple(view.snapshot['participation_missing']))
        component = next(c for c in view.components if c.name == 'margin_turnover')
        self.assertIsNone(component.weight_share)
        self.assertLess(component.value_date, view.as_of)

    def test_complete_margin_series_has_no_explanation(self):
        """比值平稳时不得出现任何剔除说明（避免把正常波动说成发布不完整）。"""
        series = db_series(400)
        series['amount'] = [(day, 1.0e12) for day, _value in series['amount']]
        series['margin_turnover'] = [(day, 1.7e11) for day, _value in series['margin_turnover']]
        view = sentiment_view.build_sentiment_view(series, {})
        self.assertEqual(view.margin_completeness, ())
        self.assertNotIn('两融完整性校验', ' '.join(view.notes))
        self.assertAlmostEqual(float(view.snapshot['participation_coverage']), 1.0, places=9)

    def test_quadrant_points_pair_both_indices_and_position(self):
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        self.assertTrue(view.quadrant_points)
        last = view.quadrant_points[-1]
        self.assertEqual(last[0], view.as_of)
        self.assertAlmostEqual(last[1], float(view.snapshot['participation_index']), places=9)
        self.assertAlmostEqual(last[2], float(view.snapshot['direction_index']), places=9)
        self.assertEqual(last[3], view.snapshot['price_position'])
        self.assertTrue(all(len(point) == 4 for point in view.quadrant_points))
        self.assertTrue(
            all(point[3] in codes.POSITION_CODES for point in view.quadrant_points)
        )
        self.assertTrue(all(0 <= point[1] <= 100 and 0 <= point[2] <= 100 for point in view.quadrant_points))

    def test_quadrant_points_keep_points_with_unknown_position(self):
        """位置缺失只影响标记，不能把当天的点从图上删掉。"""
        series = db_series(400)
        del series['sh_close']
        view = sentiment_view.build_sentiment_view(series, {})
        self.assertTrue(view.quadrant_points)
        self.assertTrue(all(point[3] == codes.POS_UNKNOWN for point in view.quadrant_points))
        self.assertEqual(len(view.quadrant_points), len(
            [point for point in sentiment_view.build_sentiment_view(db_series(400), {}).quadrant_points]
        ))

    def test_missing_sh_close_degrades_gracefully(self):
        series = db_series(400)
        del series['sh_close']
        view = sentiment_view.build_sentiment_view(series, {})
        self.assertTrue(view.available)
        self.assertEqual(view.snapshot['price_position'], codes.POS_UNKNOWN)
        context = sentiment_view.render_context(view)
        self.assertIn('位置未知', context['position_text'])
        self.assertIn('当日无上证指数观测', context['position_text'])
        # 状态退回四象限基础口径（不是复合码）
        self.assertIn(view.snapshot['state'], codes.STATE_CODES[:6])

    def test_position_text_names_the_levels_and_the_gap(self):
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        text = sentiment_view.render_context(view)['position_text']
        self.assertIn('上证指数', text)
        self.assertIn('60 日均线', text)
        self.assertTrue('高位' in text or '低位' in text)

    def test_position_text_explains_why_the_position_is_unknown(self):
        """两种"位置未知"的原因必须区分：均线样本不足 vs 当日无观测。"""
        base = {
            'price_position': codes.POS_UNKNOWN,
            'sh_close': 3000.0,
            'price_ma': None,
            'price_ma_window': 60,
            'price_ma_gap': None,
        }
        insufficient = sentiment_view.position_text(base)
        self.assertIn('位置未知', insufficient)
        self.assertIn('样本不足', insufficient)
        no_quote = sentiment_view.position_text({**base, 'sh_close': None})
        self.assertIn('位置未知', no_quote)
        self.assertIn('当日无上证指数观测', no_quote)
        self.assertNotIn('样本不足', no_quote)

    def test_state_text_differs_by_position_for_the_same_quadrant(self):
        """同一批情绪输入，只翻转上证指数趋势 -> 复合文案随之改变。"""
        series = panic_tail(db_series(400))
        rising = {**series}
        rising['sh_close'] = [
            (day, 3000.0 + 3.0 * index) for index, (day, _value) in enumerate(series['sh_close'])
        ]
        low = sentiment_view.build_sentiment_view(series, {})
        high = sentiment_view.build_sentiment_view(rising, {})
        self.assertEqual(low.snapshot['state'], codes.STATE_PANIC_LOW)
        self.assertEqual(high.snapshot['state'], codes.STATE_PANIC_HIGH)
        self.assertEqual(high.snapshot['price_position'], codes.POS_HIGH)
        self.assertEqual(low.snapshot['price_position'], codes.POS_LOW)
        self.assertNotEqual(high.snapshot['state_label'], low.snapshot['state_label'])
        self.assertIn('恐慌', str(low.snapshot['state_label']))
        self.assertIn('终止预警', str(high.snapshot['state_label']))

    def test_state_reference_frame_lists_every_combination(self):
        frame = sentiment_view.state_reference_frame()
        self.assertEqual(len(frame), len(scoring.STATE_COMBINATIONS))
        self.assertEqual(list(frame.columns), ['位置', '四象限基础', '组合状态'])
        for state in frame['组合状态']:
            self.assertTrue(state.strip())

    def test_notes_explain_margin_publication_and_window(self):
        """两融的公布时点与退路口径、以及分位窗口必须写在说明里。"""
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        text = ' '.join(view.notes)
        self.assertIn('两融 T+1', text)
        self.assertIn('量比得分独占', text)
        # 不再有「按 T-1 滞后使用」的声明，否则与算法实际行为不符
        self.assertNotIn('滞后', text)
        self.assertIn('252', text)
        self.assertIn('中位秩', text)

    def test_components_carry_latest_value_method_and_weight(self):
        """面板要显示的量比得分、两融交易额 20 日分位与方向分量都在 view.components 里。"""
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        by_name = {info.name: info for info in view.components}
        self.assertEqual(
            [name for name in by_name if by_name[name].in_index],
            ['volume_ratio', 'margin_turnover', 'return_20d', 'macd_hist_norm',
             'limit_up_down_ratio', 'up_ratio'],
        )
        volume_ratio = by_name['volume_ratio']
        self.assertEqual(volume_ratio.label, '量比得分')
        self.assertIn('阈值映射', volume_ratio.method)
        self.assertIn('不取分位', volume_ratio.method)
        self.assertAlmostEqual(volume_ratio.weight_share, 0.5, places=12)
        margin = by_name['margin_turnover']
        self.assertEqual(margin.label, '两融交易额分位')
        self.assertEqual(margin.availability_lag, 0)
        self.assertNotIn('T+1', margin.method)
        for info in view.components:
            if not info.in_index:
                with self.subTest(component=info.name):
                    self.assertIsNone(info.weight_share)
                continue
            with self.subTest(component=info.name):
                self.assertIsNotNone(info.value)
                self.assertAlmostEqual(info.value_date, view.as_of, places=0)
        shares = {info.name: info.weight_share for info in view.components if info.in_index}
        self.assertAlmostEqual(
            sum(shares[name] for name in ('volume_ratio', 'margin_turnover')), 1.0, places=12
        )
        self.assertAlmostEqual(
            sum(shares[name] for name in ('return_20d', 'macd_hist_norm',
                                          'limit_up_down_ratio', 'up_ratio')), 1.0, places=12
        )

    def test_component_frame_lists_values_and_usage(self):
        view = sentiment_view.build_sentiment_view(db_series(400), {})
        frame = sentiment_view.component_frame(view)
        self.assertEqual(
            list(frame.columns), ['分量', '用途', '口径', '最新值', '取值日期', '组内权重']
        )
        self.assertEqual(len(frame), len(view.components))
        by_label = frame.set_index('分量')
        self.assertEqual(by_label.loc['量比得分', '用途'], '参与度指数')
        self.assertEqual(by_label.loc['量比得分', '组内权重'], '50%')
        self.assertEqual(by_label.loc['换手率分位', '用途'], '不参与指数')
        self.assertEqual(by_label.loc['换手率分位', '组内权重'], '—')
        self.assertNotEqual(by_label.loc['量比得分', '取值日期'], '—')

    def test_component_weight_share_follows_availability(self):
        """缺字段时组内权重按可用分量重归一（与 compose_index 一致）。"""
        series = db_series(400)
        series.pop('margin_turnover')
        view = sentiment_view.build_sentiment_view(series, {})
        by_name = {info.name: info for info in view.components}
        self.assertIsNone(by_name['margin_turnover'].value)
        self.assertIsNone(by_name['margin_turnover'].weight_share)
        self.assertAlmostEqual(by_name['volume_ratio'].weight_share, 1.0, places=12)


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
        self.assertEqual(sentiment_view.state_text('STATE_PANIC'), '恐慌抛售')
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
