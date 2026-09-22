import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from review_workflow.domain.config import WorkflowConfig
from review_workflow.infrastructure.legacy_gateway import LegacyPortfolioGateway
from review_workflow.infrastructure.weekly_strategy import (
    _item_status,
    _operation_status,
    _render,
    _zone_or_point,
    review_weekly_strategy,
)


def _write_sidecar(
    root: Path,
    shares: str = "100",
    operation: bool = False,
    bar_date: str = "2026-09-14",
    rich_history: bool = False,
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
            "date": bar_date,
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
        "history": _history(bar_date, rich_history),
        "indicators": {
            "ma5": 10.0,
            "ma10": 9.8,
            "ma20": 9.7,
            "ma60": 9.5,
            "rsi6": 35.0,
            "rsi12": 40.0,
            "volume_ma5": 80.0,
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


def _history(bar_date: str, rich: bool) -> list:
    if not rich:
        return [
            {
                "date": bar_date,
                "open": 10.0,
                "close": 10.5,
                "low": 9.8,
                "high": 10.8,
                "volume_hands": 100.0,
            },
            {
                "date": "2026-09-11",
                "open": 10.0,
                "close": 10.0,
                "low": 9.5,
                "high": 10.2,
                "volume_hands": 80.0,
            },
        ]

    parsed = date.fromisoformat(bar_date)
    rows = []
    index = 0
    for offset in range(210, -1, -1):
        current = parsed - timedelta(days=offset)
        if current.weekday() >= 5:
            continue
        close = 8.0 + index * 0.015 + (0.08 if index % 5 == 0 else 0)
        rows.append(
            {
                "date": current.isoformat(),
                "open": close - 0.15,
                "close": close,
                "high": close + 0.2,
                "low": close - 0.25,
                "volume_hands": 100.0 + index,
            }
        )
        index += 1
    rows[-1].update({"open": 10.0, "close": 10.5, "high": 10.8, "low": 9.8})
    return rows


def _write_chart_metadata(review_path: Path) -> None:
    stock_dir = review_path.parent
    metadata = {
        "resolved_stock": {"name": "示例股票", "code": "000001"},
        "screenshots": {
            "daily_kline": "04_daily_kline.png",
            "weekly_kline": "04_weekly_kline.png",
        },
    }
    (stock_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
    )
    for filename in metadata["screenshots"].values():
        (stock_dir / filename).write_bytes(b"fake-chart")


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

    def test_friday_summary_backfills_weekly_chart_before_weekly_strategy(self) -> None:
        with mock.patch(
            "review_workflow.infrastructure.legacy_gateway.review_weekly_strategy",
            return_value=("WEEKLY", Path("weekly.json"), Path("weekly.md")),
        ), mock.patch(
            "review_workflow.infrastructure.legacy_gateway"
            ".generate_local_portfolio_summary",
            return_value="DAILY",
        ), mock.patch(
            "portfolio_daily_review.ensure_weekly_kline_screenshot",
            return_value=True,
        ) as ensure_weekly:
            LegacyPortfolioGateway().summarize(
                "2026-09-18",
                [{"review_path": "stock/local.md", "stock": {"name": "示例股票"}}],
                WorkflowConfig(review_date="2026-09-18", provider="local"),
            )

        ensure_weekly.assert_called_once()

    def test_skip_capture_does_not_backfill_weekly_chart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stock_dir = Path(directory)
            with mock.patch(
                "portfolio_daily_review.prepare_stock_dir",
                return_value=(stock_dir, {"name": "示例股票", "code": "000001"}),
            ), mock.patch(
                "portfolio_daily_review.ensure_holding_metadata"
            ), mock.patch(
                "portfolio_daily_review.capture_or_reuse_boards"
            ), mock.patch(
                "portfolio_daily_review.ensure_weekly_kline_screenshot",
                return_value=True,
            ) as ensure_weekly:
                LegacyPortfolioGateway().capture(
                    {"name": "示例股票", "cost": "10", "shares": "100", "plan": "1年"},
                    WorkflowConfig(
                        review_date="2026-09-18",
                        provider="local",
                        skip_capture=True,
                    ),
                    [],
                )

        ensure_weekly.assert_not_called()

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
                for item in strategy["items"]:
                    self.assertNotIn("main_business", item)
                    self.assertNotIn("themes", item)
                self.assertNotIn("主营", first_document)
                self.assertNotIn("题材", first_document)

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

    def test_friday_builds_next_week_strategy_with_daily_and_weekly_charts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = _write_sidecar(
                root,
                bar_date="2026-09-18",
                rich_history=True,
            )
            _write_chart_metadata(review_path)
            weekly_root = root / "weekly"

            with mock.patch(
                "review_workflow.infrastructure.weekly_strategy.WEEKLY_ROOT",
                weekly_root,
            ):
                document, json_path, markdown_path = review_weekly_strategy(
                    "2026-09-18", [{"review_path": str(review_path)}]
                )

            week_directory = weekly_root / "2026-W39"
            self.assertEqual(
                json_path,
                week_directory / "2026-W39_交易策略单.json",
            )
            self.assertEqual(
                markdown_path,
                week_directory / "2026-W39_交易策略单.md",
            )
            strategy = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(strategy["target_week_start"], "2026-09-21")
            self.assertTrue(strategy["prepared_before_week"])
            self.assertFalse(strategy["first_trading_day"])

            item = strategy["items"][0]
            self.assertIn("daily_pattern", item)
            self.assertIn("daily_trend", item)
            self.assertIn("weekly_pattern", item)
            self.assertIn("weekly_trend", item)
            self.assertEqual(item["daily_pattern"]["name"], "放量阳线")
            self.assertEqual(item["daily_trend"]["name"], "强上升趋势")
            self.assertIn("5日均量", item["daily_pattern"]["definition"])
            self.assertIn("RSI6", item["daily_trend"]["definition"])
            self.assertIn("当周", item["weekly_pattern"]["definition"])
            self.assertIn("5周均量", item["weekly_pattern"]["definition"])
            self.assertIn("周线RSI12", item["weekly_trend"]["definition"])
            self.assertTrue(item["left_side_buy"]["enabled"])
            self.assertGreaterEqual(item["right_side_buy"]["trigger_price"], 10.5)
            self.assertEqual(len(item["support_zones"]), 2)
            self.assertEqual(len(item["resistance_zones"]), 2)
            self.assertIn("zone_low", item["support_zones"][0])
            self.assertIn("strength", item["resistance_zones"][0])
            self.assertIn("breakout_confirmation", item["resistance_zones"][0])
            self.assertLess(item["stop_loss"]["price"], 10.5)
            self.assertLessEqual(item["stop_loss"]["risk_pct"], 0.08)
            self.assertIsNotNone(item["weekly_signals"]["ma10"])
            self.assertIn("charts/示例股票_000001_daily_kline.png", document)
            self.assertIn("charts/示例股票_000001_weekly_kline.png", document)
            # 每只股票一个独立小节，明细放在个股内部而不是开头集中罗列。
            self.assertIn("## 示例股票（000001）", document)
            self.assertIn("### 核心策略与买卖触发", document)
            self.assertIn("### 形态与趋势定义", document)
            self.assertIn("- 日线形态：", document)
            self.assertIn("- 周线趋势：", document)
            self.assertIn("### 支撑/压力区域", document)
            self.assertIn("### 指标快照", document)
            self.assertIn("### 日线/周线截图", document)
            self.assertIn("- 左侧买入：", document)
            self.assertIn("- 右侧突破：", document)
            self.assertIn("RSI6 ≤ 40", document)
            self.assertIn("100.00 手 / 80.00 手", document)
            self.assertIn("收盘站上", document)
            self.assertIn("止损线", document)
            self.assertTrue(
                (week_directory / "charts" / "示例股票_000001_daily_kline.png").is_file()
            )
            self.assertTrue(
                (week_directory / "charts" / "示例股票_000001_weekly_kline.png").is_file()
            )

    def test_upgrades_existing_strategy_to_signal_schema(self) -> None:
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
                strategy["signal_schema"] = 2
                for item in strategy["items"]:
                    for key in (
                        "left_side_buy",
                        "right_side_buy",
                        "stop_loss",
                        "daily_signals",
                        "weekly_signals",
                        "daily_pattern",
                        "daily_trend",
                        "weekly_pattern",
                        "weekly_trend",
                    ):
                        item.pop(key, None)
                json_path.write_text(
                    json.dumps(strategy, ensure_ascii=False), encoding="utf-8"
                )

                document, _, _ = review_weekly_strategy(
                    "2026-09-14", [{"review_path": str(review_path)}]
                )

            upgraded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["version"], 2)
            self.assertEqual(upgraded["signal_schema"], 4)
            self.assertIn("left_side_buy", upgraded["items"][0])
            self.assertIn("right_side_buy", upgraded["items"][0])
            self.assertIn("stop_loss", upgraded["items"][0])
            for key in (
                "daily_pattern",
                "daily_trend",
                "weekly_pattern",
                "weekly_trend",
            ):
                self.assertIn(key, upgraded["items"][0])
                self.assertIn("定义", upgraded["items"][0][key]["definition"])
            self.assertIn("策略格式升级", document)

    def test_weekend_uses_latest_trading_data_for_next_week(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = _write_sidecar(
                root,
                bar_date="2026-09-20",
                rich_history=True,
            )
            _write_chart_metadata(review_path)

            with mock.patch(
                "review_workflow.infrastructure.weekly_strategy.WEEKLY_ROOT",
                root / "weekly",
            ):
                _, json_path, _ = review_weekly_strategy(
                    "2026-09-20", [{"review_path": str(review_path)}]
                )

            strategy = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(strategy["target_week_start"], "2026-09-21")
            self.assertTrue(strategy["prepared_before_week"])
            self.assertEqual(
                strategy["items"][0]["weekly_signals"]["week_ending"],
                "2026-09-18",
            )

    def test_legacy_strategy_without_zone_fields_falls_back_to_price_points(self) -> None:
        """旧版策略没有区域字段时，仍能渲染，并退回原有价格点。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = _write_sidecar(root, bar_date="2026-09-14")
            with mock.patch(
                "review_workflow.infrastructure.weekly_strategy.WEEKLY_ROOT",
                root / "weekly",
            ):
                _, json_path, _ = review_weekly_strategy(
                    "2026-09-14", [{"review_path": str(review_path)}]
                )

            strategy = json.loads(json_path.read_text(encoding="utf-8"))
            strategy["signal_schema"] = 3
            for item in strategy["items"]:
                for key in ("support_zones", "resistance_zones", "right_trigger_price"):
                    item.pop(key, None)

            document = _render(strategy)

            self.assertIn("## 支撑/压力区域", document)
            self.assertIn("旧版策略：仅记录价格点", document)

            # 个股小节内的区域明细退回旧版四个价格点（支撑1/支撑2/压力1/压力2），
            # 不依赖核心策略文案，避免策略描述调整时误报。
            core_rows = [
                line
                for line in document.splitlines()
                if "| 10.00 | 待核实 | 旧版策略" in line
            ]
            self.assertEqual(len(core_rows), 1)

            # 区域明细退回价格点（无股票列，行在个股小节内），并标注“旧版策略”。
            for label, point in (
                ("支撑区1", "10.00"),
                ("支撑区2", "9.80"),
                ("压力区1", "10.80"),
                ("压力区2", "11.02"),
            ):
                self.assertIn(
                    f"| {label} | {point} | 待核实 | "
                    "旧版策略：仅记录价格点，下次运行会自动升级 | 待核实 |",
                    document,
                )

            # 旧版字段缺失时的取数顺序：先区域，再退回价格点。
            legacy_item = {
                "name": "示例股票",
                "support_1": 10.0,
                "support_2": 9.8,
                "pressure_1": 10.8,
                "pressure_2": 11.02,
            }
            self.assertEqual(_zone_or_point(legacy_item, "support", 1), "10.00")
            self.assertEqual(_zone_or_point(legacy_item, "support", 2), "9.80")
            self.assertEqual(_zone_or_point(legacy_item, "resistance", 1), "10.80")
            self.assertEqual(_zone_or_point(legacy_item, "resistance", 2), "11.02")

            self.assertEqual(_zone_or_point(legacy_item, "support", 3), "待核实")
            upgraded_item = {
                **legacy_item,
                "support_zones": [
                    {"zone_low": 9.76, "zone_high": 10.04},
                    {"zone_low": 9.5, "zone_high": 9.6},
                ],
            }
            self.assertEqual(
                _zone_or_point(upgraded_item, "support", 1), "9.76 ~ 10.04"
            )
            self.assertEqual(_zone_or_point(upgraded_item, "support", 2), "9.50 ~ 9.60")
            self.assertEqual(_zone_or_point(upgraded_item, "support", 3), "待核实")

    def test_right_side_breakout_requires_zone_high_and_volume(self) -> None:
        """右侧买入升级为“有效突破整个压力区”，其余条件仍沿用原有规则。"""

        strategy_item = {
            "right_trigger_price": 10.5,
            "support_1": 9.0,
            "pressure_1": 10.2,
            "pressure_2": 11.0,
            "stop_loss": {"price": 8.5},
            "support_zones": [{"zone_low": 9.0, "zone_high": 9.2}],
            "resistance_zones": [{"zone_low": 10.3, "zone_high": 10.5}],
        }

        def payload(close, volume, ma5=10.6, ma10=10.4):
            return {
                "bar": {"close": close, "volume_hands": volume},
                "indicators": {"volume_ma5": 100.0, "ma5": ma5, "ma10": ma10},
            }

        # 未站上压力区上沿：仍按原有压力区处理。
        self.assertEqual(_item_status(strategy_item, payload(10.4, 200.0)), "进入压力/减仓区")
        # 突破压力区但成交量不足。
        self.assertEqual(
            _item_status(strategy_item, payload(10.8, 100.0)),
            "价格突破压力区，但右侧确认不足：量能不足",
        )
        # 放量突破但收盘不在 MA5/MA10 上方。
        self.assertEqual(
            _item_status(strategy_item, payload(10.8, 150.0, ma5=11.0, ma10=10.9)),
            "价格突破压力区，但右侧确认不足：日线均线位置不足",
        )
        # 同时满足原有右侧突破规则。
        self.assertEqual(_item_status(strategy_item, payload(10.8, 150.0)), "已有效突破压力区")
        # 止损优先级最高，不受突破状态影响。
        self.assertEqual(
            _item_status(
                {**strategy_item, "stop_loss": {"price": 10.9}}, payload(10.8, 150.0)
            ),
            "已触发止损线",
        )

    def test_operation_status_uses_price_zones(self) -> None:
        strategy_item = {
            "right_trigger_price": 10.5,
            "support_zones": [{"zone_low": 9.8, "zone_high": 10.0}],
            "resistance_zones": [{"zone_low": 10.3, "zone_high": 10.5}],
            "stop_loss": {"price": 8.5},
        }

        def payload(price, action="买入", close=10.2, volume=150.0):
            return {
                "bar": {"close": close, "volume_hands": volume},
                "indicators": {"volume_ma5": 100.0, "ma5": 10.0, "ma10": 9.9},
                "holding": {
                    "operation": {"action": action, "price": price, "quantity": 100}
                },
            }

        self.assertEqual(
            _operation_status(strategy_item, payload(9.9)), "买入接近支撑：执行有安全边际"
        )
        self.assertEqual(
            _operation_status(strategy_item, payload(10.4)), "买入接近压力：存在追高风险"
        )
        self.assertEqual(
            _operation_status(strategy_item, payload(10.1)), "买入位于中性区间"
        )
        self.assertEqual(
            _operation_status(strategy_item, payload(10.8, close=10.8)),
            "买入达到有效右侧突破条件",
        )
        self.assertEqual(
            _operation_status(strategy_item, payload(10.8, close=10.8, volume=100.0)),
            "买入突破压力区，但右侧确认不足",
        )
        self.assertEqual(
            _operation_status(strategy_item, payload(10.4, action="卖出")),
            "卖出接近压力：执行质量较好",
        )
        self.assertEqual(
            _operation_status(strategy_item, payload(9.9, action="卖出")),
            "卖出接近支撑：执行质量偏差",
        )


if __name__ == "__main__":
    unittest.main()
