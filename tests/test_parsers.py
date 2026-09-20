"""各数据源解析测试：全部使用固定样本，不触网。"""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from macroboard.sources import cn_bond, fx_usdcny, gold_lbma, mx, us_treasury


class TreasuryTests(unittest.TestCase):
    CSV = (
        'Date,"1 Mo","2 Yr","10 Yr","30 Yr"\n'
        '09/16/2026,3.97,4.74,5.01,5.34\n'
        '09/17/2026,3.98,4.67,4.94,5.29\n'
        '09/18/2026,3.99,4.76,5.00,5.30\n'
    )

    def test_parse_csv_picks_10_year_column(self):
        rows = us_treasury.parse_csv(self.CSV)
        self.assertEqual(rows[0], (date(2026, 9, 16), 5.01))
        self.assertEqual(rows[-1], (date(2026, 9, 18), 5.00))

    def test_parse_csv_rejects_missing_column(self):
        with self.assertRaises(us_treasury.SourceError):
            us_treasury.parse_csv('Date,"2 Yr"\n09/18/2026,4.76\n')

    def test_parse_csv_skips_blank_values(self):
        rows = us_treasury.parse_csv('Date,"10 Yr"\n09/18/2026,\n09/17/2026,4.94\n')
        self.assertEqual(len(rows), 1)


class CnBondTests(unittest.TestCase):
    PAYLOAD = {
        'success': True,
        'result': {
            'pages': 1,
            'data': [
                {'SOLAR_DATE': '2026-09-18 00:00:00', 'EMM00166466': 1.682},
                {'SOLAR_DATE': '2024-01-15 00:00:00', 'EMM00166466': 2.5212},
                {'SOLAR_DATE': '2026-09-17 00:00:00', 'EMM00166466': None},
            ],
        },
    }

    def test_parse_payload_returns_sorted_valid_rows(self):
        rows = cn_bond.parse_payload(self.PAYLOAD)
        self.assertEqual(rows, [(date(2024, 1, 15), 2.5212), (date(2026, 9, 18), 1.682)])


class FxTests(unittest.TestCase):
    TEXT = (
        '/*<script>location.href=\'//sina.com\';</script>*/\n'
        'var _d=("2026-09-17,6.7074,6.6951,6.7150,6.7101,|'
        '2026-09-18,6.7067,6.6856,6.7074,6.6984,");'
    )

    def test_parse_daykline_uses_close_field(self):
        rows = fx_usdcny.parse_daykline(self.TEXT)
        self.assertEqual(rows[-1], (date(2026, 9, 18), 6.6984))

    def test_parse_daykline_rejects_other_payloads(self):
        with self.assertRaises(fx_usdcny.SourceError):
            fx_usdcny.parse_daykline('{"__ERROR":3}')


class GoldTests(unittest.TestCase):
    PAYLOAD = [
        {'is_cms_locked': 0, 'd': '2026-09-17', 'v': [4368.1, 3265.15, 3800.25]},
        {'is_cms_locked': 0, 'd': '2026-09-18', 'v': [4348.15, 3259.37, 3793.33]},
        {'is_cms_locked': 0, 'd': '2026-09-19', 'v': [None, None, None]},
    ]

    def test_parse_payload_uses_usd_leg(self):
        rows = gold_lbma.parse_payload(self.PAYLOAD)
        self.assertEqual(rows[-1], (date(2026, 9, 18), 4348.15))


