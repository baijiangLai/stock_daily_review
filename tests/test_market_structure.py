import unittest
from datetime import date, timedelta
from typing import Iterable, List, Optional

from review_workflow.infrastructure.market_structure import (
    analyze_trend_from_bars,
    build_price_zones,
    classify_pattern,
    classify_trend,
)


def _bars_from_closes(
    closes: Iterable[float],
    *,
    high_offset: float = 0.3,
    low_offset: float = 0.3,
    volume: float = 100.0,
) -> List[dict]:
    values = list(closes)
    start = date(2026, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "open": close - 0.1,
            "close": close,
            "high": close + high_offset,
            "low": close - low_offset,
            "volume_hands": volume,
        }
        for index, close in enumerate(values)
    ]


class MarketStructureTest(unittest.TestCase):
    def test_classifies_bullish_engulfing_and_strong_uptrend(self) -> None:
        pattern = classify_pattern(
            {"open": 10.0, "close": 11.0, "high": 11.2, "low": 9.9, "volume_hands": 120},
            {"open": 10.8, "close": 10.0, "high": 10.9, "low": 9.8, "volume_hands": 100},
            volume_ma5=100,
        )
        trend = classify_trend(11.0, 10.5, 10.0, 9.5, rsi=65)

        self.assertEqual(pattern["name"], "阳线反包")
        self.assertIn("至少 80%", pattern["definition"])
        self.assertIn("5日均量", pattern["definition"])
        self.assertEqual(trend["name"], "强上升趋势")
        self.assertIn("多头排列", trend["definition"])
        self.assertIn("RSI6 为 65.00", trend["definition"])

    def test_classifies_long_upper_shadow_and_bearish_trend(self) -> None:
        pattern = classify_pattern(
            {"open": 10.0, "close": 10.1, "high": 11.0, "low": 9.9, "volume_hands": 80}
        )
        trend = classify_trend(9.8, 10.0, 10.2, 10.4, rsi=28)

        self.assertEqual(pattern["name"], "长上影线")
        self.assertIn("上影线", pattern["definition"])
        self.assertEqual(trend["name"], "空头下跌趋势")
        self.assertIn("空头排列", trend["definition"])
        self.assertIn("超卖区", trend["definition"])

    def test_weekly_pattern_uses_weekly_period_words(self) -> None:
        pattern = classify_pattern(
            {"open": 10.0, "close": 10.5, "high": 10.8, "low": 9.8, "volume_hands": 120},
            volume_ma5=100,
            timeframe="周线",
        )
        trend = classify_trend(
            10.5, 10.0, 9.8, 9.5, rsi=55, rsi_name="周线RSI12"
        )

        self.assertEqual(pattern["name"], "放量阳线")
        self.assertIn("当周", pattern["definition"])
        self.assertIn("5周均量", pattern["definition"])
        self.assertEqual(trend["name"], "强上升趋势")
        self.assertIn("周线RSI12 为 55.00", trend["definition"])

    def test_invalid_ohlc_is_rejected(self) -> None:
        pattern = classify_pattern(
            {"open": 12.0, "close": 11.0, "high": 11.0, "low": 10.0}
        )

        self.assertEqual(pattern["name"], "形态待核实")
        self.assertIn("无法定义有效 K 线形态", pattern["definition"])

    def test_unsupported_timeframe_is_rejected(self) -> None:
        pattern = classify_pattern(
            {"open": 10.0, "close": 10.5, "high": 10.8, "low": 9.8},
            timeframe="60分钟",
        )

        self.assertEqual(pattern["name"], "形态待核实")
        self.assertIn("当前仅支持日线和周线形态", pattern["definition"])

    def test_trend_strength_and_change_cover_required_market_states(self) -> None:
        neutral = [10.0 + (0.1 if index % 2 == 0 else -0.1) for index in range(35)]
        cases = {
            "刚形成强上升趋势": _bars_from_closes(
                neutral + [10.2, 10.45, 10.7, 10.95, 11.2, 11.45]
            ),
            "持续强上升趋势": _bars_from_closes(
                10.0 + index * 0.08 for index in range(60)
            ),
            "强上升趋势但动能减弱": _bars_from_closes(
                [10.0 + index * 0.06 for index in range(40)]
                + [12.3, 12.24, 12.22, 12.26, 12.3, 12.35]
            ),
            "上涨转震荡": _bars_from_closes(
                [10.0 + index * 0.06 for index in range(40)]
                + [12.4, 12.45, 12.2, 12.15, 12.1, 12.12]
            ),
            "震荡转上涨": _bars_from_closes(
                [10.0 + (0.1 if index % 2 == 0 else -0.1) for index in range(40)]
                + [10.3, 10.65, 11.0, 11.35, 11.7, 12.05]
            ),
            "趋势反转": _bars_from_closes(
                [10.0 + index * 0.1 for index in range(45)]
                + [14.0, 13.4, 12.7, 11.9, 11.0, 10.1]
            ),
        }

        expected_states = {
            "刚形成强上升趋势": "强上升趋势",
            "持续强上升趋势": "强上升趋势",
            # 动能减弱只改变「趋势变化」，不允许把趋势状态直接改成下跌。
            "强上升趋势但动能减弱": "强上升趋势",
            "上涨转震荡": "震荡整理",
            "震荡转上涨": "强上升趋势",
            "趋势反转": "空头下跌趋势",
        }
        for caption, bars in cases.items():
            with self.subTest(caption):
                trend = analyze_trend_from_bars(bars)

                self.assertEqual(trend["name"], expected_states[caption])
                self.assertIn(trend["strength"], {"弱", "中", "强"})
                self.assertIn(trend["direction"], {"加强", "稳定", "减弱", "反转"})
                self.assertIsInstance(trend["strength_score"], int)
                self.assertTrue(trend["evidence"])
                self.assertIn(trend["name"], trend["interpretation"])

    def test_trend_change_is_separate_from_trend_state(self) -> None:
        strengthening = analyze_trend_from_bars(
            _bars_from_closes(
                [10.0 + (0.1 if index % 2 == 0 else -0.1) for index in range(35)]
                + [10.2, 10.45, 10.7, 10.95, 11.2, 11.45]
            )
        )
        weakening = analyze_trend_from_bars(
            _bars_from_closes(
                [10.0 + index * 0.06 for index in range(40)]
                + [12.3, 12.24, 12.22, 12.26, 12.3, 12.35]
            )
        )
        reversal = analyze_trend_from_bars(
            _bars_from_closes(
                [10.0 + index * 0.1 for index in range(45)]
                + [14.0, 13.4, 12.7, 11.9, 11.0, 10.1]
            )
        )

        self.assertEqual(strengthening["direction"], "加强")
        self.assertTrue(
            any(item.startswith("MA5 斜率较前期加快") for item in strengthening["evidence"])
        )
        self.assertIn("动能较前期增强", strengthening["interpretation"])

        self.assertEqual(weakening["name"], "强上升趋势")
        self.assertEqual(weakening["direction"], "减弱")
        self.assertIn("MA5 走平", weakening["evidence"])
        self.assertTrue(
            any(item.startswith("价格与 MA20 距离较前期收窄") for item in weakening["evidence"])
        )
        self.assertTrue(
            any(item.startswith("RSI6 较前期下降") for item in weakening["evidence"])
        )
        self.assertIn("动能较前期减弱", weakening["interpretation"])
        self.assertIn("不因此直接改变趋势状态", weakening["interpretation"])

        self.assertEqual(reversal["direction"], "反转")
        self.assertIn("反转风险", reversal["interpretation"])
        self.assertTrue(reversal["oversold"])
        self.assertFalse(reversal["overheated"])

    def test_trend_change_arrow_follows_trend_direction(self) -> None:
        """箭头跟随趋势推进方向：下跌趋势加强记 ↓、减弱记 ↑。"""

        falling_faster = [20.0]
        for index in range(39):
            falling_faster.append(falling_faster[-1] - (0.10 + index * 0.012))
        falling_slower = [20.0]
        for index in range(39):
            falling_slower.append(falling_slower[-1] - max(0.02, 0.60 - index * 0.015))

        faster = analyze_trend_from_bars(_bars_from_closes(falling_faster))
        slower = analyze_trend_from_bars(_bars_from_closes(falling_slower))

        self.assertEqual(faster["name"], "空头下跌趋势")
        self.assertEqual(faster["direction"], "加强")
        self.assertEqual(faster["direction_text"], "↓ 加强")
        self.assertIn("下跌动能较前期增强", faster["interpretation"])

        self.assertEqual(slower["name"], "空头下跌趋势")
        self.assertEqual(slower["direction"], "减弱")
        self.assertEqual(slower["direction_text"], "↑ 减弱")
        self.assertIn("下跌动能较前期减弱", slower["interpretation"])

    def test_trend_analysis_requires_enough_bars(self) -> None:
        trend = analyze_trend_from_bars(_bars_from_closes([10.0, 10.1, 10.2, 10.3]))

        self.assertEqual(trend["name"], "趋势待核实")
        self.assertEqual(trend["strength"], "待核实")
        self.assertEqual(trend["direction"], "待核实")
        self.assertIsNone(trend["strength_score"])
        self.assertTrue(trend["evidence"])

    def test_build_price_zones_merges_multiple_sources(self) -> None:
        bars = _bars_from_closes(
            [10.0, 10.3, 10.8, 10.4, 10.1]
            + [10.4, 10.7, 11.0, 10.5, 10.2]
            + [10.5, 10.75, 11.08, 10.55, 10.25]
            + [10.4, 10.6, 10.9, 10.5, 10.2]
        )
        for bar in bars:
            bar["high"] = bar["close"]
            bar["low"] = bar["close"] - 0.2
        zones = build_price_zones(
            bars,
            10.2,
            {"ma20": 10.02, "ma60": 9.8},
        )

        self.assertEqual(len(zones["support"]), 2)
        self.assertEqual(len(zones["resistance"]), 2)
        resistance_text = "\n".join(
            f"{zone['zone_low']}~{zone['zone_high']} {zone['reasons']}"
            for zone in zones["resistance"]
        )
        self.assertIn("前期高点", resistance_text)
        self.assertIn("多次冲高回落", resistance_text)
        support_reasons = [
            reason for zone in zones["support"] for reason in zone["reasons"]
        ]
        self.assertIn("MA20附近", support_reasons)
        for zone in zones["support"] + zones["resistance"]:
            self.assertLessEqual(zone["zone_low"], zone["zone_high"])
            self.assertIn(zone["strength"], {1, 2, 3, 4, 5})
            self.assertTrue(zone["reasons"])
            self.assertIn("breakout_confirmation", zone)

    def test_support_zone_merges_with_moving_average(self) -> None:
        """均线附近需要与结构位合并成同一区域，而不是单独当成支撑位。"""

        bars = _bars_from_closes(
            [10.6, 10.3, 10.05, 10.3] * 4
        )
        for bar in bars:
            bar["high"] = bar["close"]
            bar["low"] = bar["close"] - 0.05
        zones = build_price_zones(bars, 10.3, {"ma20": 10.0})

        merged = [
            zone
            for zone in zones["support"]
            if "MA20附近" in zone["reasons"] and "前期低点" in zone["reasons"]
        ]
        self.assertTrue(merged)
        zone = merged[0]
        self.assertLessEqual(zone["zone_low"], 10.0)
        self.assertGreaterEqual(zone["zone_high"], 10.0)
        self.assertGreaterEqual(zone["strength"], 4)

    def test_moving_average_alone_stays_a_weak_zone(self) -> None:
        """均线只能作为形成原因之一：只有均线来源时强度必须偏弱。"""

        bars = _bars_from_closes(10.0 + index * 0.05 for index in range(30))
        zones = build_price_zones(bars, 11.45, {"ma20": 11.13})

        ma20_zone = next(
            zone for zone in zones["support"] if zone["reasons"] == ["MA20附近"]
        )
        self.assertLessEqual(ma20_zone["strength"], 2)

    def test_build_price_zones_keeps_two_far_resistance_zones(self) -> None:
        bars = _bars_from_closes(
            [9.8, 10.0, 10.2]
            + [10.2, 10.55, 10.8, 10.4, 10.1]
            + [10.3, 10.6, 10.85, 10.4, 10.1]
            + [10.4, 10.9, 11.8, 11.2, 10.9]
            + [10.6, 10.4, 10.25]
        )
        for bar in bars:
            bar["high"] = bar["close"]
            bar["low"] = bar["close"] - 0.2
        zones = build_price_zones(bars, 10.7)
        centers = [
            (zone["zone_low"] + zone["zone_high"]) / 2
            for zone in zones["resistance"]
        ]

        self.assertEqual(len(centers), 2)
        self.assertGreater(centers[1] - centers[0], 0.5)

    def test_price_zone_breakout_conditions_are_recorded(self) -> None:
        bars = _bars_from_closes([10.0, 10.3, 10.8, 10.4, 10.1])
        zones = build_price_zones(bars, 10.1)
        resistance = zones["resistance"][0]
        confirmation = resistance["breakout_confirmation"]

        self.assertEqual(confirmation["volume_ratio_min"], 1.2)
        self.assertIn("close_above", confirmation)
        self.assertGreater(confirmation["close_above"], resistance["zone_low"])

    def test_support_and_resistance_zones_never_overlap(self) -> None:
        """支撑区必须整体位于收盘价下方，压力区整体位于收盘价上方。"""

        bars = _bars_from_closes(10.0 + index * 0.2 for index in range(40))
        close = bars[-1]["close"]
        zones = build_price_zones(
            bars,
            close,
            {
                "ma5": close - 0.05,
                "ma10": close - 0.3,
                "ma20": close - 0.8,
                "ma60": close - 2.0,
            },
        )

        self.assertEqual(len(zones["support"]), 2)
        self.assertEqual(len(zones["resistance"]), 2)
        for zone in zones["support"]:
            self.assertLessEqual(zone["zone_high"], close)
            self.assertLess(zone["zone_low"], zone["zone_high"])
        for zone in zones["resistance"]:
            self.assertGreaterEqual(zone["zone_low"], close)
            self.assertLess(zone["zone_low"], zone["zone_high"])
        self.assertLessEqual(
            max(zone["zone_high"] for zone in zones["support"]),
            min(zone["zone_low"] for zone in zones["resistance"]),
        )
        # 同一侧的两档区域也不能互相覆盖，否则「支撑区2」会压到「支撑区1」。
        self.assertGreater(
            zones["support"][0]["zone_low"], zones["support"][1]["zone_high"]
        )
        self.assertGreater(
            zones["resistance"][1]["zone_low"], zones["resistance"][0]["zone_high"]
        )

    def test_same_side_zones_are_separated(self) -> None:
        """同侧相邻两档必须按远近分开：支撑区2 上沿不能压到支撑区1 下沿。"""

        bars = _bars_from_closes([9.4, 9.6, 9.8, 9.9, 10.0] * 2 + [10.5])
        close = bars[-1]["close"]
        zones = build_price_zones(
            bars, close, {"ma5": 10.0, "ma10": 9.8, "ma20": 9.7}
        )

        supports = zones["support"]
        self.assertEqual(len(supports), 2)
        self.assertGreater(supports[0]["zone_low"], supports[1]["zone_high"])
        self.assertLessEqual(supports[0]["zone_high"], close)
        resistances = zones["resistance"]
        self.assertEqual(len(resistances), 2)
        self.assertGreater(resistances[1]["zone_low"], resistances[0]["zone_high"])

    def test_broken_pivot_high_becomes_support(self) -> None:
        """前高被有效突破后应转为支撑，而不是继续留在压力区上方。"""

        bars = _bars_from_closes(
            [10.0, 10.4, 10.9, 10.6, 10.2]
            + [10.3, 10.7, 11.0, 10.6, 10.3]
            + [10.5, 11.2, 11.8, 12.1]
        )
        for bar in bars:
            bar["high"] = bar["close"]
            bar["low"] = bar["close"] - 0.2
        close = bars[-1]["close"]
        zones = build_price_zones(bars, close)

        resistance_text = "\n".join(
            reason for zone in zones["resistance"] for reason in zone["reasons"]
        )
        support_text = "\n".join(
            reason for zone in zones["support"] for reason in zone["reasons"]
        )
        self.assertNotIn("前期高点", resistance_text)
        self.assertNotIn("20日高点", resistance_text)
        self.assertIn("前期高点", support_text)
        for zone in zones["resistance"]:
            self.assertGreaterEqual(zone["zone_low"], close)

    def test_invalid_previous_candle_does_not_create_engulfing(self) -> None:
        pattern = classify_pattern(
            {"open": 10.0, "close": 11.0, "high": 11.2, "low": 9.9, "volume_hands": 120},
            {"open": 10.8, "close": 10.0, "high": 10.9, "low": 11.0},
            volume_ma5=100,
        )

        self.assertEqual(pattern["name"], "放量阳线")

    def test_negative_current_volume_is_rejected(self) -> None:
        pattern = classify_pattern(
            {
                "open": 10.0,
                "close": 10.5,
                "high": 10.8,
                "low": 9.8,
                "volume_hands": -1,
            }
        )

        self.assertEqual(pattern["name"], "形态待核实")


if __name__ == "__main__":
    unittest.main()
