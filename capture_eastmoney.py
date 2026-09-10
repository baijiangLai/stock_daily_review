#!/usr/bin/env python3
"""Capture stock information screenshots from Eastmoney's browser UI."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from eastmoney_auth import (
    AuthStateError,
    create_eastmoney_context,
    load_auth_state,
    login_and_save,
    resolve_auth_state,
)


SEARCH_API = "https://searchadapter.eastmoney.com/api/suggest/get"
SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class CaptureError(RuntimeError):
    """Raised when stock resolution or screenshot capture fails."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="输入股票名称或代码，抓取东方财富个股页的四张截图。"
    )
    parser.add_argument(
        "stock",
        nargs="?",
        help="股票名称或代码，例如：贵州茅台 / 600519；使用 --login 时可省略",
    )
    parser.add_argument("-o", "--output", default="screenshots", help="截图根目录")
    parser.add_argument(
        "-t", "--timeout", type=float, default=45.0, help="页面元素最长等待秒数"
    )
    parser.add_argument(
        "--headed", action="store_true", help="显示浏览器窗口（默认无头模式）"
    )
    parser.add_argument(
        "--device-scale-factor", type=float, default=2.0, help="截图分辨率倍数"
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="打开浏览器，手动登录东方财富并保存登录状态",
    )
    parser.add_argument(
        "--auth-state",
        help=f"登录状态文件，默认 {resolve_auth_state(None)}",
    )
    return parser.parse_args()


