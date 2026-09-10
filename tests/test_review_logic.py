import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import portfolio_daily_review as portfolio
import run_daily_review as review
from run_daily_review import (
    ReviewError,
    build_prompt_parts,
    collect_board_images,
    grounding_sources,
    metadata_matches_query,
    split_template,
    stock_secid,
    validate_reused_boards,
    normalize_stock_query,
    append_zhipu_sources,
    append_external_search_sources,
    format_zhipu_search_evidence,
    resolve_model,
    resolve_provider,
    resolve_search_provider,
    search_tool_name,
    zhipu_search_payload,
    zhipu_search_queries,
    zhipu_request_payload,
)


TEMPLATE = """# 股票名称/代码
当前数据

### 一、 板块
板块内容

### 二、 技术面与量价状态
技术面内容

### 三、近期大事
大事与仓位内容
"""


class ReviewLogicTest(unittest.TestCase):
    def test_stock_secid_prefers_quote_id(self) -> None:
        self.assertEqual(
            stock_secid({"quote_id": "0.000157", "code": "000157"}), "0.000157"
        )
        self.assertEqual(stock_secid({"code": "600519"}), "1.600519")
        self.assertEqual(normalize_stock_query("SH600519"), "600519")
        self.assertEqual(normalize_stock_query("1.600519"), "600519")

    def test_template_splits_and_builds_ordered_prompt(self) -> None:
        current, board, technical, closing = split_template(TEMPLATE)
        self.assertIn("当前数据", current)
        self.assertIn("一、 板块", board)
        self.assertIn("二、 技术面与量价状态", technical)
        self.assertIn("三、近期大事", closing)

        stock = {"name": "示例股票", "code": "000157"}
        images = [{"description": "图", "mime_type": "image/png", "data": ""}]
        holding = {"cost": "10.00", "shares": "100", "plan": "1年"}
        board_metadata = {
            "boards": {
                "一级行业": {"name": "机械设备", "code": "BK1205"},
                "核心板块": {"name": "工程机械整机", "code": "BK1393"},
                "细分方向": {"name": "工程机械", "code": "BK0739"},
            }
        }
        parts = build_prompt_parts(
            TEMPLATE,
            stock,
            images,
            images,
            holding,
            board_metadata,
            "2026-09-01",
        )
        texts = [part["text"] for part in parts if "text" in part]

        self.assertIn("示例股票 / 000157", texts[0])
        self.assertNotIn("股票名称/代码", texts[0])
        self.assertIn("复盘基准日：2026-09-01", texts[0])
        self.assertIn("机械设备（BK1205）", texts[0])
        self.assertIn("本次动态资料检索来源为 Google Search", texts[0])
        self.assertIn("板块龙头", texts[0])
        self.assertIn("必须优先使用 Google Search 提供的检索证据", texts[0])
        self.assertIn("禁止把成分股表格", texts[0])
        self.assertTrue(any("分析分段 1" in text for text in texts))
        self.assertTrue(any("分析分段 2" in text for text in texts))
        self.assertTrue(any("分析分段 3" in text for text in texts))

    def test_zhipu_payload_uses_data_url_and_web_search(self) -> None:
        parts = [
            {"text": "分析文本"},
            {
                "inline_data": {
                    "mime_type": "image/png",
                    "data": "aW1hZ2U=",
                }
            },
        ]

        payload = zhipu_request_payload(parts, "glm-5.3-flash", True)

        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "分析文本"})
        self.assertEqual(
            content[1]["image_url"]["url"],
            "data:image/png;base64,aW1hZ2U=",
        )
        self.assertEqual(payload["tools"][0]["type"], "web_search")
        self.assertTrue(payload["tools"][0]["web_search"]["enable"])

    def test_zhipu_standalone_search_uses_search_std(self) -> None:
        payload = zhipu_search_payload("示例股票 2026-09-01 财报")

        self.assertEqual(payload["search_engine"], "search_std")
        self.assertFalse(payload["search_intent"])
        self.assertEqual(payload["count"], 6)

    def test_engineering_peer_candidates_include_current_stock(self) -> None:
        metadata = {
            "boards": {
                "核心板块": {"name": "工程机械整机", "code": "BK1393"},
            }
        }

        peers = review.infer_peer_stocks(
            metadata, {"name": "示例股票", "code": "000157"}
        )

        self.assertEqual(len(peers), 9)
        self.assertIn("000157", {peer["code"] for peer in peers})
        self.assertIn("600031", {peer["code"] for peer in peers})

    def test_stock_daily_bar_parses_exact_review_date(self) -> None:
        original = review.request_json
        payload = {
            "rc": 0,
            "data": {
                "klines": [
                    "2026-08-31,6.79,6.69,6.82,6.68,731288,491200215.29,"
                    "2.05,-1.91,-0.13,1.03",
                    "2026-09-01,6.70,6.73,6.75,6.69,458066,308233680.48,"
                    "0.90,0.60,0.04,0.65",
                ]
            },
        }

        def fake_request_json(url, timeout):
            self.assertIn("beg=20260901", url)
            self.assertIn("end=20260901", url)
            return payload

        review.request_json = fake_request_json
        try:
            bar = review.fetch_stock_daily_bar(
                {"name": "示例股票", "code": "000157"}, "2026-09-01", 10.0
            )
        finally:
            review.request_json = original

        self.assertEqual(bar["date"], "2026-09-01")
        self.assertEqual(bar["close"], "6.73")
        self.assertEqual(bar["change_pct"], "0.60")
        self.assertEqual(bar["turnover_pct"], "0.65")

    def test_peer_metadata_and_prompt_use_dated_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stock_dir = Path(directory)
            screenshot = stock_dir / "core_peers" / "table.png"
            screenshot.parent.mkdir()
            screenshot.write_bytes(b"fake-image")
            metadata = {
                "captured_at": "2026-09-03T10:00:00",
                "review_date": "2026-09-01",
                "screenshot": "core_peers/table.png",
                "peers": [
                    {
                        "name": "徐工机械",
                        "code": "000425",
                        "type": "核心龙头",
                        "daily_bar": {"change_pct": "4.44"},
                    }
                ],
            }
            peers = [{"name": "徐工机械", "code": "000425", "type": "核心龙头"}]

            self.assertTrue(
                review.peer_metadata_matches(
                    metadata, peers, stock_dir, "2026-09-01"
                )
            )
            self.assertFalse(
                review.peer_metadata_matches(
                    metadata, peers, stock_dir, "2026-09-02"
                )
            )
            peer_images = review.collect_peer_images(stock_dir, metadata, strict=True)

        self.assertEqual(len(peer_images), 1)
        self.assertIn("交易日：2026-09-01", peer_images[0]["description"])

        stock = {"name": "示例股票", "code": "000157"}
        image = {
            "description": "核心个股表",
            "mime_type": "image/png",
            "data": "",
        }
        parts = build_prompt_parts(
            TEMPLATE,
            stock,
            [image],
            [image],
            None,
            {"boards": {"核心板块": {"name": "工程机械整机", "code": "BK1393"}}},
            "2026-09-01",
            "未启用联网检索",
            None,
            peer_images,
            metadata,
        )
        texts = [part["text"] for part in parts if "text" in part]

        self.assertTrue(
            any("板块龙头/核心个股候选" in text for text in texts)
        )
        self.assertTrue(
            any("徐工机械 / 000425：核心龙头" in text for text in texts)
        )

    def test_zhipu_search_queries_cover_stock_and_three_boards(self) -> None:
        stock = {"name": "示例股票", "code": "000157"}
        metadata = {
            "boards": {
                "一级行业": {"name": "机械设备", "code": "BK1205"},
                "核心板块": {"name": "工程机械整机", "code": "BK1393"},
                "细分方向": {"name": "工程机械", "code": "BK0739"},
            }
        }

        queries = zhipu_search_queries(stock, metadata, "2026-09-01")

        self.assertTrue(any("财报" in query for query in queries))
        self.assertTrue(any("重大事件" in query for query in queries))
        joined_queries = "\n".join(queries)
        self.assertTrue(
            all(board["code"] in joined_queries for board in metadata["boards"].values())
        )
        self.assertTrue(any("上涨 下跌 原因" in query for query in queries))

    def test_external_search_evidence_and_sources_are_formatted(self) -> None:
        grouped = [
            {
                "query": "测试查询",
                "items": [
                    {
                        "title": "结果A",
                        "link": "https://example.com/a",
                        "media": "示例",
                        "publish_date": "2026-09-01",
                        "content": "摘要内容",
                    },
                    {
                        "title": "结果B",
                        "link": "https://example.com/b",
                        "media": "示例",
                        "publish_date": "2026-09-01",
                        "content": "摘要内容B",
                    },
                ],
            }
        ]

        evidence = format_zhipu_search_evidence(grouped, ["失败查询：测试"])
        review = append_external_search_sources("复盘", grouped)

        self.assertIn("#### 查询：测试查询", evidence)
        self.assertIn("[结果A](https://example.com/a)", evidence)
        self.assertIn("#### 未成功完成的查询", evidence)
        self.assertIn("### 智谱 Web Search 来源（search_std，自动附加）", review)
        self.assertIn("[结果B](https://example.com/b)", review)

    def test_zhipu_sources_are_appended_and_deduplicated(self) -> None:
        response = {
            "web_search": [
                {
                    "title": "来源A",
                    "link": "https://example.com/a",
                    "media": "示例",
                    "publish_date": "2026-09-01",
                },
                {
                    "title": "来源A重复",
                    "link": "https://example.com/a",
                },
            ]
        }

        result = append_zhipu_sources("复盘", response)

        self.assertIn("### 智谱检索来源（自动附加）", result)
        self.assertEqual(result.count("https://example.com/a"), 1)

    def test_provider_and_model_resolution_prefers_gemini_for_vision(self) -> None:
        original_gemini = os.environ.get("GEMINI_API_KEY")
        original = os.environ.get("ZHIPU_API_KEY")
        os.environ["GEMINI_API_KEY"] = "test-gemini-key"
        os.environ["ZHIPU_API_KEY"] = "test-key"
        try:
            self.assertEqual(resolve_provider("auto"), "gemini")
            self.assertEqual(resolve_provider("auto", "gemini-3.7-flash"), "gemini")
            self.assertEqual(resolve_model(None, "zhipu"), "glm-5.3-flash")
            self.assertEqual(resolve_model(None, "gemini"), "gemini-3.7-flash")
        finally:
            if original is None:
                os.environ.pop("ZHIPU_API_KEY", None)
            else:
                os.environ["ZHIPU_API_KEY"] = original
            if original_gemini is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = original_gemini

    def test_search_provider_resolution_prefers_zhipu_key(self) -> None:
        original = os.environ.get("ZHIPU_API_KEY")
        os.environ["ZHIPU_API_KEY"] = "test-key"
        try:
            self.assertEqual(resolve_search_provider("auto", "gemini"), "zhipu")
            self.assertEqual(resolve_search_provider("model", "gemini"), "model")
            self.assertEqual(
                resolve_search_provider("auto", "gemini", disabled=True), "none"
            )
            self.assertEqual(
                search_tool_name("zhipu", "gemini"),
                "智谱 Web Search（search_std）",
            )
        finally:
            if original is None:
                os.environ.pop("ZHIPU_API_KEY", None)
            else:
                os.environ["ZHIPU_API_KEY"] = original
    def test_board_flow_snapshot_is_audited_but_not_sent_to_gemini(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stock_dir = Path(directory)
            screenshots = {
                "trading_data": "trading.png",
                "intraday_chart": "intraday.png",
                "daily_kline": "kline.png",
                "flow_and_members": "flow.png",
            }
            for filename in screenshots.values():
                (stock_dir / filename).write_bytes(b"fake")
            metadata = {
                "boards": {
                    "一级行业": {
                        "code": "BK1205",
                        "name": "机械设备",
                        "screenshots": screenshots,
                    }
                }
            }

            images = collect_board_images(stock_dir, metadata, strict=True)

        self.assertEqual(len(images), 3)
        self.assertNotIn(
            "板块资金流与成分股涨跌幅",
            [image["description"] for image in images],
        )

    def test_grounding_sources_are_deduplicated(self) -> None:
        source = type("Web", (), {"uri": "https://example.com", "title": "示例"})()
        chunk = type("Chunk", (), {"web": source})()
        metadata = type("Metadata", (), {"grounding_chunks": [chunk, chunk]})()
        candidate = type("Candidate", (), {"grounding_metadata": metadata})()
        response = type("Response", (), {"candidates": [candidate]})()

        self.assertEqual(
            grounding_sources(response), [("示例", "https://example.com")]
        )

    def test_parse_portfolio_and_escape_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            portfolio_file = Path(directory) / "my_stock.txt"
            portfolio_file.write_text("测试|股票,10.00,100,1年\n", encoding="utf-8")
            holding = portfolio.parse_portfolio(portfolio_file)[0]

        self.assertEqual(holding.name, "测试|股票")
        self.assertEqual(portfolio.markdown_cell(holding.name), "测试\\|股票")

    def test_metadata_match_does_not_self_match_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata_path = Path(directory) / "metadata.json"
            metadata_path.write_text(
                json.dumps(
                    {"query": "示例股票", "resolved_stock": {"code": "000157"}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertTrue(metadata_matches_query(metadata_path, "SZ000157"))
            self.assertFalse(metadata_matches_query(metadata_path, "SH600519"))

    def test_complete_capture_is_reused_without_recapture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            date_root = Path(directory)
            stock_dir = date_root / "示例股票_000157"
            stock_dir.mkdir()
            holding = portfolio.Holding("示例股票", "10.00", "100", "1年")

            def fake_reusable(root, item, seen_codes):
                return stock_dir, {"name": "示例股票", "code": "000157"}

            def fail_resolve(*args, **kwargs):
                raise AssertionError("完整同日截图不应重新解析股票")

            original_reusable = portfolio.find_reusable_stock_dir
            original_resolve = portfolio.resolve_stock
            portfolio.find_reusable_stock_dir = fake_reusable
            portfolio.resolve_stock = fail_resolve
            try:
                result = portfolio.prepare_stock_dir(
                    date_root,
                    holding,
                    timeout=10.0,
                    recapture=False,
                    seen_codes=set(),
                )
            finally:
                portfolio.find_reusable_stock_dir = original_reusable
                portfolio.resolve_stock = original_resolve

        self.assertEqual(result, (stock_dir, {"name": "示例股票", "code": "000157"}))

    def test_explicit_board_overrides_existing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stock_dir = Path(directory)
            payload = {
                "boards": {
                    "旧板块": {
                        "code": "BK0001",
                        "name": "旧板块",
                        "screenshots": {"trading_data": "old.png"},
                    }
                }
            }
            (stock_dir / "boards_metadata.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            args = argparse.Namespace(
                board=[
                    "一级行业:BK1111",
                    "核心板块:BK9999",
                    "细分方向:BK2222",
                ],
                timeout=10.0,
            )
            captured = []

            def fake_capture(path, boards, args):
                captured.append(list(boards))
                return {"boards": {}}

            original = portfolio.capture_boards
            portfolio.capture_boards = fake_capture
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    portfolio.capture_or_reuse_boards(
                        stock_dir,
                        {"name": "测试", "code": "000000", "quote_id": "0.000000"},
                        args,
                    )
            finally:
                portfolio.capture_boards = original

        self.assertEqual(
            captured,
            [[("一级行业", "BK1111"), ("核心板块", "BK9999"), ("细分方向", "BK2222")]],
        )

    def test_strict_board_validation_requires_three_boards_and_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stock_dir = Path(directory)
            boards = {}
            for index, label in enumerate(("一级行业", "核心板块", "细分方向")):
                screenshots = {
                    key: f"{index}_{key}.png"
                    for key in ("trading_data", "intraday_chart", "daily_kline")
                }
                for filename in screenshots.values():
                    (stock_dir / filename).touch()
                boards[label] = {
                    "code": f"BK000{index}",
                    "name": label,
                    "screenshots": screenshots,
                }
            metadata = {"boards": boards}

            with self.assertRaisesRegex(ReviewError, "一级行业/flow_and_members"):
                collect_board_images(stock_dir, metadata, strict=True)

            boards["一级行业"]["screenshots"]["flow_and_members"] = "flow.png"
            (stock_dir / "flow.png").touch()
            del boards["细分方向"]
            with self.assertRaisesRegex(ReviewError, "板块截图不完整"):
                validate_reused_boards(metadata, [])


if __name__ == "__main__":
    unittest.main()
