import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest import mock

from review_workflow.domain.config import WorkflowConfig
from review_workflow.infrastructure.legacy_gateway import LegacyPortfolioGateway
from review_workflow.infrastructure.local_review import (
    DailyBar,
    calculate_indicators,
    LocalStockData,
    parse_sohu_history,
    render_portfolio_summary,
    render_stock_review,
    _snapshot_is_for_date,
)


def _history_payload() -> list:
    return [
        {
            "status": 0,
            "code": "cn_000157",
            "hq": [
                ["2026-09-16", "10.00", "11.00", "1.00", "10.00%", "9.80", "11.20", "200.00", "2200.00", "5.00", ""],
                ["2026-09-15", "9.00", "10.00", "0.50", "5.26%", "8.90", "10.20", "100.00", "1000.00", "2.00", ""],
                ["2026-09-14", "8.50", "9.50", "0.50", "5.56%", "8.40", "9.60", "100.00", "950.00", "2.00", ""],
                ["2026-09-13", "8.00", "9.00", "0.00", "0.00%", "8.00", "9.10", "100.00", "900.00", "2.00", ""],
                ["2026-09-12", "7.00", "9.00", "0.00", "0.00%", "7.00", "9.00", "100.00", "850.00", "2.00", ""],
                ["2026-09-11", "7.00", "9.00", "0.00", "0.00%", "7.00", "9.00", "100.00", "800.00", "2.00", ""],
                ["2026-09-10", "7.00", "9.00", "0.00", "0.00%", "7.00", "9.00", "100.00", "750.00", "2.00", ""],
            ],
        }
    ]


class LocalReviewTest(unittest.TestCase):
    def test_parse_sohu_history_and_indicators(self) -> None:
        history = parse_sohu_history(_history_payload(), "2026-09-16")
        self.assertEqual(history[0].date, "2026-09-10")
        self.assertEqual(history[-1].close, 11.0)
        indicators = calculate_indicators(history)
        self.assertAlmostEqual(indicators["ma5"], 9.7)
        self.assertAlmostEqual(indicators["volume_ma5"], 120.0)
        self.assertIsNotNone(indicators["rsi6"])

    def test_render_stock_review_contains_position_and_rules(self) -> None:
        history = parse_sohu_history(_history_payload(), "2026-09-16")
        data = LocalStockData(
            stock={"name": "示例股票", "code": "000157"},
            holding={"name": "示例股票", "cost": "10.00", "shares": "100", "plan": "1年"},
            bar=history[-1],
            history=history,
            indicators=calculate_indicators(history),
            boards=[
                {
                    "label": "核心板块",
                    "name": "测试板块",
                    "change_pct": 2.0,
                    "amount_yuan": 100000000.0,
                }
            ],
            valuation={
                "market_cap": 10_000_000_000.0,
                "float_market_cap": 900_000_000.0,
                "pe": 20.0,
                "pb": 2.0,
            },
            financial=None,
            announcements=[],
            news=[],
            source_note="测试来源。",
        )
        report = render_stock_review(data)

        self.assertIn("示例股票 000157", report)
        self.assertIn("今日持仓盈亏 **+100.00 元**", report)
        self.assertIn("显著放量上涨", report)
        self.assertIn("100.00亿", report)
        self.assertIn("个股相对核心板块", report)
        self.assertIn("明显强于核心板块", report)
        self.assertIn("不构成投资建议", report)

    def test_portfolio_summary_uses_local_data_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = root / "local当日复盘.md"
            review_path.write_text("review", encoding="utf-8")
            sidecar = {
                "stock": {"name": "示例股票", "code": "000157"},
                "holding": {"name": "示例股票", "cost": "10.00", "shares": "100", "plan": "1年"},
                "bar": asdict(DailyBar(
                    "2026-09-16", 10, 11, 1, 10, 9.8, 11.2, 200, 2200, 5
                )),
                "indicators": {"ma5": 9.5},
            }
            (root / "local_review_data.json").write_text(
                json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
            )
            summary = render_portfolio_summary(
                "2026-09-16",
                [{"review_path": str(review_path)}],
                [{"name": "上证指数", "close": 3900.0, "change_pct": 0.0071}],
            )
            self.assertIn("本地组合摘要", summary)
            self.assertIn("示例股票", summary)
            self.assertIn("+100.00", summary)
            self.assertIn("上证指数", summary)
            self.assertIn("0.71%", summary)

    def test_gateway_local_provider_does_not_call_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "metadata.json").write_text(
                json.dumps({"resolved_stock": {"name": "示例股票", "code": "000157"}}),
                encoding="utf-8",
            )
            (root / "boards_metadata.json").write_text(
                json.dumps({"boards": {}}), encoding="utf-8"
            )
            review_path = root / "local当日复盘.md"
            with mock.patch(
                "review_workflow.infrastructure.legacy_gateway.generate_local_stock_review",
                return_value=({}, review_path),
            ) as generator:
                result = LegacyPortfolioGateway().analyze(
                    {"name": "示例股票", "cost": "10", "shares": "100", "plan": "1年"},
                    root,
                    WorkflowConfig(review_date="2026-09-16", provider="local"),
                )
            generator.assert_called_once()
            self.assertEqual(result.review_path, review_path)
            self.assertEqual(result.stock["code"], "000157")

    def test_stale_snapshot_is_rejected(self) -> None:
        snapshot_timestamp = int(
            datetime(
                2026, 9, 17, 9, 45, 36, tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
        )

        self.assertFalse(
            _snapshot_is_for_date({"f86": snapshot_timestamp}, "2026-09-16")
        )
        self.assertTrue(
            _snapshot_is_for_date({"f86": snapshot_timestamp}, "2026-09-17")
        )


if __name__ == "__main__":
    unittest.main()
