import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_workflow.domain.config import WorkflowConfig
from review_workflow.infrastructure.legacy_gateway import LegacyPortfolioGateway
from review_workflow.infrastructure.weekly_strategy import review_weekly_strategy


def _write_sidecar(
    root: Path,
    shares: str = "100",
    operation: bool = False,
) -> Path:
    stock_dir = root / "示例股票_000001"
    stock_dir.mkdir(parents=True, exist_ok=True)
    review_path = stock_dir / "local当日复盘.md"
    review_path.write_text("review", encoding="utf-8")
    payload = {
        "stock": {"name": "示例股票", "code": "000001"},
        "holding": {
            "name": "示例股票",
            "cost": "10.00",
            "shares": shares,
            "plan": "6个月内",
            **({"operation": {"action": "买入", "price": "10.00", "quantity": "20"}}
               if operation else {}),
        },
        "bar": {
            "date": "2026-09-14",
            "open": 10.0,
            "close": 10.5,
            "change": 0.5,
            "change_pct": 5.0,
            "low": 9.8,
            "high": 10.8,
            "volume_hands": 100.0,
            "amount_wan": 1000.0,
            "turnover_pct": 5.0,
        },
        "history": [
            {"date": "2026-09-14", "close": 10.5, "low": 9.8, "high": 10.8},
            {"date": "2026-09-11", "close": 10.0, "low": 9.5, "high": 10.2},
        ],
        "indicators": {
            "ma5": 10.0,
            "ma10": 9.8,
            "ma20": 9.7,
            "ma60": 9.5,
        },
        "boards": [],
        "valuation": {},
        "financial": None,
        "announcements": [],
        "news": [],
        "f10": None,
    }
    (stock_dir / "local_review_data.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return review_path


class WeeklyStrategyTest(unittest.TestCase):
    def test_local_summary_includes_weekly_strategy(self) -> None:
        with mock.patch(
            "review_workflow.infrastructure.legacy_gateway.review_weekly_strategy",
            return_value=("WEEKLY", Path("weekly.json"), Path("weekly.md")),
        ) as weekly, mock.patch(
            "review_workflow.infrastructure.legacy_gateway"
            ".generate_local_portfolio_summary",
            return_value="DAILY",
        ):
            summary = LegacyPortfolioGateway().summarize(
                "2026-09-16",
                [{"review_path": "fake.md"}],
                WorkflowConfig(review_date="2026-09-16", provider="local"),
            )

        weekly.assert_called_once()
        self.assertEqual(summary, "WEEKLY\n\n---\n\nDAILY")

    def test_creates_and_reviews_weekly_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = _write_sidecar(root / "day1", operation=True)
            weekly_root = root / "weekly"
            reviews = [{"review_path": str(review_path)}]

            with mock.patch(
                "review_workflow.infrastructure.weekly_strategy.WEEKLY_ROOT",
                weekly_root,
            ):
                first_document, json_path, markdown_path = review_weekly_strategy(
                    "2026-09-14", reviews
                )
                self.assertEqual(
                    json_path,
                    weekly_root / "2026-W38" / "2026-W38_交易策略单.json",
                )
                self.assertTrue(json_path.is_file())
                self.assertTrue(markdown_path.is_file())
                strategy = json.loads(json_path.read_text(encoding="utf-8"))
                self.assertEqual(strategy["version"], 1)
                self.assertTrue(strategy["first_trading_day"])
                self.assertEqual(strategy["baseline_holdings"]["000001"]["shares"], "100")
                self.assertIn("交易策略执行单", first_document)

                # 兼容旧版“按年份”目录，并在下一次执行时迁移到具体周目录。
                legacy_directory = weekly_root / "2026"
                legacy_directory.mkdir(parents=True, exist_ok=True)
                legacy_json = legacy_directory / json_path.name
                legacy_markdown = legacy_directory / markdown_path.name
                json_path.rename(legacy_json)
                markdown_path.rename(legacy_markdown)
                self.assertFalse(json_path.is_file())

                # 周内第二天：读取同一份策略并追加每日回顾。
                second_document, second_json_path, _ = review_weekly_strategy(
                    "2026-09-15", reviews
                )
                self.assertEqual(second_json_path, json_path)
                self.assertTrue(json_path.is_file())
                self.assertFalse(legacy_json.is_file())
                self.assertFalse(legacy_markdown.is_file())
                strategy = json.loads(json_path.read_text(encoding="utf-8"))
                self.assertEqual(strategy["version"], 1)
                self.assertEqual(
                    [item["date"] for item in strategy["daily_reviews"]],
                    ["2026-09-14", "2026-09-15"],
                )
                self.assertIn("每日执行回顾", second_document)
                self.assertIn("买入接近支撑", second_document)

                # 持仓变化后自动生成 v2。
                changed_path = _write_sidecar(root / "changed", shares="200")
                third_document, _, _ = review_weekly_strategy(
                    "2026-09-16", [{"review_path": str(changed_path)}]
                )
                strategy = json.loads(json_path.read_text(encoding="utf-8"))
                self.assertEqual(strategy["version"], 2)
                self.assertEqual(strategy["baseline_holdings"]["000001"]["shares"], "200")
                self.assertIn("策略更新记录", third_document)

    def test_prior_week_bar_does_not_make_monday_not_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = _write_sidecar(root)
            with mock.patch(
                "review_workflow.infrastructure.weekly_strategy.WEEKLY_ROOT",
                root / "weekly",
            ):
                _, json_path, _ = review_weekly_strategy(
                    "2026-09-14", [{"review_path": str(review_path)}]
                )
            strategy = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertTrue(strategy["first_trading_day"])


if __name__ == "__main__":
    unittest.main()
