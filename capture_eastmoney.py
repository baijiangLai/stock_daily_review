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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

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


class LoginRequiredError(CaptureError):
    """Raised when the saved Eastmoney login state is expired or missing."""


class PersistentOcclusionError(CaptureError):
    """Raised when overlays still cover capture targets after hiding."""


LOGIN_DIALOG_SELECTORS = (
    "#login-mask",
    ".login-mask",
    "#passport_login_box",
    ".passport-login",
    ".login_box",
    'iframe[src*="passport.eastmoney.com"]',
    'iframe[src*="login"]',
)

OCCLUSION_PROBE_JS = """
selector => {
    const elements = Array.from(document.querySelectorAll(selector)).filter(
        element => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' &&
                   rect.width > 10 && rect.height > 10;
        }
    );
    if (!elements.length) return null;
    const target = elements[0];
    const rect = target.getBoundingClientRect();
    const probes = [
        [rect.left + rect.width * 0.5, rect.top + rect.height * 0.5],
        [rect.left + rect.width * 0.25, rect.top + rect.height * 0.25],
        [rect.left + rect.width * 0.75, rect.top + rect.height * 0.75],
    ];
    for (const [x, y] of probes) {
        const hit = document.elementFromPoint(x, y);
        if (!hit || hit === target || target.contains(hit) || hit.contains(target)) {
            continue;
        }
        const style = window.getComputedStyle(hit);
        if (style.pointerEvents === 'none' || style.visibility === 'hidden' ||
            style.display === 'none' || Number(style.opacity) === 0) {
            continue;
        }
        hit.style.setProperty('display', 'none', 'important');
        return {
            tag: hit.tagName.toLowerCase(),
            id: hit.id || '',
            className: typeof hit.className === 'string' ? hit.className : '',
        };
    }
    return null;
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="输入股票名称或代码，抓取东方财富个股页截图；周五/周末追加周 K。"
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
    parser.add_argument(
        "--review-date",
        default=date.today().isoformat(),
        help="复盘日期，默认今天；周五/周末会追加周 K 截图",
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


def find_login_dialog(page: Page) -> Optional[str]:
    """Return the selector of the first visible login dialog, if any."""

    for selector in LOGIN_DIALOG_SELECTORS:
        try:
            handles = page.query_selector_all(selector)
        except Exception:  # invalid selector or page navigating
            continue
        for handle in handles:
            try:
                if handle.is_visible():
                    return selector
            except Exception:
                continue
    return None


def ensure_logged_in(page: Page) -> None:
    """Fail fast when the saved login state has expired and a popup appeared.

    截图前先确认没有登录弹窗：登录过期时东方财富会弹出登录框/遮罩，
    继续截图只会得到带弹窗的废图（买卖五档也需要登录后才展示）。
    """

    dialog = find_login_dialog(page)
    if dialog is None:
        return
    raise LoginRequiredError(
        "东方财富登录态已过期：页面出现登录弹窗"
        f"（匹配到 {dialog}）。请先执行 "
        "python capture_eastmoney.py --login 重新登录后再截图。"
    )


def describe_occluder(occluder: Optional[Dict[str, Any]]) -> str:
    if not occluder:
        return "未知浮层"
    parts = [str(occluder.get("tag") or "element")]
    if occluder.get("id"):
        parts.append(f"#{occluder['id']}")
    class_name = str(occluder.get("className") or "").strip()
    if class_name:
        parts.append(f".{'.'.join(class_name.split()[:3])}")
    return "".join(parts)


def clear_occlusions(
    page: Page,
    selectors: Iterable[str],
    *,
    attempts: int = 3,
) -> None:
    """Hide overlays covering the capture targets, or fail with details.

    每次截图前对目标元素做命中测试：发现浮层先自动隐藏并复测，
    仍被遮挡则报错，避免产出被弹窗/广告盖住的截图。
    """

    selector_list = tuple(selectors)
    last_occluder: Optional[Dict[str, Any]] = None
    for _ in range(max(attempts, 1)):
        # 登录弹窗不允许静默隐藏：登录过期时必须报错重新登录，
        # 否则隐藏遮罩后截到的仍是未登录状态的废图。
        dialog = find_login_dialog(page)
        if dialog:
            raise LoginRequiredError(
                "截图前检测到登录弹窗（匹配到 "
                f"{dialog}），登录态可能已过期。请先执行 "
                "python capture_eastmoney.py --login 重新登录后再截图。"
            )
        hide_interference(page)
        found: Optional[Dict[str, Any]] = None
        for selector in selector_list:
            try:
                occluder = page.evaluate(OCCLUSION_PROBE_JS, selector)
            except Exception:
                occluder = None
            if occluder:
                found = occluder
                break
        if found is None:
            return
        last_occluder = found
        print(f"  已隐藏遮挡截图的浮层：{describe_occluder(last_occluder)}")
    raise PersistentOcclusionError(
        "截图区域仍被浮层遮挡（"
        f"{describe_occluder(last_occluder)}），自动隐藏失败。"
        "请确认页面无弹窗后重试；若登录弹窗持续出现，"
        "说明登录态已过期，请重新执行 python capture_eastmoney.py --login。"
    )


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
        if find_login_dialog(page):
            raise LoginRequiredError(
                "买卖五档加载超时：页面出现登录弹窗，登录态可能已过期。"
                "请先执行 python capture_eastmoney.py --login 重新登录。"
            ) from exc
        raise CaptureError("买卖五档数据加载超时") from exc
    first_visible(page, [".quote_title", ".zsquote3l", ".sider_quote_price"])


def should_capture_weekly_kline(review_date: Optional[str]) -> bool:
    if not review_date:
        return False
    try:
        parsed = date.fromisoformat(review_date)
    except ValueError:
        return False
    return parsed.weekday() >= 4  # Friday, Saturday, or Sunday


def switch_to_weekly_kline(page: Page, timeout: float) -> None:
    try:
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('a')).some(link => {
                const text = (link.innerText || '').trim();
                return text === '周K';
            })""",
            timeout=timeout * 1000,
        )
        page.evaluate(
            """() => {
                const links = Array.from(document.querySelectorAll('a'));
                const weekly = links.find(link => (link.innerText || '').trim() === '周K');
                if (!weekly) return false;
                for (const type of ['mouseover', 'mousedown', 'mouseup']) {
                    weekly.dispatchEvent(new MouseEvent(type, {
                        bubbles: true,
                        view: window,
                    }));
                }
                weekly.click();
                return true;
            }"""
        )
        page.wait_for_function(
            """() => Array.from(document.querySelectorAll('a')).some(link => {
                const text = (link.innerText || '').trim();
                return text === '周K' && link.classList.contains('active');
            })""",
            timeout=timeout * 1000,
        )
    except PlaywrightTimeoutError as exc:
        raise CaptureError("周 K 线切换超时") from exc


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


