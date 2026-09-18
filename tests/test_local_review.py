import json
import gzip
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
    F10Data,
    calculate_indicators,
    fetch_market_indices,
    fetch_f10_profile,
    fetch_stock_history,
    LocalStockData,
    _decode_response_payload,
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
    def test_decode_gzip_response_payload(self) -> None:
        compressed = gzip.compress('{"ok":true}'.encode("utf-8"))

        self.assertEqual(
            _decode_response_payload(compressed),
            b'{"ok":true}',
        )
        self.assertEqual(
            _decode_response_payload(b'{"ok":true}'),
            b'{"ok":true}',
        )

    def test_fetch_market_indices_uses_exact_history_date(self) -> None:
        payload = [
            {
                "status": 0,
                "hq": [[
                    "2026-09-17", "3860.00", "3891.60", "27.32",
                    "0.71%", "3842.72", "3894.66", "100", "10000", None,
                ]],
            }
        ]
        with mock.patch(
            "review_workflow.infrastructure.local_review._request_json",
            return_value=payload,
        ) as request:
            indices = fetch_market_indices("2026-09-17", timeout=5)

        self.assertEqual(len(indices), 3)
        self.assertEqual(indices[0]["name"], "上证指数")
        self.assertEqual(indices[0]["close"], 3891.60)
        self.assertAlmostEqual(indices[0]["change_pct"], 0.0071)
        self.assertEqual(request.call_count, 3)

    def test_fetch_market_indices_falls_back_to_exact_snapshot(self) -> None:
        prior_payload = [
            {
                "status": 0,
                "hq": [[
                    "2026-09-17", "3860.00", "3891.60", "27.32",
                    "0.71%", "3842.72", "3894.66", "100", "10000", None,
                ]],
            }
        ]
        snapshot = {"name": "上证指数", "close": 3911.87, "change_pct": 0.0094}
        with mock.patch(
            "review_workflow.infrastructure.local_review._request_json",
            return_value=prior_payload,
        ), mock.patch(
            "review_workflow.infrastructure.local_review.fetch_market_index_snapshot",
            return_value=snapshot,
        ) as snapshot_mock:
            indices = fetch_market_indices("2026-09-18", timeout=5)

        self.assertEqual(indices, [snapshot, snapshot, snapshot])
        self.assertEqual(snapshot_mock.call_count, 3)

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
            holding={
                "name": "示例股票",
                "cost": "10.00",
                "shares": "100",
                "plan": "1年",
                "operation": {"action": "买入", "price": "10.00", "quantity": "20"},
            },
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
            f10=F10Data(
                company={
                    "ORG_NAME": "示例股份有限公司",
                    "EM2016": "信息技术-通信设备",
                    "CHAIRMAN": "张三",
                    "PRESIDENT": "李四",
                    "EMP_NUM": 12000,
                    "ORG_PROFILE": "示例公司专注通信设备与数据中心冷却业务。",
                },
                listing={"LISTING_DATE": "2010-01-01 00:00:00"},
                main_business_date="2026-06-30",
                main_business=[
                    {
                        "name": "通信设备",
                        "income_yuan": 1_000_000_000.0,
                        "income_ratio": 0.65,
                        "gross_margin": 0.28,
                    },
                    {
                        "name": "数据中心冷却",
                        "income_yuan": 300_000_000.0,
                        "income_ratio": 0.30,
                        "gross_margin": 0.12,
                    },
                ],
                regions=[
                    {"name": "境内销售", "income_ratio": 0.75},
                    {"name": "境外销售", "income_ratio": 0.25},
                ],
                business_review="公司通信设备收入保持增长。",
                boards=["通信", "通信设备"],
                concepts=[
                    {"keyword": "通信设备", "classification": "主营业务"},
                    {"keyword": "AI 算力", "classification": "行业背景"},
                ],
                holder_stats={
                    "HOLDER_TOTAL_NUM": 100000,
                    "TOTAL_NUM_RATIO": 25.0,
                    "HOLD_FOCUS": "非常分散",
                    "FREEHOLD_RATIO_TOTAL": 30.0,
                },
                actual_controller={"HOLDER_NAME": "示例控股", "HOLD_RATIO": 25.0},
                top_float_holders=[
                    {"name": "示例控股", "ratio": 20.0, "change": "不变"}
                ],
                errors=[],
            ),
            source_note="测试来源。",
        )
        report = render_stock_review(data)

        self.assertIn("示例股票 000157", report)
        self.assertIn("今日持仓盈亏 **+100.00 元**", report)
        self.assertIn("显著放量上涨", report)
        self.assertIn("100.00亿", report)
        self.assertIn("个股相对核心板块", report)
        self.assertIn("明显强于核心板块", report)
        self.assertIn("F10 公司资料与主营结构", report)
        self.assertIn("今日实际操作复盘", report)
        self.assertIn("买入 20 股，成交价 10.00 元", report)
        self.assertIn("执行评价", report)
        self.assertIn("后续操作", report)
        self.assertIn("通信设备", report)
        self.assertIn("海外暴露", report)
        self.assertIn("筹码趋于分散", report)
        self.assertIn("不构成投资建议", report)

    def test_stock_history_falls_back_to_exact_date_snapshot(self) -> None:
        snapshot = DailyBar(
            "2026-09-17", 10.2, 10.5, 0.5, 5.0, 10.0, 10.6,
            300.0, 3200.0, 6.0,
        )
        with mock.patch(
            "review_workflow.infrastructure.local_review._request_json",
            return_value=_history_payload(),
        ), mock.patch(
            "review_workflow.infrastructure.local_review.fetch_stock_snapshot_bar",
            return_value=snapshot,
        ):
            history = fetch_stock_history(
                {"name": "示例股票", "code": "000157", "quote_id": "0.000157"},
                "2026-09-17",
                timeout=5,
            )

        self.assertEqual(history[-1], snapshot)
        self.assertIsNotNone(calculate_indicators(history)["ma5"])

    def test_render_operation_section_supports_sell(self) -> None:
        history = parse_sohu_history(_history_payload(), "2026-09-16")
        data = LocalStockData(
            stock={"name": "示例股票", "code": "000157"},
            holding={
                "name": "示例股票",
                "cost": "10.00",
                "shares": "100",
                "plan": "1年",
                "operation": {"action": "卖出", "price": "10.80", "quantity": "30"},
            },
            bar=history[-1],
            history=history,
            indicators=calculate_indicators(history),
            boards=[],
            valuation={},
            financial=None,
            announcements=[],
            news=[],
            f10=None,
            source_note="测试来源。",
        )

        report = render_stock_review(data)

        self.assertIn("今日持仓盈亏 **+124.00 元**", report)
        self.assertIn("卖出 30 股，成交价 10.80 元", report)
        self.assertIn("相对综合成本，本次卖出估算盈亏 **+24.00 元**", report)
        self.assertIn("卖出后股价上行", report)
        self.assertIn("等待回踩", report)

    def test_fetch_f10_profile_filters_future_report_dates(self) -> None:
        def module(stock, name: str, timeout: float):
            if name == "CompanySurvey":
                return {
                    "jbzl": [{
                        "ORG_NAME": "示例股份有限公司",
                        "EM2016": "信息技术-通信设备",
                        "CHAIRMAN": "张三",
                        "PRESIDENT": "李四",
                        "EMP_NUM": 12000,
                        "ORG_PROFILE": "示例公司。",
                    }],
                    "fxxg": [{"LISTING_DATE": "2010-01-01 00:00:00"}],
                }
            if name == "BusinessAnalysis":
                return {
                    "zygcfx": [
                        {
                            "REPORT_DATE": "2026-06-30 00:00:00",
                            "MAINOP_TYPE": "2",
                            "ITEM_NAME": "通信设备",
                            "MAIN_BUSINESS_INCOME": 1000000000.0,
                            "MBI_RATIO": 0.65,
                            "GROSS_RPOFIT_RATIO": 0.28,
                        },
                        {
                            "REPORT_DATE": "2026-09-30 00:00:00",
                            "MAINOP_TYPE": "2",
                            "ITEM_NAME": "未来报告期业务",
                            "MAIN_BUSINESS_INCOME": 2000000000.0,
                            "MBI_RATIO": 0.80,
                            "GROSS_RPOFIT_RATIO": 0.40,
                        },
                    ],
                    "jyps": [{
                        "REPORT_DATE": "2026-06-30 00:00:00",
                        "BUSINESS_REVIEW": "通信设备收入增长。",
                    }],
                }
            if name == "CoreConception":
                return {
                    "ssbk": [{"BOARD_NAME": "通信设备"}],
                    "hxtc": [
                        {"KEYWORD": "通信设备", "KEY_CLASSIF": "主营业务"}
                    ],
                }
            return {
                "gdrs": [{
                    "END_DATE": "2026-06-30 00:00:00",
                    "HOLDER_TOTAL_NUM": 100000,
                    "TOTAL_NUM_RATIO": 25.0,
                    "HOLD_FOCUS": "非常分散",
                    "FREEHOLD_RATIO_TOTAL": 30.0,
                }],
                "sdltgd": [{
                    "END_DATE": "2026-06-30 00:00:00",
                    "HOLDER_NAME": "示例控股",
                    "FREE_HOLDNUM_RATIO": 20.0,
                    "HOLD_NUM_CHANGE": "不变",
                }],
                "sjkzr": [{"HOLDER_NAME": "示例控股", "HOLD_RATIO": 25.0}],
            }

        with mock.patch(
            "review_workflow.infrastructure.local_review._f10_module",
            side_effect=module,
        ):
            profile = fetch_f10_profile(
                {"code": "600522"}, "2026-09-17", timeout=5
            )

        self.assertEqual(profile.main_business_date, "2026-06-30")
        self.assertEqual(profile.main_business[0]["name"], "通信设备")
        self.assertEqual(profile.boards, ["通信设备"])
        self.assertEqual(profile.actual_controller["HOLDER_NAME"], "示例控股")

    def test_portfolio_summary_uses_local_data_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = root / "local当日复盘.md"
            review_path.write_text("review", encoding="utf-8")
            sidecar = {
                "stock": {"name": "示例股票", "code": "000157"},
                "holding": {
                    "name": "示例股票",
                    "cost": "10.00",
                    "shares": "100",
                    "plan": "1年",
                    "operation": {"action": "买入", "price": "10.50", "quantity": "20"},
                },
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
            self.assertIn("+90.00", summary)
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
