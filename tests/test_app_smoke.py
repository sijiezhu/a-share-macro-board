"""页面整页冒烟：指向临时库，确认无异常且情绪区块已接线。

用 Streamlit 的 AppTest 在进程内跑 app.py，不启动服务器、不触网。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

from macroboard import charts, db

ROOT = Path(__file__).resolve().parents[1]


def _seed(path: Path, size: int = 200) -> None:
    """写入少量合成观测（SYNTHETIC FIXTURE），让页面各区块都有内容可渲染。

    日期锚定到「今天」往回推：页面按真实当前日期切片，固定的历史日期会落到短线
    默认时间范围（1 年）之外而显示为未接通，导致测试随日历老化。
    """
    conn = db.connect(path)
    db.init_db(conn)
    start = date.today() - timedelta(days=size - 1)
    days = [start + timedelta(days=index) for index in range(size)]
    metrics = {
        'us10y': [(day, 4.0 + index * 0.002) for index, day in enumerate(days)],
        'cn10y': [(day, 2.0 + index * 0.001) for index, day in enumerate(days)],
        'usdcny': [(day, 7.1 + index * 0.0005) for index, day in enumerate(days)],
        'xauusd': [(day, 3000.0 + index * 1.5) for index, day in enumerate(days)],
        'adv_count': [(day, 1500.0 + (index % 40) * 10) for index, day in enumerate(days)],
        'dec_count': [(day, 1500.0 - (index % 40) * 10) for index, day in enumerate(days)],
        'flat_count': [(day, 150.0) for day in days],
        'hs300_close': [(day, 3500.0 + index * 1.2) for index, day in enumerate(days)],
        # 上证指数上下震荡（相对 60 日均线来回穿越），让近期点位同时出现高位与低位
        'sh_close': [
            (day, 3200.0 + 260.0 * math.sin(index / 21.0)) for index, day in enumerate(days)
        ],
        'hs300_pe_ttm': [(day, 12.0 + index * 0.001) for index, day in enumerate(days)],
        'amount': [(day, 2.0e12 - index * 1e9) for index, day in enumerate(days)],
        'volume': [(day, 3.0e10 + index * 1e7) for index, day in enumerate(days)],
        'turnover_rate': [(day, 1.5 + (index % 20) * 0.01) for index, day in enumerate(days)],
        'margin_net_buy': [(day, 1e8 * (1 if index % 2 else -1)) for index, day in enumerate(days)],
        'margin_turnover': [(day, 5e8 + 1e8 * (1 if index % 2 else -1)) for index, day in enumerate(days)],
        'limit_up_count': [(day, 40.0 + (index % 15)) for index, day in enumerate(days)],
        'limit_down_count': [(day, 5.0 + (index % 5)) for index, day in enumerate(days)],
    }
    stamp = db.utcnow()
    for metric, rows in metrics.items():
        db.upsert_observations(conn, metric, rows, 'test', stamp)
        db.set_status(
            conn,
            metric,
            'normal',
            obs_date=rows[-1][0].isoformat(),
            value=rows[-1][1],
            source='test',
            checked_at=stamp,
        )
    conn.close()


class AppSmokeTests(unittest.TestCase):
    def test_full_page_renders_and_includes_sentiment_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            previous = os.environ.get('MACROBOARD_DB')
            os.environ['MACROBOARD_DB'] = str(path)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                if previous is None:
                    os.environ.pop('MACROBOARD_DB', None)
                else:
                    os.environ['MACROBOARD_DB'] = previous

        self.assertEqual([str(item.value) for item in app.exception], [])
        subheaders = [item.value for item in app.subheader]
        self.assertIn('短线指标：A股市场情绪', subheaders)
        self.assertIn('长线指标：宏观经济与估值', subheaders)
        # 「A股市场宽度」区块暂时下线（app.py::SHOW_BREADTH_BLOCK = False）；
        # 重新上线时这条断言与下面的控件数量断言要一起改回三个区块。
        self.assertNotIn('短线指标：A股市场宽度', subheaders)
        markdown = ' '.join(item.value for item in app.markdown)
        self.assertIn('沪深300股债利差', markdown)
        labels = [item.label for item in app.metric]
        for expected in (
            '综合情绪指数',
            '参与度指数',
            '方向指数',
            '恐慌抛售得分',
            '冰点反转观察',
            '量价状态',
        ):
            with self.subTest(metric=expected):
                self.assertIn(expected, labels)

    def test_range_controls_are_independent_per_block(self):
        """情绪与长线各有一组时间范围与粒度控件，互不联动。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            os.environ['MACROBOARD_DB'] = str(path)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
                range_boxes = [item for item in app.selectbox if item.label == '时间范围']
                self.assertEqual(
                    [item.key for item in range_boxes],
                    ['sentiment-range', 'long-range'],
                )
                self.assertEqual([item.value for item in range_boxes], ['1年', '5年'])
                resolution_boxes = [item for item in app.selectbox if item.label == '曲线粒度']
                self.assertEqual(len(resolution_boxes), 2)

                range_boxes[0].set_value('全部')
                app.run()
                range_boxes = [item for item in app.selectbox if item.label == '时间范围']
                self.assertEqual([item.value for item in range_boxes], ['全部', '5年'])
            finally:
                os.environ.pop('MACROBOARD_DB', None)
        self.assertEqual([str(item.value) for item in app.exception], [])

    def test_quadrant_state_is_visible_and_no_missing_noise(self):
        """四象限状态必须能在页面上直接看到；未接通的分量不再反复提示缺失。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            os.environ['MACROBOARD_DB'] = str(path)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                os.environ.pop('MACROBOARD_DB', None)
        self.assertEqual([str(item.value) for item in app.exception], [])
        markdown = ' '.join(item.value for item in app.markdown)
        self.assertIn('四象限状态', markdown)
        captions = ' '.join(item.value for item in app.caption)
        for word in ('未接通', '缺失分量', 'pcr', 'PCR', '隐含波动率'):
            with self.subTest(word=word):
                self.assertNotIn(word, captions)

    def test_price_position_is_shown_on_the_sentiment_card(self):
        """高位/低位必须写在卡片上，且图上给出 ●/○ 图例（否则高低位不可见）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            os.environ['MACROBOARD_DB'] = str(path)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                os.environ.pop('MACROBOARD_DB', None)
        self.assertEqual([str(item.value) for item in app.exception], [])
        markdown = ' '.join(item.value for item in app.markdown)
        self.assertIn('位置：', markdown)
        self.assertIn('60 日均线', markdown)
        self.assertTrue('高位' in markdown or '低位' in markdown)
        self.assertIn('四象限', markdown)
        captions = ' '.join(item.value for item in app.caption)
        self.assertIn('● 高位', captions)
        self.assertIn('○ 低位', captions)

    def test_index_components_are_folded_on_the_panel(self):
        """分量的逐项取值收在默认收起的折叠区里（与「情绪指标口径与输入数据」一致）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            os.environ['MACROBOARD_DB'] = str(path)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                os.environ.pop('MACROBOARD_DB', None)
        self.assertEqual([str(item.value) for item in app.exception], [])
        labels = [item.label for item in app.metric]
        for label in (
            '量比得分',
            '两融交易额分位',
            '20 日收益分位',
            'MACD 柱分位',
            '涨跌停比分位',
            '上涨家数占比分位',
        ):
            with self.subTest(label=label):
                self.assertIn(label, labels)
        captions = ' '.join(item.value for item in app.caption)
        folded = [item for item in app.expander if item.label == '指数分量（进入计算的最新取值）']
        self.assertEqual([item.proto.expanded for item in folded], [False])  # 默认收起
        self.assertIn('参与度指数 = ', captions)
        self.assertIn('方向指数 = ', captions)
        self.assertIn('组内权重', captions)

    def test_manual_refresh_button_is_hidden_by_default(self):
        """页面可能被局域网访问：默认不暴露会触发采集（消耗配额）的按钮。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            os.environ['MACROBOARD_DB'] = str(path)
            os.environ.pop('MACROBOARD_ENABLE_MANUAL_REFRESH', None)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                os.environ.pop('MACROBOARD_DB', None)
        self.assertEqual([str(item.value) for item in app.exception], [])
        self.assertEqual([item.label for item in app.button], [])

    def test_manual_refresh_button_can_be_enabled_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            os.environ['MACROBOARD_DB'] = str(path)
            os.environ['MACROBOARD_ENABLE_MANUAL_REFRESH'] = '1'
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                os.environ.pop('MACROBOARD_DB', None)
                os.environ.pop('MACROBOARD_ENABLE_MANUAL_REFRESH', None)
        self.assertEqual([str(item.value) for item in app.exception], [])
        self.assertIn('立即采集一次', [item.label for item in app.button])

    def test_page_renders_when_database_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'empty.sqlite3'
            os.environ['MACROBOARD_DB'] = str(path)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                os.environ.pop('MACROBOARD_DB', None)
        self.assertEqual([str(item.value) for item in app.exception], [])
        self.assertIn('短线指标：A股市场情绪', [item.value for item in app.subheader])
        self.assertIn('长线指标：宏观经济与估值', [item.value for item in app.subheader])

    def test_visit_is_counted_once_per_session_beside_market_db(self):
        """访问统计写入与行情库同目录的独立文件；会话内重跑不重复计数。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            os.environ['MACROBOARD_DB'] = str(path)
            os.environ.pop('MACROBOARD_VISIT_DB', None)
            os.environ.pop('MACROBOARD_VISIT_LOG', None)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
                app.run()  # 同一会话的第二次重跑（等价于一次控件交互）
            finally:
                os.environ.pop('MACROBOARD_DB', None)
            visit_db = Path(tmp) / 'visits.sqlite3'
            exists = visit_db.exists()
            conn = sqlite3.connect(visit_db)
            try:
                visits_count, stored_ip = conn.execute(
                    'SELECT COUNT(*), MAX(ip) FROM visits'
                ).fetchone()
            finally:
                conn.close()

        self.assertTrue(exists)
        self.assertEqual(visits_count, 1)
        # AppTest 里没有真实连接来源 IP（st.context.ip_address 为 None），落库应为 NULL
        self.assertIsNone(stored_ip)
        self.assertEqual([str(item.value) for item in app.exception], [])
        markdown = ' '.join(item.value for item in app.markdown)
        self.assertIn('访问统计', markdown)

    def test_visit_logging_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            os.environ['MACROBOARD_DB'] = str(path)
            os.environ['MACROBOARD_VISIT_LOG'] = '0'
            os.environ.pop('MACROBOARD_VISIT_DB', None)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                os.environ.pop('MACROBOARD_DB', None)
                os.environ.pop('MACROBOARD_VISIT_LOG', None)
            visit_db_exists = (Path(tmp) / 'visits.sqlite3').exists()

        self.assertFalse(visit_db_exists)
        self.assertEqual([str(item.value) for item in app.exception], [])
        captions = ' '.join(item.value for item in app.caption)
        self.assertIn('访问统计：已关闭', captions)

    def test_page_survives_unwritable_visit_store(self):
        """统计库不可写时页面照常渲染，并明确标注本次访问未计入。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            blocked = Path(tmp) / 'blocked.sqlite3'
            blocked.mkdir()  # 目录占位：sqlite 无法把它当数据库打开
            os.environ['MACROBOARD_DB'] = str(path)
            os.environ['MACROBOARD_VISIT_DB'] = str(blocked)
            os.environ.pop('MACROBOARD_VISIT_LOG', None)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                os.environ.pop('MACROBOARD_DB', None)
                os.environ.pop('MACROBOARD_VISIT_DB', None)

        self.assertEqual([str(item.value) for item in app.exception], [])
        captions = ' '.join(item.value for item in app.caption)
        self.assertIn('本次访问没有计入统计', captions)
        self.assertIn('访问统计：未接通', captions)