def reload_and_settle(
    page: Page,
    timeout: float,
    prepare: Optional[Callable[[Page], None]] = None,
) -> None:
    """重新加载页面并恢复就绪状态，用于遮挡无法清除时重抓。"""

    page.reload(wait_until="domcontentloaded", timeout=timeout * 1000)
    if prepare is not None:
        prepare(page)
    else:
        page.wait_for_timeout(1500)


def capture_with_recapture(
    capture: Callable[[], None],
    page: Page,
    timeout: float,
    *,
    prepare: Optional[Callable[[Page], None]] = None,
    reloads: int = 2,
) -> None:
    """执行截图；持续遮挡时重新加载页面后重抓，而不是带遮挡硬截。

    登录过期（LoginRequiredError）不属于遮挡，直接向上抛出，不做重抓。
    """

    last_error: Optional[PersistentOcclusionError] = None
    for attempt in range(max(reloads, 0) + 1):
        try:
            capture()
            return
        except PersistentOcclusionError as exc:
            last_error = exc
            if attempt < reloads:
                print(
                    "  截图区域仍被遮挡，重新加载页面后重抓"
                    f"（第 {attempt + 1}/{reloads} 次）…"
                )
                reload_and_settle(page, timeout, prepare)
    assert last_error is not None
    raise last_error


