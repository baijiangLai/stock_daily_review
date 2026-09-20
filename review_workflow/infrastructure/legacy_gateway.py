"""Adapter connecting the workflow to the existing Eastmoney/AI scripts."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import portfolio_daily_review as portfolio
from eastmoney_auth import load_auth_state

from review_workflow.domain.config import WorkflowConfig

from ..application.ports import AnalysisResult, CaptureResult, RuntimeInfo
from .local_review import (
    generate_local_stock_review,
    generate_portfolio_summary as generate_local_portfolio_summary,
)
from .weekly_strategy import review_weekly_strategy


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT_ROOT = PROJECT_ROOT / "screenshots"


class LegacyPortfolioGateway:
    """Adapt legacy procedural scripts to the application port."""

    def parse_review_date(self, value: str) -> date:
        return portfolio.parse_review_date(value)

    def load_holdings(self, config: WorkflowConfig) -> List[Dict[str, Any]]:
        if config.holdings:
            return [dict(holding) for holding in config.holdings]
        return [
            holding.as_dict()
            for holding in portfolio.parse_portfolio(config.portfolio_file)
        ]

    def capture(
        self,
        holding: Mapping[str, str],
        config: WorkflowConfig,
        seen_codes: Iterable[str],
    ) -> CaptureResult:
        args = self._legacy_args(config)
        if not config.skip_capture and load_auth_state(args.auth_state) is None:
            raise RuntimeError("未找到东方财富登录状态，请先执行 capture_eastmoney.py --login。")

        holding_value = portfolio.Holding(**dict(holding))
        stock_dir, resolved_stock = portfolio.prepare_stock_dir(
            self._date_root(config.review_date),
            holding_value,
            config.timeout,
            config.recapture,
            set(seen_codes),
            config.skip_capture,
        )
        if not stock_dir.is_dir():
            portfolio.capture_holding(
                holding_value,
                stock_dir,
                self.parse_review_date(config.review_date),
                args,
            )
        else:
            portfolio.ensure_holding_metadata(
                stock_dir,
                holding_value,
                self.parse_review_date(config.review_date),
            )
            if not config.skip_capture:
                portfolio.ensure_weekly_kline_screenshot(
                    stock_dir,
                    resolved_stock,
                    args,
                    config.review_date,
                )
        portfolio.capture_or_reuse_boards(stock_dir, resolved_stock, args)
        return CaptureResult(stock_dir=stock_dir, resolved_stock=resolved_stock)

    def analyze(
        self,
        holding: Mapping[str, str],
        stock_dir: Path,
        config: WorkflowConfig,
    ) -> AnalysisResult:
        if config.provider == "local":
            _, review_path = generate_local_stock_review(
                holding, stock_dir, config.to_dict()
            )
            stock = json.loads((stock_dir / "metadata.json").read_text(encoding="utf-8"))[
                "resolved_stock"
            ]
            individual_count = sum(
                path.is_file()
                for path in (
                    stock_dir / "01_trading_data.png",
                    stock_dir / "02_intraday_chart.png",
                    stock_dir / "03_bid_ask_5.png",
                    stock_dir / "04_daily_kline.png",
                    stock_dir / "04_weekly_kline.png",
                )
            )
            boards = json.loads(
                (stock_dir / "boards_metadata.json").read_text(encoding="utf-8")
            ).get("boards", {})
            board_count = sum(
                len(info.get("screenshots", {}))
                for info in boards.values()
                if isinstance(info, dict)
            )
            return AnalysisResult(
                stock=stock,
                individual_images=individual_count,
                board_images=board_count,
                review_path=review_path,
            )

        template = config.template_file.read_text(encoding="utf-8")
        stock, individual_images, board_images, _ = portfolio.review_one_stock(
            portfolio.Holding(**dict(holding)),
            stock_dir,
            template,
            self._legacy_args(config),
        )
        provider = portfolio.resolve_provider(config.provider, config.model)
        review_path = stock_dir / f"{provider}当日复盘.md"
        if not review_path.is_file():
            raise RuntimeError(f"模型已返回，但未找到个股复盘文件：{review_path}")
        return AnalysisResult(
            stock=stock,
            individual_images=len(individual_images),
            board_images=board_images,
            review_path=review_path,
        )

    def summarize(
        self,
        review_date: str,
        reviews: Sequence[Mapping[str, Any]],
        config: WorkflowConfig,
        ) -> str:
        if config.provider == "local":
            self._ensure_weekly_charts(review_date, reviews, config)
            weekly_strategy, _, _ = review_weekly_strategy(review_date, reviews)
            daily_summary = generate_local_portfolio_summary(
                review_date, reviews, config.timeout
            )
            return (
                weekly_strategy.rstrip()
                + "\n\n---\n\n"
                + daily_summary.rstrip()
            )

        legacy_reviews = []
        for item in reviews:
            legacy_reviews.append(
                (
                    portfolio.Holding(**dict(item["holding"])),
                    dict(item.get("stock") or {}),
                    str(item.get("content", "")),
                )
            )
        prompt = portfolio.portfolio_summary_prompt(
            self.parse_review_date(review_date),
            legacy_reviews,
        )
        provider = portfolio.resolve_provider(config.provider, config.model)
        return portfolio.call_model(
            [{"text": prompt}],
            config.model,
            config.timeout,
            provider,
            enable_search=False,
        )

    def runtime_info(self, config: WorkflowConfig) -> RuntimeInfo:
        if config.provider == "local":
            date_root = self._date_root(config.review_date)
            output_path = (
                config.output_file
                if config.output_file
                else portfolio.default_daily_output(
                    date_root,
                    self.parse_review_date(config.review_date),
                )
            )
            return RuntimeInfo(
                provider="local",
                model="local-rule-engine-v1",
                search_provider="none" if config.no_web_search else "eastmoney-public-api",
                date_root=date_root,
                output_path=output_path,
            )

        provider = portfolio.resolve_provider(config.provider, config.model)
        model = portfolio.resolve_model(config.model, provider)
        search_provider = portfolio.resolve_search_provider(
            config.search_provider,
            provider,
            config.no_web_search,
        )
        date_root = self._date_root(config.review_date)
        output_path = (
            config.output_file
            if config.output_file
            else portfolio.default_daily_output(
                date_root,
                self.parse_review_date(config.review_date),
            )
        )
        return RuntimeInfo(
            provider=provider,
            model=model,
            search_provider=search_provider,
            date_root=date_root,
            output_path=output_path,
        )

    def _ensure_weekly_charts(
        self,
        review_date: str,
        reviews: Sequence[Mapping[str, Any]],
        config: WorkflowConfig,
    ) -> None:
        should_capture_weekly = (
            not config.skip_capture
            and portfolio.should_capture_weekly_kline(review_date)
        )
        if not should_capture_weekly:
            return
        args = self._legacy_args(config)
        for item in reviews:
            review_path = Path(str(item.get("review_path", "")))
            stock = dict(item.get("stock") or {})
            if not stock:
                try:
                    stock = json.loads(
                        (review_path.parent / "metadata.json").read_text(
                            encoding="utf-8"
                        )
                    )["resolved_stock"]
                except (OSError, ValueError, KeyError):
                    stock = {}
            try:
                portfolio.ensure_weekly_kline_screenshot(
                    review_path.parent, stock, args, review_date
                )
            except Exception as exc:
                print(f"周 K 截图补充失败：{stock.get('name', '')}：{exc}")

    def _date_root(self, review_date: str) -> Path:
        parsed = self.parse_review_date(review_date)
        return SCREENSHOT_ROOT / parsed.strftime("%Y/%m/%d")

    def _legacy_args(self, config: WorkflowConfig) -> argparse.Namespace:
        return argparse.Namespace(
            board=list(config.board),
            peer_stock=list(config.peer_stock),
            timeout=config.timeout,
            device_scale_factor=config.device_scale_factor,
            headed=config.headed,
            auth_state=str(config.auth_state) if config.auth_state else None,
            provider=config.provider,
            model=config.model,
            search_provider=config.search_provider,
            no_web_search=config.no_web_search,
            no_peer_capture=config.no_peer_capture,
            dry_run=False,
            capture_only=False,
            recapture=config.recapture,
            skip_capture=config.skip_capture,
        )
