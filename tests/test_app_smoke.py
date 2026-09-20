"""页面整页冒烟：指向临时库，确认无异常且情绪区块已接线。

用 Streamlit 的 AppTest 在进程内跑 app.py，不启动服务器、不触网。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

from macroboard import db

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
        'hs300_pe_ttm': [(day, 12.0 + index * 0.001) for index, day in enumerate(days)],
        'amount': [(day, 2.0e12 - index * 1e9) for index, day in enumerate(days)],
        'volume': [(day, 3.0e10 + index * 1e7) for index, day in enumerate(days)],
        'turnover_rate': [(day, 1.5 + (index % 20) * 0.01) for index, day in enumerate(days)],
        'margin_net_buy': [(day, 1e8 * (1 if index % 2 else -1)) for index, day in enumerate(days)],
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


if __name__ == '__main__':
    unittest.main()