def request_json(url: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            payload = json.loads(response.read().decode(charset))
    except (OSError, ValueError) as exc:
        raise CaptureError(f"东方财富搜索接口请求失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise CaptureError("东方财富搜索接口返回格式异常")
    return payload


def resolve_stock(query: str, timeout: float) -> Dict[str, str]:
    payload = request_json(
        SEARCH_API,
        {
            "input": query,
            "type": 14,
            "token": SEARCH_TOKEN,
            "count": 20,
        },
        timeout,
    )
    table = payload.get("QuotationCodeTable", {})
    records = [record for record in table.get("Data") or [] if isinstance(record, dict)]
    if not records:
        raise CaptureError(f"东方财富没有找到与 “{query}” 匹配的股票")

    def record_rank(record: Dict[str, Any]) -> tuple:
        code = str(record.get("Code", ""))
        unified_code = str(record.get("UnifiedCode", ""))
        name = str(record.get("Name", ""))
        exact_code = query.isdigit() and query in {code, unified_code}
        exact_name = query == name
        stock_class = str(record.get("Classify", "")) == "AStock"
        return (not exact_code, not exact_name, not stock_class)

    candidates = sorted(records, key=record_rank)
    selected = candidates[0]
    quote_id = str(selected.get("QuoteID", "")).strip()
    if not quote_id:
        raise CaptureError(f"“{query}” 的搜索结果缺少 QuoteID，无法打开行情页")
    return {
        "code": str(selected.get("Code", "")),
        "name": str(selected.get("Name", "")),
        "market": str(selected.get("SecurityTypeName", "")),
        "quote_id": quote_id,
        "url": f"https://quote.eastmoney.com/unify/r/{quote_id}",
    }


def slugify(text: str) -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text, flags=re.UNICODE).strip("_")
    return safe or "stock"


def first_visible(page: Page, selectors: Iterable[str]) -> Locator:
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() and locator.is_visible():
            return locator
    raise CaptureError("未找到可截图的页面元素")


def wait_for_ready(page: Page, timeout: float) -> None:
    page.wait_for_selector(".quote_title_name", timeout=timeout * 1000)
    try:
        page.wait_for_function(
            """() => {
                const price = document.querySelector('.quote_quotenums .zxj');
                const name = document.querySelector('.quote_title_name');
                return name && name.textContent.trim() &&
                       price && price.textContent.trim() !== '' &&
                       price.textContent.trim() !== '-';
            }""",
            timeout=timeout * 1000,
        )
    except PlaywrightTimeoutError as exc:
        raise CaptureError("交易数据加载超时，行情接口未返回最新价格") from exc


def wait_for_chart(page: Page, selectors: Iterable[str], timeout: float) -> None:
    try:
        page.wait_for_function(
            """selectors => {
                return selectors.some(selector => {
                    const root = document.querySelector(selector);
                    if (!root) return false;
                    const visible = root.querySelector('canvas, svg');
                    return Boolean(visible && visible.getBoundingClientRect().width > 10);
                });
            }""",
            arg=tuple(selectors),
            timeout=timeout * 1000,
        )
    except PlaywrightTimeoutError as exc:
        raise CaptureError(f"图表渲染超时：{', '.join(selectors)}") from exc


def wait_for_capture_targets(page: Page, timeout: float) -> None:
    wait_for_chart(page, (".time_chart", ".timessechart"), timeout)
    wait_for_chart(page, (".k_chart", ".kchart_d", "#emchartk"), timeout)
    try:
        page.wait_for_function(
            """() => {
                const value = document.querySelector(
                    '.sider_quote_price tr td:nth-child(2)'
                );
                return value && value.textContent.trim() !== '' &&
                       value.textContent.trim() !== '-';
            }""",
            timeout=timeout * 1000,
        )
    except PlaywrightTimeoutError as exc:
        raise CaptureError("买卖五档数据加载超时") from exc
    first_visible(page, [".quote_title", ".zsquote3l", ".sider_quote_price"])


def hide_interference(page: Page) -> None:
    interference_selectors = [
        "#em-window-ads",
        ".fixed-fullscreen",
        ".popups",
        ".dialog",
        ".mask",
        "#login-mask",
    ]
    for selector in interference_selectors:
        page.eval_on_selector_all(
            selector,
            "elements => elements.forEach(element => element.style.display = 'none')",
        )


def capture_area(
    page: Page,
    selectors: Iterable[str],
    path: Path,
    timeout: float,
    *,
    wait_chart: bool = False,
) -> None:
    if wait_chart:
        wait_for_chart(page, selectors, timeout)
    hide_interference(page)
    element = first_visible(page, selectors)
    element.scroll_into_view_if_needed(timeout=timeout * 1000)
    page.wait_for_timeout(800)
    element.screenshot(path=path, timeout=timeout * 1000)


def capture_union(
    page: Page,
    selectors: Iterable[str],
    path: Path,
    timeout: float,
) -> None:
    hide_interference(page)
    boxes = []
    for selector in selectors:
        element = first_visible(page, [selector])
        box = element.bounding_box()
        if box is None:
            raise CaptureError(f"元素没有可截图区域：{selector}")
        boxes.append(box)

    padding = 8
    clip = {
        "x": min(box["x"] for box in boxes) - padding,
        "y": min(box["y"] for box in boxes) - padding,
        "width": max(box["x"] + box["width"] for box in boxes)
        - min(box["x"] for box in boxes)
        + padding * 2,
        "height": max(box["y"] + box["height"] for box in boxes)
        - min(box["y"] for box in boxes)
        + padding * 2,
    }
    page.screenshot(path=path, clip=clip, timeout=timeout * 1000)


def write_metadata(
    path: Path,
    query: str,
    stock: Dict[str, str],
    screenshots: Dict[str, str],
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    data = {
        "query": query,
        "resolved_stock": stock,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source": "https://quote.eastmoney.com/",
        "screenshots": screenshots,
    }
    if extra_metadata:
        data.update(extra_metadata)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def open_stock_page(
    browser: Any,
    stock: Dict[str, str],
    args: argparse.Namespace,
    timeout: float,
) -> tuple[Any, Page]:
    retry_plan = ((15.0, False), (timeout, True))
    last_error: Optional[Exception] = None
    for attempt_timeout, use_delay_fallback in retry_plan:
        context = create_eastmoney_context(
            browser,
            args.auth_state,
            device_scale_factor=args.device_scale_factor,
        )
        page = context.new_page()
        page.set_default_timeout(timeout * 1000)
        if use_delay_fallback:
            def rewrite_to_delay_host(route: Any) -> None:
                parts = urllib.parse.urlsplit(route.request.url)
                fallback_url = urllib.parse.urlunsplit(
                    parts._replace(netloc="push2delay.eastmoney.com")
                )
                route.continue_(url=fallback_url)

            page.route("**://push2.eastmoney.com/**", rewrite_to_delay_host)
        try:
            page.goto(stock["url"], wait_until="domcontentloaded")
            wait_for_ready(page, attempt_timeout)
            wait_for_capture_targets(page, attempt_timeout)
            return context, page
        except Exception as exc:
            last_error = exc
            context.close()
    raise CaptureError(f"打开或加载东方财富个股页失败：{last_error}")


def run(args: argparse.Namespace, output_dir: Optional[Path] = None) -> Path:
    timeout_seconds = max(args.timeout, 5.0)
    review_date = getattr(args, "review_date", None)
    if args.login:
        if args.stock:
            raise CaptureError("使用 --login 时不需要提供股票参数")
        return login_and_save(args.auth_state, timeout_seconds)
    if not args.stock:
        raise CaptureError("请输入股票名称或代码，或使用 --login 先登录东方财富")
    if load_auth_state(args.auth_state) is None:
        raise CaptureError(
            f"未找到东方财富登录状态（{resolve_auth_state(args.auth_state)}），"
            "请先执行：python capture_eastmoney.py --login"
        )

    query = args.stock.strip()
    code_match = re.fullmatch(r"(?:sh|sz|bj)?(\d{6})", query, flags=re.IGNORECASE)
    search_query = code_match.group(1) if code_match else query
    stock = resolve_stock(search_query, timeout_seconds)
    if output_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = (
            Path(args.output).expanduser()
            / f"{slugify(stock['name'])}_{stock['code']}_{timestamp}"
        ).resolve()
    else:
        output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)

    screenshots: Dict[str, str] = {
        "trading_data": "01_trading_data.png",
        "intraday_chart": "02_intraday_chart.png",
        "bid_ask_5": "03_bid_ask_5.png",
        "daily_kline": "04_daily_kline.png",
    }

    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=not args.headed,
            )
            context, page = open_stock_page(browser, stock, args, timeout_seconds)

            capture_union(
                page,
                [".quote_title", ".zsquote3l"],
                output_dir / screenshots["trading_data"],
                timeout_seconds,
            )
            capture_area(
                page,
                [".time_chart", ".timessechart"],
                output_dir / screenshots["intraday_chart"],
                timeout_seconds,
                wait_chart=True,
            )
            capture_area(
                page,
                [".sider_quote_price.sider_quote_price2", ".sider_quote_price"],
                output_dir / screenshots["bid_ask_5"],
                timeout_seconds,
            )
            capture_area(
                page,
                [".k_chart", ".kchart_d", "#emchartk"],
                output_dir / screenshots["daily_kline"],
                timeout_seconds,
                wait_chart=True,
            )

            missing = [
                filename
                for filename in screenshots.values()
                if not (output_dir / filename).is_file()
            ]
            if missing:
                raise CaptureError(f"截图未完整生成：{', '.join(missing)}")

            write_metadata(
                output_dir / "metadata.json",
                query,
                stock,
                screenshots,
                {"review_date": review_date} if review_date else None,
            )
        except Exception:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()

    return output_dir


def main() -> int:
    args = parse_args()
    try:
        output_dir = run(args)
    except (CaptureError, AuthStateError, PlaywrightTimeoutError) as exc:
        print(f"截图失败：{exc}", file=sys.stderr)
        return 1
    if args.login:
        print(f"已保存东方财富登录状态：{output_dir}")
        print("后续截图会自动复用该登录态；如登录过期，重新执行 --login 即可。")
        return 0
    print(f"已生成 {args.stock} 的东方财富截图：{output_dir}")
    for filename in (
        "01_trading_data.png",
        "02_intraday_chart.png",
        "03_bid_ask_5.png",
        "04_daily_kline.png",
    ):
        print(f"  - {output_dir / filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
