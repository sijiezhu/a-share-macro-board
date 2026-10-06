"""情绪模块 P1：算法层中性码值与展示层文案的分层契约。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import pandas as pd

from macroboard.sentiment import codes, labels

MODULE_DIR = Path(labels.__file__).resolve().parent
CJK_PATTERN = re.compile(r'[\u4e00-\u9fff]')
FORBIDDEN_WORDS = ('买入', '卖出', '建议', '目标价', '仓位', '上涨概率', '必涨', '抄底')


class CodesLayeringTests(unittest.TestCase):
    def test_codes_module_contains_no_cjk(self):
        source = (MODULE_DIR / 'codes.py').read_text(encoding='utf-8')
        self.assertIsNone(CJK_PATTERN.search(source), 'codes.py 不得出现中文文案')

    def test_codes_are_ascii_identifiers(self):
        for name in codes.STATE_CODES + codes.BAND_CODES + codes.MACD_VOL_STATES:
            with self.subTest(code=name):
                self.assertRegex(name, r'^[A-Z][A-Z0-9_]*$')

    def test_code_groups_are_unique(self):
        for group in (
            codes.STATE_CODES,
            codes.BAND_CODES,
            codes.PANIC_CONDITION_CODES,
            codes.REV_CONDITION_CODES,
            codes.REV_BLOCK_CODES,
            codes.MACD_VOL_STATES,
            codes.DATA_QUALITY_CODES,
            codes.COLUMN_STATUSES,
        ):
            with self.subTest(group=group[:2]):
                self.assertEqual(len(set(group)), len(group))


class LabelCoverageTests(unittest.TestCase):
    def test_every_code_has_a_label(self):
        expectations = (
            (codes.STATE_CODES, labels.STATE_LABELS),
            (codes.BAND_CODES, labels.BAND_LABELS),
            (codes.PANIC_CONDITION_CODES, labels.PANIC_REASON_LABELS),
            (codes.REV_CONDITION_CODES, labels.REV_REASON_LABELS),
            (codes.REV_BLOCK_CODES, labels.REV_BLOCK_REASON_LABELS),
            (codes.MACD_VOL_STATES, labels.MACD_VOL_STATE_LABELS),
            (codes.DATA_QUALITY_CODES, labels.DATA_QUALITY_LABELS),
            (codes.COLUMN_STATUSES, labels.COLUMN_STATUS_LABELS),
        )
        for group, mapping in expectations:
            for code in group:
                with self.subTest(code=code):
                    self.assertIn(code, mapping)
                    self.assertTrue(mapping[code].strip())

    def test_labels_have_no_extra_keys(self):
        self.assertEqual(set(labels.STATE_LABELS), set(codes.STATE_CODES))
        self.assertEqual(set(labels.MACD_VOL_STATE_LABELS), set(codes.MACD_VOL_STATES))
        self.assertEqual(set(labels.POSITION_LABELS), set(codes.POSITION_CODES))

    def test_state_labels_follow_review_wording(self):
        self.assertEqual(labels.STATE_LABELS[codes.STATE_PANIC], '恐慌抛售')
        self.assertEqual(labels.STATE_LABELS[codes.STATE_COLD], '缩量阴跌')

    def test_low_cold_is_ice_but_not_panic(self):
        """低位缩量阴跌可以叫冰点，但不能挪用"恐慌抛售"那句（那是放量急跌型）。"""
        self.assertIn('冰点', labels.STATE_LABELS[codes.STATE_COLD_LOW])
        self.assertNotIn('恐慌', labels.STATE_LABELS[codes.STATE_COLD_LOW])

    def test_high_and_low_state_wording_is_distinct(self):
        """同一象限在高位/低位必须给不同文案（这是本次改动的核心诉求）。"""
        for base, codes_in_position in (
            ('GREED', (codes.STATE_GREED_HIGH, codes.STATE_GREED_LOW)),
            ('PANIC', (codes.STATE_PANIC_HIGH, codes.STATE_PANIC_LOW)),
            ('COLD', (codes.STATE_COLD_HIGH, codes.STATE_COLD_LOW)),
            ('THAW', (codes.STATE_THAW_HIGH, codes.STATE_THAW_LOW)),
        ):
            with self.subTest(quadrant=base):
                high, low = (labels.STATE_LABELS[code] for code in codes_in_position)
                self.assertNotEqual(high, low)
                self.assertTrue(high.startswith('高位'))
                self.assertTrue(low.startswith('低位'))
        # 高位放量急跌要说"趋势终止预警"，低位放量急跌要说"恐慌/冰点"
        self.assertIn('终止预警', labels.STATE_LABELS[codes.STATE_PANIC_HIGH])
        self.assertIn('恐慌', labels.STATE_LABELS[codes.STATE_PANIC_LOW])

    def test_position_texts_are_distinct(self):
        self.assertEqual(
            [labels.POSITION_LABELS[code] for code in codes.POSITION_CODES],
            ['高位', '低位', '位置未知'],
        )
        self.assertEqual(labels.describe_position(codes.POS_HIGH), '高位')
        with self.assertRaises(ValueError):
            labels.describe_position('NOT_A_CODE')

    def test_labels_contain_no_investment_advice(self):
        for mapping in (
            labels.STATE_LABELS,
            labels.POSITION_LABELS,
            labels.PANIC_REASON_LABELS,
            labels.REV_REASON_LABELS,
            labels.REV_BLOCK_REASON_LABELS,
            labels.MACD_VOL_STATE_LABELS,
        ):
            for code, text in mapping.items():
                for word in FORBIDDEN_WORDS:
                    with self.subTest(code=code, word=word):
                        self.assertNotIn(word, text)


class DescribeHelpersTests(unittest.TestCase):
    def test_describe_state_returns_chinese(self):
        text = labels.describe_state(codes.STATE_GREED)
        self.assertEqual(text, '贪婪/主升')

    def test_describe_state_rejects_unknown_code(self):
        with self.assertRaises(ValueError):
            labels.describe_state('STATE_NOT_A_REAL_CODE')

    def test_label_series_maps_and_keeps_missing(self):
        series = pd.Series([codes.STATE_PANIC, None, codes.STATE_COLD], dtype='object')
        out = labels.label_series(series, labels.STATE_LABELS)
        self.assertEqual(out.iloc[0], '恐慌抛售')
        self.assertTrue(pd.isna(out.iloc[1]))
        self.assertEqual(out.iloc[2], '缩量阴跌')

    def test_label_series_rejects_unknown_code(self):
        series = pd.Series(['STATE_NOPE'], dtype='object')
        with self.assertRaises(ValueError):
            labels.label_series(series, labels.STATE_LABELS)

    def test_label_history_renders_reasons(self):
        text = labels.label_history((codes.PANIC_RETURN_20D_LOW, codes.PANIC_VOLUME_RATIO_HIGH),
                                   labels.PANIC_REASON_LABELS)
        self.assertIn('20 日收益', text)
        self.assertIn('；', text)
        self.assertEqual(labels.label_history((), labels.PANIC_REASON_LABELS), '无')


if __name__ == '__main__':
    unittest.main()