def capture_area(
    page: Page,
    selectors: Iterable[str],
    path: Path,
    timeout: float,
    *,
    wait_chart: bool = False,
    prepare: Optional[Callable[[Page], None]] = None,
) -> None:
    selector_list = tuple(selectors)

    def capture() -> None:
        if wait_chart:
            wait_for_chart(page, selector_list, timeout)
        clear_occlusions(page, selector_list)
        element = first_visible(page, selector_list)
        element.scroll_into_view_if_needed(timeout=timeout * 1000)
        page.wait_for_timeout(800)
        # 浮层可能在检查后、截图前的等待窗口内弹出（如 APP 推广弹窗），
        # 截图前一刻再复测一次，发现遮挡就走重抓流程。
        clear_occlusions(page, selector_list)
        element.screenshot(path=path, timeout=timeout * 1000)

    capture_with_recapture(capture, page, timeout, prepare=prepare)


def capture_union(
    page: Page,
    selectors: Iterable[str],
    path: Path,
    timeout: float,
    *,
    prepare: Optional[Callable[[Page], None]] = None,
) -> None:
    selector_list = tuple(selectors)

    def capture() -> None:
        clear_occlusions(page, selector_list)
        boxes = []
        for selector in selector_list:
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
        # 与 capture_area 相同：截图前一刻复测遮挡，防止浮层在等待窗口内弹出。
        clear_occlusions(page, selector_list)
        page.screenshot(path=path, clip=clip, timeout=timeout * 1000)

    capture_with_recapture(capture, page, timeout, prepare=prepare)


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
            try:
                wait_for_ready(page, attempt_timeout)
            except PlaywrightTimeoutError as exc:
                if find_login_dialog(page):
                    raise LoginRequiredError(
                        "行情页加载超时：页面出现登录弹窗，登录态可能已过期。"
                        "请先执行 python capture_eastmoney.py --login 重新登录。"
                    ) from exc
                raise
            ensure_logged_in(page)
            wait_for_capture_targets(page, attempt_timeout)
            return context, page
        except LoginRequiredError:
            context.close()
            raise  # 登录态过期重试无效，直接提示重新登录
        except Exception as exc:
            last_error = exc
            context.close()
    raise CaptureError(f"打开或加载东方财富个股页失败：{last_error}")


def ensure_weekly_kline_screenshot(
    stock_dir: Path,
    stock: Dict[str, str],
    args: argparse.Namespace,
    review_date: Optional[str] = None,
) -> bool:
    """Add the weekly K-line snapshot used by Friday/weekend weekly plans."""

    metadata_path = stock_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaptureError(f"读取截图元数据失败：{exc}") from exc
    screenshots = metadata.get("screenshots")
    if not isinstance(screenshots, dict):
        screenshots = {}
        metadata["screenshots"] = screenshots

    filename = str(screenshots.get("weekly_kline") or "04_weekly_kline.png")
    output_path = stock_dir / filename
    effective_review_date = review_date or str(metadata.get("review_date") or "")
    if output_path.is_file() or not should_capture_weekly_kline(effective_review_date):
        return output_path.is_file()

    timeout_seconds = max(args.timeout, 5.0)
    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=not args.headed,
            )
            context, page = open_stock_page(browser, stock, args, timeout_seconds)
            switch_to_weekly_kline(page, timeout_seconds)
            page.wait_for_timeout(1000)
            capture_area(
                page,
                [".k_chart", ".kchart_d", "#emchartk"],
                output_path,
                timeout_seconds,
                wait_chart=True,
            )
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()

    screenshots["weekly_kline"] = filename
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


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
    weekly_due = should_capture_weekly_kline(review_date)
    if weekly_due:
        screenshots["weekly_kline"] = "04_weekly_kline.png"

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
            if weekly_due:
                switch_to_weekly_kline(page, timeout_seconds)
                page.wait_for_timeout(1000)
                capture_area(
                    page,
                    [".k_chart", ".kchart_d", "#emchartk"],
                    output_dir / screenshots["weekly_kline"],
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
    weekly_due = should_capture_weekly_kline(args.review_date)
    for filename in (
        "01_trading_data.png",
        "02_intraday_chart.png",
        "03_bid_ask_5.png",
        "04_daily_kline.png",
        "04_weekly_kline.png",
    ):
        if filename == "04_weekly_kline.png" and not weekly_due:
            continue
        print(f"  - {output_dir / filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