class MXTests(unittest.TestCase):
    PAYLOAD = {
        'code': 0,
        'success': True,
        'data': {
            'data': {
                'searchDataResultDTO': {
                    'dataTableDTOList': [
                        {
                            'code': '001004',
                            'nameMap': {'329915': '上涨家数'},
                            'table': {
                                '329915': ['3,937', '2,504'],
                                'headName': ['2026-09-18(日)', '2026-09-17(日)'],
                            },
                            'rawTable': {
                                '329915': ['3937', '2504'],
                                'headName': ['2026-09-18(日)', '2026-09-17(日)'],
                            },
                        }
                    ]
                }
            }
        },
    }

    def test_extract_series_from_raw_table(self):
        rows = mx.extract_series(self.PAYLOAD)
        self.assertEqual(rows, [(date(2026, 9, 17), 2504.0), (date(2026, 9, 18), 3937.0)])

    def test_parse_number_handles_units(self):
        self.assertAlmostEqual(mx.parse_number('14.23倍'), 14.23)
        self.assertAlmostEqual(mx.parse_number('4,507.39点'), 4507.39)
        self.assertIsNone(mx.parse_number('--'))

    def test_parse_date_from_head_name(self):
        self.assertEqual(mx.parse_date('2026-09-18(日)'), date(2026, 9, 18))
        self.assertIsNone(mx.parse_date(None))

    def test_extract_series_raises_without_tables(self):
        with self.assertRaises(mx.SourceError):
            mx.extract_series({'code': 0, 'data': {'data': {'searchDataResultDTO': {}}}})

    def test_query_templates_use_natural_year(self):
        for template in (
            mx.QUERY_AMOUNT_YEAR,
            mx.QUERY_VOLUME_YEAR,
            mx.QUERY_TURNOVER_YEAR,
            mx.QUERY_LIMIT_UP_YEAR,
            mx.QUERY_LIMIT_DOWN_YEAR,
            mx.QUERY_MARGIN_BUY_YEAR,
            mx.QUERY_MARGIN_REPAY_YEAR,
        ):
            with self.subTest(template=template):
                text = template.format(year=2026)
                self.assertIn('2026年1月1日至2026年12月31日', text)
                self.assertIn('每个交易日', text)

    def test_margin_net_buy_requires_same_date(self):
        buy = [(date(2026, 9, 15), 300.0), (date(2026, 9, 16), 500.0), (date(2026, 9, 18), 900.0)]
        repay = [(date(2026, 9, 15), 100.0), (date(2026, 9, 17), 400.0), (date(2026, 9, 18), 950.0)]
        rows = mx.build_margin_net_buy(buy, repay)
        self.assertEqual(
            rows,
            [(date(2026, 9, 15), 200.0), (date(2026, 9, 18), -50.0)],
        )

    def test_margin_net_buy_sorts_and_allows_negative(self):
        buy = [(date(2026, 9, 17), 100.0), (date(2026, 9, 16), 100.0)]
        repay = [(date(2026, 9, 17), 250.0), (date(2026, 9, 16), 50.0)]
        rows = mx.build_margin_net_buy(buy, repay)
        self.assertEqual([day for day, _ in rows], [date(2026, 9, 16), date(2026, 9, 17)])
        self.assertAlmostEqual(rows[1][1], -150.0, places=9)

    def test_margin_net_buy_with_no_common_date_is_empty(self):
        rows = mx.build_margin_net_buy(
            [(date(2026, 9, 17), 100.0)], [(date(2026, 9, 16), 100.0)]
        )
        self.assertEqual(rows, [])

    def test_activity_fetch_marks_single_failure_without_killing_others(self):
        def fake_year_series(session, api_key, template, start, end):
            if template == mx.QUERY_VOLUME_YEAR:
                raise mx.SourceError('妙想未返回数据')
            return mx.FetchedSeries(rows=[(date(2026, 9, 18), 1.0)])

        with mock.patch.object(mx, 'fetch_year_series', side_effect=fake_year_series), mock.patch.object(
            mx, 'fetch_margin_net_buy', return_value=mx.FetchedSeries(rows=[(date(2026, 9, 18), -5.0)])
        ):
            out = mx.fetch_a_share_activity(
                session=object(),  # type: ignore[arg-type]
                api_key='test',
                start=date(2026, 1, 1),
                end=date(2026, 9, 18),
            )
        self.assertEqual(out['volume'].rows, [])
        self.assertTrue(out['volume'].warnings)
        self.assertEqual(out['amount'].rows, [(date(2026, 9, 18), 1.0)])
        self.assertEqual(out['margin_net_buy'].rows, [(date(2026, 9, 18), -5.0)])

    def test_activity_fetch_raises_when_everything_fails(self):
        def always_fail(*args, **kwargs):
            raise mx.SourceError('妙想未返回数据')

        with mock.patch.object(mx, 'fetch_year_series', side_effect=always_fail), mock.patch.object(
            mx, 'fetch_margin_net_buy', side_effect=always_fail
        ):
            with self.assertRaises(mx.SourceError):
                mx.fetch_a_share_activity(
                    session=object(),  # type: ignore[arg-type]
                    api_key='test',
                    start=date(2026, 1, 1),
                    end=date(2026, 9, 18),
                )


if __name__ == '__main__':
    unittest.main()