def _drop_last_day_metric(path: Path, metric: str) -> None:
    """删掉某指标最后一个交易日的观测，模拟"该源当日尚未发布"（两融 T+1）。"""
    conn = db.connect(path)
    last = conn.execute(
        'select max(obs_date) from observations where metric = ?', (metric,)
    ).fetchone()[0]
    conn.execute('delete from observations where metric = ? and obs_date = ?', (metric, last))
    conn.commit()
    conn.close()


def _strip_last_day(path: Path, keep: str = 'limit_down_count') -> None:
    """删掉最后一个交易日除 keep 外的全部观测，复现「最新一行残缺」。

    真实成因：定时采集早于数据源发布（15:01 采集当天），源站当时只给出部分字段，
    库里先落下一行只有少数序列有值的最新记录，其余序列的最新日期仍停在上一日。
    """
    conn = db.connect(path)
    last = conn.execute('select max(obs_date) from observations').fetchone()[0]
    conn.execute('delete from observations where obs_date = ? and metric != ?', (last, keep))
    conn.commit()
    conn.close()


class PartialLatestDayTests(unittest.TestCase):
    """最新交易日残缺时的降级表现（回归：整页 TypeError，长线区块一并消失）。"""

    def test_margin_absence_degrades_to_volume_ratio_without_alarm(self):
        """两融 T+1 未公布是每日常态：参与度 = 量比得分（权重 100%），不弹「读数不可用」。

        当日缺两融不算异常——覆盖率由卡片上的「可用分量覆盖率」体现，
        不该报警告（报警告会让人以为整个读数都取不到）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            _drop_last_day_metric(path, 'margin_turnover')
            previous = os.environ.get('MACROBOARD_DB')
            os.environ['MACROBOARD_DB'] = str(path)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                if previous is None:
                    os.environ.pop('MACROBOARD_DB', None)
                else:
                    os.environ['MACROBOARD_DB'] = previous

        self.assertEqual([str(item.value) for item in app.exception], [])
        values = {item.label: item.value for item in app.metric}
        self.assertNotEqual(values['参与度指数'], '不可用')
        # 权重全部落在量比得分上，不拿前一日两融补位
        self.assertEqual(values['参与度指数'], values['量比得分'])
        warnings = ' '.join(item.value for item in app.warning)
        self.assertNotIn('读数不可用', warnings)

    def test_partial_latest_day_degrades_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            _strip_last_day(path)
            previous = os.environ.get('MACROBOARD_DB')
            os.environ['MACROBOARD_DB'] = str(path)
            try:
                app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
                app.run()
            finally:
                if previous is None:
                    os.environ.pop('MACROBOARD_DB', None)
                else:
                    os.environ['MACROBOARD_DB'] = previous

        self.assertEqual([str(item.value) for item in app.exception], [])
        # 指数读数按不可用显示，并说明是哪一天、缺了什么
        values = {item.label: item.value for item in app.metric}
        self.assertEqual(values['方向指数'], '不可用')
        self.assertEqual(values['综合情绪指数'], '不可用')
        warnings = ' '.join(item.value for item in app.warning)
        self.assertIn('读数不可用', warnings)
        # 长线区块不依赖情绪读数，照常渲染
        subheaders = [item.value for item in app.subheader]
        self.assertIn('长线指标：宏观经济与估值', subheaders)


class SentimentCurveLayoutTests(unittest.TestCase):
    """四条曲线（上证指数 + 三条指数）同列、共用一条时间轴：移动一次光标就能同时读到四张图的值。"""

    def _run(self, path: Path):
        previous = os.environ.get('MACROBOARD_DB')
        os.environ['MACROBOARD_DB'] = str(path)
        try:
            app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=180)
            app.run()
        finally:
            if previous is None:
                os.environ.pop('MACROBOARD_DB', None)
            else:
                os.environ['MACROBOARD_DB'] = previous
        return app

    def test_three_indices_share_one_stacked_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            app = self._run(path)
        self.assertEqual([str(item.value) for item in app.exception], [])
        specs = [json.loads(item.proto.spec) for item in app.get('plotly_chart')]
        stacked = [spec for spec in specs if spec['layout'].get('hoversubplots') == 'axis']
        # 只有一张图跨子图悬停：四条曲线都在它里面（上证指数在最上，不再是并排的小图）
        self.assertEqual(len(stacked), 1)
        spec = stacked[0]
        self.assertEqual(
            [trace['name'] for trace in spec['data']],
            ['上证指数', '综合情绪指数', '参与度指数', '方向指数'],
        )
        self.assertEqual([trace['yaxis'] for trace in spec['data']], ['y', 'y2', 'y3', 'y4'])
        # 四张图共用**同一个** x 轴对象（没有 xaxis2/xaxis3/xaxis4）：悬停竖线纵穿四图的前提
        self.assertEqual([trace['xaxis'] for trace in spec['data']], ['x', 'x', 'x', 'x'])
        for extra in ('xaxis2', 'xaxis3', 'xaxis4'):
            with self.subTest(axis=extra):
                self.assertNotIn(extra, spec['layout'])
        self.assertEqual(spec['layout']['xaxis']['anchor'], 'y4')
        # 统一的 x 悬停 + 贯穿整条轴的竖线：移动一次光标就能同时读到四条曲线的值
        self.assertEqual(spec['layout']['hovermode'], 'x unified')
        self.assertEqual(spec['layout']['xaxis']['spikemode'], 'across')
        self.assertTrue(spec['layout']['xaxis']['showspikes'])
        titles = [item.get('text') for item in spec['layout'].get('annotations', [])]
        for title in ('上证指数', '综合情绪指数', '参与度指数', '方向指数'):
            with self.subTest(title=title):
                self.assertIn(title, titles)
        # 每张图一条自己的曲线，不把几条曲线叠进同一个子图
        for trace in spec['data']:
            with self.subTest(trace=trace['name']):
                self.assertGreater(len(trace['x']), 0)
                self.assertEqual(trace['mode'], 'lines')
        # 分数曲线纵轴固定 0—100；上证指数是指数点位，不套这个范围
        self.assertEqual(spec['layout']['yaxis2']['range'], [0.0, 100.0])
        self.assertNotIn('range', spec['layout']['yaxis'])

    def test_all_panels_get_high_low_background_bands(self):
        """四张图都铺价格位置底色：高位红、低位绿，且各只铺在自己那张图的高度范围内。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            app = self._run(path)
        self.assertEqual([str(item.value) for item in app.exception], [])
        spec = next(
            json.loads(item.proto.spec)
            for item in app.get('plotly_chart')
            if json.loads(item.proto.spec)['layout'].get('hoversubplots') == 'axis'
        )
        layout = spec['layout']
        rects = [shape for shape in layout.get('shapes', []) if shape['type'] == 'rect']
        self.assertTrue(rects)
        self.assertEqual({rect['fillcolor'] for rect in rects}, set(charts.POSITION_BAND_FILL.values()))
        domains = {
            axis: layout[axis]['domain'] for axis in ('yaxis', 'yaxis2', 'yaxis3', 'yaxis4')
        }
        # 每张图都有自己的底色带，且底色不越界到别的图
        drawn = {axis: 0 for axis in domains}
        for rect in rects:
            with self.subTest(fill=rect['fillcolor'], y0=rect['y0']):
                self.assertEqual(rect['yref'], 'paper')
                self.assertEqual(rect['layer'], 'below')
                owners = [axis for axis, domain in domains.items() if (rect['y0'], rect['y1']) == tuple(domain)]
                self.assertEqual(len(owners), 1)
                drawn[owners[0]] += 1
        for axis, count in drawn.items():
            with self.subTest(axis=axis):
                self.assertGreater(count, 0, '每张图都应有底色带')
        captions = ' '.join(item.value for item in app.caption)
        self.assertIn('红底＝高位', captions)
        self.assertIn('绿底＝低位', captions)
        self.assertIn('无底色＝位置未知', captions)
        self.assertIn('不是买卖提示', captions)

    def test_caption_states_shared_axis_and_unified_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'app.sqlite3'
            _seed(path)
            app = self._run(path)
        self.assertEqual([str(item.value) for item in app.exception], [])
        captions = ' '.join(item.value for item in app.caption)
        self.assertIn('4 张图同列', captions)
        self.assertIn('一条竖线纵穿 4 张图', captions)
        self.assertIn('同时给出 4 条曲线在该日期的取值', captions)
        self.assertIn('一个视图内统一分辨率', captions)
        for title in ('上证指数', '综合情绪指数', '参与度指数', '方向指数'):
            with self.subTest(title=title):
                self.assertIn(title, captions)
        self.assertIn('起点', captions)


if __name__ == '__main__':
    unittest.main()
