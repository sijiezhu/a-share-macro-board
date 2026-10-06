"""曲线粒度测试：一个视图内统一分辨率、只保留真实观测、近密远疏可选。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from macroboard import charts
from macroboard.sentiment import codes

END = date(2026, 9, 18)


def daily_series(days: int) -> list[tuple[date, float]]:
    start = END - timedelta(days=days - 1)
    return [(start + timedelta(days=index), 100.0 + index) for index in range(days)]


class AutoResolutionTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(charts.auto_resolution(250), charts.RESOLUTION_DAILY)
        self.assertEqual(charts.auto_resolution(365), charts.RESOLUTION_DAILY)
        self.assertEqual(charts.auto_resolution(750), charts.RESOLUTION_WEEKLY)
        self.assertEqual(charts.auto_resolution(1095), charts.RESOLUTION_WEEKLY)
        self.assertEqual(charts.auto_resolution(1250), charts.RESOLUTION_MONTHLY)

    def test_effective_resolution_resolves_auto(self):
        one_year = daily_series(365)
        five_years = daily_series(1250)
        self.assertEqual(
            charts.effective_resolution(charts.RESOLUTION_AUTO, one_year), charts.RESOLUTION_DAILY
        )
        self.assertEqual(
            charts.effective_resolution(charts.RESOLUTION_AUTO, five_years), charts.RESOLUTION_MONTHLY
        )
        self.assertEqual(
            charts.effective_resolution(charts.RESOLUTION_WEEKLY, one_year), charts.RESOLUTION_WEEKLY
        )

    def test_span_days(self):
        self.assertEqual(charts.span_days(daily_series(10)), 9)
        self.assertEqual(charts.span_days([]), 0)
        self.assertEqual(charts.span_days([(END, 1.0)]), 0)


class ResampleTests(unittest.TestCase):
    def test_daily_and_raw_keep_every_observation(self):
        series = daily_series(300)
        self.assertEqual(charts.resample(series, charts.RESOLUTION_DAILY), series)
        self.assertEqual(charts.resample(series, charts.RESOLUTION_RAW), series)

    def test_weekly_and_monthly_reduce_with_real_values(self):
        series = daily_series(800)
        weekly = charts.resample(series, charts.RESOLUTION_WEEKLY)
        monthly = charts.resample(series, charts.RESOLUTION_MONTHLY)
        self.assertLess(len(weekly), len(series))
        self.assertLess(len(monthly), len(weekly))
        source = dict(series)
        for sampled in (weekly, monthly):
            days = [day for day, _ in sampled]
            self.assertEqual(days, sorted(days))
            self.assertEqual(len(days), len(set(days)))
            for day, value in sampled:
                self.assertEqual(value, source[day])

    def test_weekly_keeps_last_observation_of_each_week(self):
        series = [
            (date(2026, 9, 7), 1.0),  # 周一
            (date(2026, 9, 11), 2.0),  # 周五
            (date(2026, 9, 14), 3.0),  # 下周一
        ]
        self.assertEqual(
            charts.resample(series, charts.RESOLUTION_WEEKLY),
            [(date(2026, 9, 11), 2.0), (date(2026, 9, 14), 3.0)],
        )

    def test_tiered_keeps_recent_daily_and_thins_older(self):
        series = daily_series(1200)
        tiered = charts.resample(series, charts.RESOLUTION_TIERED, now=END)
        recent_input = [row for row in series if (END - row[0]).days <= charts.CHART_RECENT_FULL_DAYS]
        recent_sampled = [row for row in tiered if (END - row[0]).days <= charts.CHART_RECENT_FULL_DAYS]
        self.assertEqual(len(recent_sampled), len(recent_input))
        self.assertLess(len(tiered), len(series) / 3)
        self.assertGreater(len(tiered), 90)

    def test_unknown_resolution_raises(self):
        with self.assertRaises(ValueError):
            charts.resample(daily_series(10), '不存在的粒度')

    def test_empty_series(self):
        self.assertEqual(charts.resample([], charts.RESOLUTION_WEEKLY), [])

    def test_tiered_boundaries_positions(self):
        boundaries = charts.tiered_boundaries(END)
        self.assertEqual(len(boundaries), 3)
        self.assertEqual(boundaries[0][0], END - timedelta(days=charts.CHART_RECENT_FULL_DAYS))
        self.assertIn('日线→周线', boundaries[0][1])


class SliceTests(unittest.TestCase):
    def test_slice_last_year(self):
        series = daily_series(1000)
        sliced = charts.slice_range(series, 365, now=END)
        self.assertEqual(len(sliced), 366)
        self.assertEqual(sliced[-1], series[-1])

    def test_slice_all_when_days_none(self):
        series = daily_series(10)
        self.assertEqual(charts.slice_range(series, None, now=END), series)

    def test_slice_empty(self):
        self.assertEqual(charts.slice_range([], 365, now=END), [])


class FormatTests(unittest.TestCase):
    def test_latest_text_units(self):
        rate = charts.ChartSpec('us10y', '美国10年期国债收益率', '%', '#fff', 2)
        fx = charts.ChartSpec('usdcny', '汇率', 'CNY/USD', '#fff', 4)
        gold = charts.ChartSpec('xauusd', '黄金', 'USD/金衡盎司', '#fff', 2)
        self.assertEqual(charts.latest_text([(END, 5.01)], rate), '5.01% · 2026-09-18')
        self.assertEqual(charts.latest_text([(END, 6.6984)], fx), '6.6984 · 2026-09-18')
        self.assertIn('4,348.15', charts.latest_text([(END, 4348.15)], gold))
        self.assertEqual(charts.latest_text([], rate), '未接通')

    def test_chart_specs_cover_six_indicators(self):
        keys = [spec.key for spec in charts.CHART_SPECS]
        self.assertEqual(keys, ['us10y', 'cn10y', 'usdcny', 'xauusd', 'adv_ratio', 'hs300_20d'])

    def test_sentiment_specs_exist(self):
        keys = [spec.key for spec in charts.SENTIMENT_SPECS]
        self.assertEqual(keys, ['sentiment_index', 'participation_index', 'direction_index'])
        for spec in charts.SENTIMENT_SPECS:
            with self.subTest(spec=spec.key):
                self.assertEqual(spec.hline_values, (40.0, 60.0))
                self.assertEqual(spec.y_range, (0.0, 100.0))  # 0—100 的分数：纵轴固定

    def test_shanghai_index_leads_the_stacked_specs(self):
        """上证指数排在最上，且不套用分数曲线的阈值线与固定纵轴。"""
        keys = [spec.key for spec in charts.SENTIMENT_CHART_SPECS]
        self.assertEqual(
            keys, ['sh_close', 'sentiment_index', 'participation_index', 'direction_index']
        )
        index_spec = charts.SENTIMENT_CHART_SPECS[0]
        self.assertEqual(index_spec.unit, '点')
        self.assertEqual(index_spec.hline_values, ())
        self.assertIsNone(index_spec.y_range)  # 指数点位：纵轴按数据自适应


class QuadrantFigureTests(unittest.TestCase):
    """四象限图：四个象限底色 + 阈值线 + 最近 10 个交易日 + 箭头（过去 → 未来）。"""

    @staticmethod
    def _points(count: int = 20, position: str = codes.POS_HIGH) -> list[tuple[date, float, float, str]]:
        start = END - timedelta(days=count - 1)
        return [
            (start + timedelta(days=index), 20.0 + index * 3, 80.0 - index * 2, position)
            for index in range(count)
        ]

    @staticmethod
    def _dots(figure) -> list:
        """位置散点（排除「最新」高亮点）。"""
        return [trace for trace in figure.data if trace.name != '最新']

    def test_figure_has_quadrants_thresholds_and_path(self):
        points = self._points(20)
        figure = charts.build_quadrant_figure(points)
        # 4 个象限矩形 + 2 条阈值线
        self.assertGreaterEqual(len(figure.layout.shapes), 6)
        # 三条位置散点（高位 / 低位 / 位置未知，空组也保留以稳定图例）+ 最新点
        self.assertEqual(len(figure.data), 4)
        self.assertEqual(sum(len(trace.x) for trace in self._dots(figure)), 10)  # 只保留最近 10 天
        for trace in self._dots(figure):
            self.assertEqual(trace.mode, 'markers')
        self.assertEqual(list(figure.layout.xaxis.range), [0, 100])
        self.assertEqual(list(figure.layout.yaxis.range), [0, 100])

    def test_arrows_connect_consecutive_days_from_past_to_future(self):
        points = self._points(20)
        figure = charts.build_quadrant_figure(points)
        arrows = [item for item in figure.layout.annotations if item.showarrow]
        self.assertEqual(len(arrows), 9)  # 10 个点 -> 9 段箭头
        recent = points[-10:]
        for index, arrow in enumerate(arrows):
            older, newer = recent[index], recent[index + 1]
            with self.subTest(segment=index):
                # 箭头终点 = 较新的点，起点 = 较旧的点
                self.assertAlmostEqual(float(arrow.x), newer[1], places=9)
                self.assertAlmostEqual(float(arrow.y), newer[2], places=9)
                self.assertAlmostEqual(float(arrow.ax), older[1], places=9)
                self.assertAlmostEqual(float(arrow.ay), older[2], places=9)

    def test_labels_mark_oldest_and_latest(self):
        points = self._points(12)
        figure = charts.build_quadrant_figure(points)
        texts = [str(item.text) for item in figure.layout.annotations if item.text]
        self.assertTrue(any('最新' in text for text in texts))
        self.assertTrue(any('9 个交易日前' in text for text in texts))

    def test_quadrant_labels_stay_inside_the_axis(self):
        """四条象限文案必须画在 0—100 的轴范围内（回归：三条落在坐标 154，图上不可见）。"""
        figure = charts.build_quadrant_figure(self._points(10))
        texts = {text for _x0, _y0, text in charts.QUADRANT_LABELS}
        found = {str(item.text): item for item in figure.layout.annotations if str(item.text) in texts}
        self.assertEqual(set(found), texts)
        low, high = charts.QUADRANT_AXIS
        for x0, y0, text in charts.QUADRANT_LABELS:
            item = found[text]
            with self.subTest(text=text):
                x, y = float(item.x), float(item.y)
                self.assertLessEqual(low, x)
                self.assertLessEqual(x, high)
                self.assertLessEqual(low, y)
                self.assertLessEqual(y, high)
                # 贴在自己那一格的外侧角，且 anchor 让文字朝象限内侧展开
                self.assertEqual(x >= 50, x0 >= 50)
                self.assertEqual(y >= 50, y0 >= 50)
                self.assertEqual(item.xanchor, 'right' if x0 >= 50 else 'left')
                self.assertEqual(item.yanchor, 'top' if y0 >= 50 else 'bottom')

    def test_max_points_is_configurable(self):
        points = self._points(30)
        figure = charts.build_quadrant_figure(points, max_points=3)
        self.assertEqual(sum(len(trace.x) for trace in self._dots(figure)), 3)
        arrows = [item for item in figure.layout.annotations if item.showarrow]
        self.assertEqual(len(arrows), 2)

    def test_single_point_has_no_arrow(self):
        figure = charts.build_quadrant_figure(self._points(1))
        arrows = [item for item in figure.layout.annotations if item.showarrow]
        self.assertEqual(arrows, [])
        self.assertEqual(sum(len(trace.x) for trace in self._dots(figure)), 1)

    def test_empty_points_still_renders(self):
        figure = charts.build_quadrant_figure([])
        self.assertEqual(len(figure.data), 0)
        self.assertGreaterEqual(len(figure.layout.shapes), 6)

    def test_latest_point_is_highlighted(self):
        points = self._points(5)
        figure = charts.build_quadrant_figure(points)
        latest = next(trace for trace in figure.data if trace.name == '最新')
        self.assertEqual(list(latest.x), [points[-1][1]])
        self.assertEqual(list(latest.y), [points[-1][2]])

    def test_high_and_low_use_filled_and_open_markers_with_legend(self):
        points = []
        for index, position in enumerate(
            (codes.POS_HIGH, codes.POS_LOW) * 3
        ):
            points.append(
                (
                    END - timedelta(days=5 - index),
                    20.0 + index * 3,
                    80.0 - index * 2,
                    position,
                )
            )
        figure = charts.build_quadrant_figure(points)
        by_symbol = {trace.marker.symbol: trace for trace in self._dots(figure)}
        self.assertIn('circle', by_symbol)  # 高位：实心
        self.assertIn('circle-open', by_symbol)  # 低位：空心
        self.assertTrue(by_symbol['circle'].name.startswith('高位'))
        self.assertTrue(by_symbol['circle-open'].name.startswith('低位'))
        self.assertEqual(len(by_symbol['circle'].x), 3)
        self.assertEqual(len(by_symbol['circle-open'].x), 3)
        self.assertTrue(figure.layout.showlegend)

    def test_unknown_position_gets_its_own_marker(self):
        points = self._points(4, position=codes.POS_UNKNOWN)
        figure = charts.build_quadrant_figure(points)
        unknown = next(trace for trace in self._dots(figure) if 'diamond-open' == trace.marker.symbol)
        self.assertTrue(unknown.name.startswith('位置未知'))
        self.assertEqual(len(unknown.x), 4)
        # 位置未知的点照样画，不被丢弃
        self.assertEqual(sum(len(trace.x) for trace in self._dots(figure)), 4)

    def test_three_tuple_points_fall_back_to_unknown_position(self):
        """老调用方传三元组时不应报错，一律按位置未知绘制。"""
        legacy = [(END - timedelta(days=1), 50.0, 50.0)]
        figure = charts.build_quadrant_figure(legacy)
        unknown = next(trace for trace in self._dots(figure) if 'diamond-open' == trace.marker.symbol)
        self.assertEqual(len(unknown.x), 1)


class UnifiedResolutionTests(unittest.TestCase):
    """同列多图共用一条时间轴，只能有一种分辨率。"""

    def test_auto_uses_the_longest_series(self):
        short = daily_series(365)
        long = daily_series(1250)
        self.assertEqual(
            charts.unified_resolution(charts.RESOLUTION_AUTO, [short, long]),
            charts.RESOLUTION_MONTHLY,
        )
        self.assertEqual(
            charts.unified_resolution(charts.RESOLUTION_AUTO, [long, short]),
            charts.RESOLUTION_MONTHLY,
        )

    def test_short_series_keep_the_fine_resolution(self):
        self.assertEqual(
            charts.unified_resolution(charts.RESOLUTION_AUTO, [daily_series(365), daily_series(200)]),
            charts.RESOLUTION_DAILY,
        )

    def test_explicit_option_passes_through(self):
        series = [daily_series(365), daily_series(30)]
        self.assertEqual(
            charts.unified_resolution(charts.RESOLUTION_RAW, series), charts.RESOLUTION_RAW
        )
        self.assertEqual(
            charts.unified_resolution(charts.RESOLUTION_TIERED, series), charts.RESOLUTION_TIERED
        )

    def test_empty_series_fall_back_to_daily(self):
        self.assertEqual(
            charts.unified_resolution(charts.RESOLUTION_AUTO, [[], []]), charts.RESOLUTION_DAILY
        )


class StackedFigureTests(unittest.TestCase):
    """多张图同列：共用同一个 x 轴，悬停竖线纵穿所有图、读数合并在一个提示框里。"""

    @staticmethod
    def _panels(count: int = 3, points: int = 30):
        """默认取情绪区块真实使用的四条曲线（上证指数在最上）。"""
        return [
            (charts.SENTIMENT_CHART_SPECS[index], daily_series(points), None)
            for index in range(count)
        ]

    def test_hover_is_shared_across_the_stacked_panels(self):
        figure = charts.build_stacked_figure(self._panels())
        self.assertEqual(figure.layout.hovermode, 'x unified')
        # 悬停跨图靠 plotly 的 hoversubplots='axis'（只对 x / x unified 生效）
        self.assertEqual(figure.layout.hoversubplots, 'axis')

    def test_all_panels_share_one_x_axis_object(self):
        """四张图共用同一个 x 轴（四个 y 轴 anchor 到它），不是四个 matches 的轴。

        悬停竖线的高度取 x 轴的「反向轴域」＝挂在该轴上的所有 y 轴域之并集；
        共用同一个 x 轴才能让一条线纵穿所有图（用 matches 只会画在光标所在那张图）。
        """
        figure = charts.build_stacked_figure(self._panels(count=4))
        self.assertEqual(len(figure.data), 4)
        self.assertEqual([trace.xaxis for trace in figure.data], ['x'] * 4)
        self.assertEqual([trace.yaxis for trace in figure.data], ['y', 'y2', 'y3', 'y4'])
        self.assertEqual(figure.layout.xaxis.anchor, 'y4')  # x 轴画在最下面那张图的底部
        self.assertEqual(figure.layout.xaxis.domain, (0.0, 1.0))
        # 没有第二个 x 轴：所有图共用一个
        for extra in ('xaxis2', 'xaxis3', 'xaxis4'):
            with self.subTest(axis=extra):
                self.assertNotIn(extra, figure.layout)
        axes = ('yaxis', 'yaxis2', 'yaxis3', 'yaxis4')
        for axis in axes:
            with self.subTest(axis=axis):
                self.assertEqual(figure.layout[axis].anchor, 'x')
        # 分数固定 0—100；上证指数是指数点位，纵轴自适应
        for axis in axes[1:]:
            with self.subTest(axis=axis):
                self.assertEqual(list(figure.layout[axis].range), [0.0, 100.0])
        self.assertIsNone(figure.layout.yaxis.range)
        # 各图上下排开、互不重叠（等高，间距相等）
        domains = [figure.layout[axis].domain for axis in axes]
        gap = domains[0][0] - domains[1][1]
        self.assertGreater(gap, 0.0)
        self.assertAlmostEqual(domains[1][0] - domains[2][1], gap, places=9)
        self.assertAlmostEqual(domains[2][0] - domains[3][1], gap, places=9)
        for higher, lower in zip(domains, domains[1:], strict=False):
            with self.subTest(pair=(higher, lower)):
                self.assertAlmostEqual(higher[1] - higher[0], lower[1] - lower[0], places=9)
                self.assertGreater(higher[0], lower[1])

    def test_vertical_spike_line_spans_the_whole_shared_axis(self):
        figure = charts.build_stacked_figure(self._panels(count=4))
        axis = figure.layout.xaxis
        self.assertTrue(axis.showspikes)
        self.assertEqual(axis.spikemode, 'across')  # 线高取整条轴的反向轴域
        self.assertEqual(axis.spikesnap, 'cursor')  # 停在同一像素，各图不错开
        self.assertEqual(axis.spikethickness, 1)

    def test_each_panel_has_a_horizontal_spike_line(self):
        """横线逐图配置：`across` 时长度取该 y 轴的反向轴域＝这张图的整幅宽度。"""
        figure = charts.build_stacked_figure(self._panels(count=4))
        for axis_name in ('yaxis', 'yaxis2', 'yaxis3', 'yaxis4'):
            with self.subTest(axis=axis_name):
                axis = figure.layout[axis_name]
                self.assertTrue(axis.showspikes)
                self.assertEqual(axis.spikemode, 'across')
                self.assertEqual(axis.spikesnap, 'cursor')  # 跟着光标，不吸附到数据点
                self.assertEqual(axis.spikethickness, 1)

    def test_titles_and_threshold_lines_are_per_panel(self):
        figure = charts.build_stacked_figure(self._panels(count=4))
        titles = [str(item.text) for item in figure.layout.annotations]
        for spec in charts.SENTIMENT_CHART_SPECS:
            with self.subTest(spec=spec.key):
                self.assertIn(spec.title, titles)
        # 三条分数曲线各画 40 / 60 两条阈值线，落在自己的 y 轴上；上证指数不带阈值线
        hlines = [shape for shape in figure.layout.shapes if shape.y0 == shape.y1 and shape.type == 'line']
        self.assertEqual(len(hlines), 6)
        self.assertEqual({shape.yref for shape in hlines}, {'y2', 'y3', 'y4'})

    def test_empty_panel_is_labelled_in_place(self):
        panels = self._panels(count=4)
        panels[1] = (panels[1][0], [], None)
        figure = charts.build_stacked_figure(panels)
        self.assertEqual(len(figure.data), 3)
        texts = [str(item.text) for item in figure.layout.annotations]
        self.assertEqual(texts.count('样本不足'), 1)
        # 空面板只在原地标注，不做插值、不用邻图数据补齐
        labelled = next(item for item in figure.layout.annotations if item.text == '样本不足')
        domain = figure.layout.yaxis2.domain
        self.assertAlmostEqual(float(labelled.y), sum(domain) / 2, places=9)

    def test_boundaries_span_the_shared_axis_once(self):
        """近密远疏的切换位置是整条轴上的位置：一条竖线纵穿所有图，标签只标一次。"""
        panels = self._panels(points=1200)
        boundaries = charts.tiered_boundaries(END)
        stacked = [(spec, sampled, boundaries) for spec, sampled, _old in panels]
        figure = charts.build_stacked_figure(stacked)
        verticals = [shape for shape in figure.layout.shapes if shape.x0 == shape.x1]
        self.assertEqual(len(verticals), len(boundaries))
        for shape in verticals:
            with self.subTest(x=shape.x0):
                self.assertEqual(shape.xref, 'x')
                self.assertEqual(shape.yref, 'paper')
                self.assertEqual((float(shape.y0), float(shape.y1)), (0.0, 1.0))
        labels = [item for item in figure.layout.annotations if '→' in str(item.text)]
        self.assertEqual(len(labels), len(boundaries))

    def test_single_panel_still_works(self):
        figure = charts.build_stacked_figure(self._panels(count=1))
        self.assertEqual(len(figure.data), 1)
        self.assertEqual(figure.layout.xaxis.anchor, 'y')
        self.assertEqual(figure.layout.hovermode, 'x unified')


class PositionBandTests(unittest.TestCase):
    """价格位置背景带：高位红底、低位绿底，位置未知留白（不铺底色）。"""

    DAYS = [END - timedelta(days=offset) for offset in range(4, -1, -1)]

    def test_merges_runs_and_skips_unknown(self):
        positions = [
            (self.DAYS[0], codes.POS_HIGH),
            (self.DAYS[1], codes.POS_HIGH),
            (self.DAYS[2], codes.POS_UNKNOWN),
            (self.DAYS[3], codes.POS_LOW),
            (self.DAYS[4], codes.POS_LOW),
        ]
        bands = charts.position_bands(positions)
        # 高位段止于"下一个不同位置的观测日"，未知段不铺底、不计入
        self.assertEqual(
            bands,
            [(self.DAYS[0], self.DAYS[2], codes.POS_HIGH), (self.DAYS[3], self.DAYS[4], codes.POS_LOW)],
        )

    def test_alternating_positions_produce_one_band_each(self):
        positions = [
            (day, code)
            for day, code in zip(self.DAYS, (codes.POS_HIGH, codes.POS_LOW) * 3, strict=False)
        ]
        bands = charts.position_bands(positions)
        self.assertEqual(len(bands), len(positions))
        self.assertEqual([code for _start, _end, code in bands], [code for _day, code in positions])

    def test_empty_input(self):
        self.assertEqual(charts.position_bands([]), [])

    def test_only_unknown_positions_leave_the_background_empty(self):
        positions = [(day, codes.POS_UNKNOWN) for day in self.DAYS]
        self.assertEqual(charts.position_bands(positions), [])

    def test_bands_are_drawn_behind_the_lines_of_their_own_panel(self):
        panels = [
            (charts.SH_INDEX_SPEC, daily_series(30), None),
            (charts.SENTIMENT_SPECS[0], daily_series(30), None),
        ]
        bands = {charts.SH_INDEX_SPEC.key: [(END - timedelta(days=9), END, codes.POS_HIGH)]}
        figure = charts.build_stacked_figure(panels, bands=bands)
        rects = [shape for shape in figure.layout.shapes if shape.type == 'rect']
        self.assertEqual(len(rects), 1)
        rect = rects[0]
        self.assertEqual(rect.fillcolor, charts.POSITION_BAND_FILL[codes.POS_HIGH])
        self.assertEqual(rect.layer, 'below')  # 底色压在曲线与网格下面
        self.assertEqual(rect.yref, 'paper')
        # 铺满上证指数那张图（最上面一张）的整个高度，不越界到下一张
        domain = figure.layout.yaxis.domain
        self.assertEqual((float(rect.y0), float(rect.y1)), (float(domain[0]), float(domain[1])))

    def test_panels_without_bands_get_no_background(self):
        panels = [
            (charts.SH_INDEX_SPEC, daily_series(30), None),
            (charts.SENTIMENT_SPECS[0], daily_series(30), None),
        ]
        figure = charts.build_stacked_figure(
            panels, bands={charts.SENTIMENT_SPECS[0].key: [(END - timedelta(days=9), END, codes.POS_LOW)]}
        )
        rects = [shape for shape in figure.layout.shapes if shape.type == 'rect']
        self.assertEqual(len(rects), 1)
        # 这一次铺在下面那张（第二条 y 轴）上
        domain = figure.layout.yaxis2.domain
        self.assertEqual((float(rects[0].y0), float(rects[0].y1)), (float(domain[0]), float(domain[1])))

    def test_high_and_low_use_different_colors(self):
        self.assertNotEqual(
            charts.POSITION_BAND_FILL[codes.POS_HIGH], charts.POSITION_BAND_FILL[codes.POS_LOW]
        )


if __name__ == '__main__':
    unittest.main()
