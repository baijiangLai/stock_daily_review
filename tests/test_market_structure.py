import unittest

from review_workflow.infrastructure.market_structure import classify_pattern, classify_trend


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
