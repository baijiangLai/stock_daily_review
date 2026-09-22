#!/usr/bin/env python3
"""Capture board screenshots and generate a daily stock review with Gemini."""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import sys
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from capture_eastmoney import (
    CaptureError,
    USER_AGENT,
    capture_area,
    capture_union,
    ensure_logged_in,
    find_login_dialog,
    run as capture_stock_screenshots,
    slugify,
    wait_for_chart,
    LoginRequiredError,
)
from eastmoney_auth import (
    AuthStateError,
    create_eastmoney_context,
    load_auth_state,
    login_and_save,
    resolve_auth_state,
)


BOARD_API = "https://push2delay.eastmoney.com/api/qt/slist/get"
BOARD_MEMBERS_API = "https://push2delay.eastmoney.com/api/qt/clist/get"
BOARD_QUOTE_URL = "https://quote.eastmoney.com/unify/r/90.{code}"
STOCK_KLINE_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
ZHIPU_API_BASE = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_CHAT_API = f"{ZHIPU_API_BASE}/chat/completions"
ZHIPU_WEB_SEARCH_API = f"{ZHIPU_API_BASE}/web_search"
ZHIPU_SEARCH_ENGINE = "search_std"
DEFAULT_GEMINI_MODEL = "gemini-3.7-flash"
DEFAULT_ZHIPU_MODEL = "glm-5.3-flash"
PROVIDERS = ("auto", "zhipu", "gemini")
SEARCH_PROVIDERS = ("auto", "zhipu", "model", "none")
PROJECT_ROOT = Path(__file__).resolve().parent
SCREENSHOT_ROOT = PROJECT_ROOT / "screenshots"
ENV_FILE = PROJECT_ROOT / ".env"
ENV_FILE_KEYS = {
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}
DEFAULT_BOARDS = {
    "平安银行": (
        ("一级行业", "BK1283"),
        ("核心板块", "BK0475"),
        ("细分方向", "BK1610"),
    ),
    "贵州茅台": (
        ("一级行业", "BK0438"),
        ("核心板块", "BK0896"),
        ("细分方向", "BK1277"),
    ),
}
REQUIRED_BOARD_LABELS = ("一级行业", "核心板块", "细分方向")
DEFAULT_PEER_STOCKS = {
    "工程机械": [
        {"name": "三一重工", "code": "600031", "type": "总龙头"},
        {"name": "徐工机械", "code": "000425", "type": "核心龙头"},
        {"name": "中联重科", "code": "000157", "type": "核心龙头"},
        {"name": "柳工", "code": "000528", "type": "核心龙头"},
        {"name": "恒立液压", "code": "601100", "type": "细分龙头"},
        {"name": "浙江鼎力", "code": "603338", "type": "细分龙头"},
        {"name": "杭叉集团", "code": "603298", "type": "细分龙头"},
        {"name": "安徽合力", "code": "600761", "type": "细分龙头"},
        {"name": "山推股份", "code": "000680", "type": "细分龙头"},
    ]
}


class ReviewError(RuntimeError):
    """Raised when board capture or Gemini review generation fails."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="补充东方财富板块截图，并调用 Gemini 或智谱 GLM 生成个股当日复盘。"
    )
    parser.add_argument(
        "stock_dir",
        nargs="?",
        help="股票名称/代码，或既有个股截图目录；--skip-capture 时也可传股票名称/代码。",
    )
    parser.add_argument(
        "--board",
        action="append",
        default=[],
        metavar="标签:板块代码",
        help="必须依次提供一级行业/核心板块/细分方向，例如 一级行业:BK1283；未提供时自动推断。",
    )
    parser.add_argument(
        "--template",
        default=str(PROJECT_ROOT / "个股复盘模板内容.md"),
        help="复盘提示词模板文件",
    )
    parser.add_argument(
        "--output",
        help="复盘 Markdown 输出路径，默认保存到个股截图目录/gemini当日复盘.md",
    )
    parser.add_argument(
        "--model",
        help=(
            "模型名；默认由服务商决定："
            f"智谱 {DEFAULT_ZHIPU_MODEL}，Gemini {DEFAULT_GEMINI_MODEL}"
        ),
    )
    parser.add_argument(
        "--provider",
        choices=PROVIDERS,
        default="auto",
        help="模型服务商；auto 表示优先使用 Gemini，仅有智谱 Key 时使用智谱。",
    )
    parser.add_argument(
        "--search-provider",
        choices=SEARCH_PROVIDERS,
        default="auto",
        help=(
            "动态信息检索来源；auto 表示有 ZHIPU_API_KEY 时优先使用智谱 Web Search，"
            "否则使用模型内置搜索。"
        ),
    )
    parser.add_argument(
        "--no-web-search",
        action="store_true",
        help="不做动态信息检索；检索型字段将明确写“待核实”。",
    )
    parser.add_argument(
        "--peer-stock",
        action="append",
        default=[],
        metavar="名称:代码",
        help="补充板块龙头/核心个股交易数据截图，可重复；未提供时工程机械使用内置候选。",
    )
    parser.add_argument(
        "--no-peer-capture",
        action="store_true",
        help="不抓取板块龙头/核心个股交易数据截图。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="页面、联网搜索与模型 API 超时时间（秒）",
    )
    parser.add_argument(
        "--device-scale-factor",
        type=float,
        default=2.0,
        help="板块截图分辨率倍数",
    )
    parser.add_argument(
        "--auth-state",
        help=f"东方财富登录状态文件，默认 {resolve_auth_state(None)}",
    )
    parser.add_argument(
        "--skip-capture",
        action="store_true",
        help="不补充板块截图，直接使用目录内既有截图调用 Gemini",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只构建提示词与图片清单，不调用模型",
    )
    return parser.parse_args()


def request_json(url: str, timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
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
        raise ReviewError(f"东方财富板块接口请求失败：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ReviewError("东方财富板块接口返回格式异常")
    return payload


def normalize_stock_query(value: str) -> str:
    normalized = value.strip().lower()
    market_code = re.fullmatch(r"[012]\.(\d{6})", normalized)
    prefixed_code = re.fullmatch(r"(?:sh|sz|bj)(\d{6})", normalized)
    if market_code:
        return market_code.group(1)
    if prefixed_code:
        return prefixed_code.group(1)
    return normalized


def metadata_matches_query(metadata_path: Path, query: Optional[str]) -> bool:
    if query is None:
        return True
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(metadata, dict):
        return False
    stock = metadata.get("resolved_stock", {})
    if not isinstance(stock, dict):
        return False
    normalized_query = normalize_stock_query(query)
    values = {
        normalize_stock_query(str(stock.get("name", ""))),
        normalize_stock_query(str(stock.get("code", ""))),
        normalize_stock_query(str(stock.get("quote_id", ""))),
        normalize_stock_query(str(metadata.get("query", ""))),
        normalize_stock_query(metadata_path.parent.name),
    }
    return normalized_query in values


def metadata_review_date(metadata: Dict[str, Any]) -> Optional[str]:
    """Return the intended review date, falling back to capture time."""

    review_date = metadata.get("review_date")
    if review_date:
        return str(review_date)
    captured_at = metadata.get("captured_at")
    return str(captured_at)[:10] if captured_at else None


def find_latest_stock_dir(explicit_dir: Optional[str]) -> Path:
    explicit = explicit_dir.strip() if explicit_dir else None
    if explicit_dir:
        path = Path(explicit_dir).expanduser().resolve()
        if path.is_dir():
            return path

    root = SCREENSHOT_ROOT.resolve()
    candidates = [
        metadata_path.parent
        for metadata_path in root.rglob("metadata.json")
        if metadata_path.is_file()
        and metadata_path.parent.is_dir()
        and "_旧_" not in metadata_path.parent.name
        and metadata_matches_query(metadata_path, explicit)
    ]
    if not candidates:
        suffix = f"（匹配条件：{explicit}）" if explicit else ""
        raise ReviewError(f"未找到个股截图目录{suffix}，请先生成截图或显式传入目录")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_stock_metadata(stock_dir: Path) -> Dict[str, Any]:
    path = stock_dir / "metadata.json"
    if not path.is_file():
        raise ReviewError(f"缺少个股截图元数据：{path}")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewError(f"读取个股截图元数据失败：{exc}") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("resolved_stock"), dict):
        raise ReviewError("个股截图 metadata.json 缺少 resolved_stock 字段")
    if not isinstance(metadata.get("screenshots"), dict):
        raise ReviewError("个股截图 metadata.json 缺少 screenshots 字段")
    return metadata


def load_boards_metadata(stock_dir: Path) -> Dict[str, Any]:
    path = stock_dir / "boards_metadata.json"
    if not path.is_file():
        raise ReviewError(f"缺少板块截图元数据：{path}")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewError(f"读取板块截图元数据失败：{path}") from exc

    boards = metadata.get("boards") if isinstance(metadata, dict) else None
    if not isinstance(boards, dict) or not boards:
        raise ReviewError(f"板块截图元数据缺少 boards 字段：{path}")
    for label, info in boards.items():
        if not isinstance(label, str) or not isinstance(info, dict):
            raise ReviewError(f"板块截图元数据格式无效：{path}")
        if not str(info.get("code", "")).strip():
            raise ReviewError(f"板块“{label}”缺少代码：{path}")
        screenshots = info.get("screenshots")
        if not isinstance(screenshots, dict) or not screenshots:
            raise ReviewError(f"板块“{label}”缺少截图清单：{path}")
        if not all(
            isinstance(filename, str) and filename.strip()
            for filename in screenshots.values()
        ):
            raise ReviewError(f"板块“{label}”截图文件名格式无效：{path}")
    return metadata


def normalize_market_code(code: str) -> str:
    """Convert a stock code to Eastmoney's numeric ``market.code`` secid."""
    normalized = code.lower()
    if re.fullmatch(r"[012]\.\d{6}", normalized):
        return normalized
    if normalized.startswith(("sh.", "sz.", "bj.")):
        prefix, number = normalized.split(".", 1)
        market = "1" if prefix == "sh" else "0"
        return f"{market}.{number}"
    if re.fullmatch(r"(?:sh|sz|bj)\d{6}", normalized):
        prefix = normalized[:2]
        number = code[2:]
        market = "1" if prefix == "sh" else "0"
        return f"{market}.{number}"
    else:
        market = "1" if code.startswith(("6", "9")) else "0"
        number = code
    return f"{market}.{number}"


def stock_secid(stock: Dict[str, str]) -> str:
    """Prefer Eastmoney's quote_id and fall back to code-based normalization."""
    quote_id = str(stock.get("quote_id", "")).strip()
    if re.fullmatch(r"[012]\.\d{6}", quote_id):
        return quote_id
    return normalize_market_code(str(stock.get("code", "")))


def fetch_stock_boards(secid: str, timeout: float) -> List[Dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "spt": 3,
            "secid": secid,
            "fltt": 2,
            "invt": 2,
            "fields": "f12,f14,f2,f3,f4,f5,f6,f104,f105,f128,f136,f140",
            "pn": 1,
            "pz": 100,
            "po": 1,
            "np": 1,
        }
    )
    payload = request_json(f"{BOARD_API}?{query}", timeout)
    raw_records = ((payload.get("data") or {}).get("diff")) or []
    records = [record for record in raw_records if isinstance(record, dict)]
    if not records:
        raise ReviewError("东方财富未返回该个股的所属板块")
    return records


def infer_boards(
    stock: Dict[str, str], secid: str, timeout: float, explicit: Sequence[str]
) -> List[Tuple[str, str]]:
    if explicit:
        return parse_explicit_boards(explicit)

    known = DEFAULT_BOARDS.get(stock.get("name", ""))
    if known:
        return list(known)

    records = fetch_stock_boards(secid, timeout)
    excluded = re.compile(
        r"AH股|HS300|上证|深证|MSCI|富时|标准普尔|融资融券|股通|大盘|权重|百元|破净|价值|风格"
    )
    # Eastmoney returns industry-like boards before regional and thematic
    # boards. Preserve that source order; ranking by daily change would
    # mistakenly promote incidental concepts such as “参股银行”.
    candidates: List[Tuple[str, str]] = []
    for record in records:
        name = str(record.get("f14", ""))
        code = str(record.get("f12", ""))
        if code.startswith("BK") and not excluded.search(name):
            candidates.append((code, name))
    if len(candidates) < 3:
        raise ReviewError(
            "无法可靠推断一级/核心/细分板块，请用 --board 标签:代码 指定三个板块"
        )
    selected = candidates[:3]
    # The source usually places the most relevant specific board first and a
    # broader industry second. Reorder here to match the review template.
    return [
        ("一级行业", selected[1][0]),
        ("核心板块", selected[0][0]),
        ("细分方向", selected[2][0]),
    ]


def parse_explicit_boards(explicit: Sequence[str]) -> List[Tuple[str, str]]:
    boards: List[Tuple[str, str]] = []
    for item in explicit:
        if ":" not in item:
            raise ReviewError(f"板块参数格式应为 标签:板块代码，当前为 {item}")
        label, code = item.split(":", 1)
        label = label.strip()
        code = code.strip().upper()
        if not label or not re.fullmatch(r"BK\d+", code):
            raise ReviewError(f"板块参数无效，当前为 {item}")
        boards.append((label, code))

    labels = tuple(label for label, _ in boards)
    if labels != REQUIRED_BOARD_LABELS:
        raise ReviewError(
            "请按顺序指定三个板块：一级行业:BKxxxx、核心板块:BKxxxx、细分方向:BKxxxx"
        )
    return boards


def validate_reused_boards(
    board_metadata: Optional[Dict[str, Any]], explicit: Sequence[str]
) -> None:
    if not board_metadata or not isinstance(board_metadata.get("boards"), dict):
        raise ReviewError("板块截图元数据缺少 boards 字段")

    boards = board_metadata["boards"]
    actual = tuple(boards.keys())
    if not all(isinstance(info, dict) for info in boards.values()):
        raise ReviewError("板块截图元数据格式无效")
    if actual != REQUIRED_BOARD_LABELS:
        raise ReviewError(
            "板块截图不完整，必须包含：一级行业、核心板块、细分方向；"
            f"当前为：{', '.join(actual) or '无'}"
        )
    if explicit:
        expected = tuple(parse_explicit_boards(explicit))
        existing = tuple(
            (label, str(info.get("code", "")).upper())
            for label, info in boards.items()
        )
        if existing != expected:
            raise ReviewError(
                "复用的板块与 --board 不一致；"
                "请修改 --board 或重新抓取板块截图。"
            )


def wait_for_board_page(page: Any, timeout: float) -> None:
    page.wait_for_selector(".quote_title_name", timeout=timeout * 1000)
    page.wait_for_function(
        """() => {
            const name = document.querySelector('.quote_title_name');
            const price = document.querySelector('.quote_quotenums .zxj');
            return name && name.textContent.trim() &&
                   price && price.textContent.trim() !== '' &&
                   price.textContent.trim() !== '-';
        }""",
        timeout=timeout * 1000,
    )
    wait_for_chart(page, (".time_chart", ".timessechart"), timeout)
    wait_for_chart(page, (".k_chart", ".kchart_d", "#emchartk"), timeout)


def open_board_page(browser: Any, board_code: str, args: argparse.Namespace, timeout: float) -> tuple:
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
            page.goto(BOARD_QUOTE_URL.format(code=board_code), wait_until="domcontentloaded")
            try:
                wait_for_board_page(page, attempt_timeout)
            except PlaywrightTimeoutError as exc:
                if find_login_dialog(page):
                    raise LoginRequiredError(
                        "板块页加载超时：页面出现登录弹窗，登录态可能已过期。"
                        "请先执行 python capture_eastmoney.py --login 重新登录。"
                    ) from exc
                raise
            ensure_logged_in(page)
            return context, page
        except LoginRequiredError:
            context.close()
            raise  # 登录态过期重试无效，直接提示重新登录
        except Exception as exc:
            last_error = exc
            context.close()
    raise ReviewError(f"打开或加载东方财富板块页失败（{board_code}）：{last_error}")


def capture_boards(
    stock_dir: Path,
    boards: Sequence[Tuple[str, str]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    timeout = max(args.timeout, 5.0)
    board_metadata: Dict[str, Any] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source": "https://quote.eastmoney.com/",
        "boards": {},
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            for index, (label, code) in enumerate(boards, start=5):
                context, page = open_board_page(browser, code, args, timeout)
                prefix = f"{index:02d}_board_{slugify(label)}_{code}"
                names = {
                    "trading_data": f"{prefix}_trading_data.png",
                    "intraday_chart": f"{prefix}_intraday_chart.png",
                    "daily_kline": f"{prefix}_daily_kline.png",
                    "flow_and_members": f"{prefix}_flow_and_members.png",
                }
                try:
                    board_name = page.evaluate(
                        "() => document.querySelector('.quote_title_name')?.textContent?.trim() || ''"
                    )

                    def restore_board(page_to_restore: Any = page) -> None:
                        wait_for_board_page(page_to_restore, timeout)

                    capture_union(
                        page,
                        [".quote_title", ".bkquote2l"],
                        stock_dir / names["trading_data"],
                        timeout,
                        prepare=restore_board,
                    )
                    capture_area(
                        page,
                        [".time_chart", ".timessechart"],
                        stock_dir / names["intraday_chart"],
                        timeout,
                        wait_chart=True,
                        prepare=restore_board,
                    )
                    capture_area(
                        page,
                        [".k_chart", ".kchart_d", "#emchartk"],
                        stock_dir / names["daily_kline"],
                        timeout,
                        wait_chart=True,
                        prepare=restore_board,
                    )
                    if index == 5:
                        capture_area(
                            page,
                            [".quote3l_r"],
                            stock_dir / names["flow_and_members"],
                            timeout,
                            prepare=restore_board,
                        )
                    else:
                        names.pop("flow_and_members")
                finally:
                    context.close()

                missing = [name for name in names.values() if not (stock_dir / name).is_file()]
                if missing:
                    raise ReviewError(f"板块截图未完整生成（{label}/{code}）：{', '.join(missing)}")
                board_metadata["boards"][label] = {
                    "code": code,
                    "name": board_name,
                    "url": BOARD_QUOTE_URL.format(code=code),
                    "screenshots": names,
                }
        finally:
            browser.close()

    (stock_dir / "boards_metadata.json").write_text(
        json.dumps(board_metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return board_metadata


def image_part(path: Path, description: str) -> Dict[str, Any]:
    if not path.is_file():
        raise ReviewError(f"截图不存在：{path}")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return {
        "description": description,
        "path": str(path),
        "mime_type": mime,
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def collect_individual_images(
    stock_dir: Path, metadata: Dict[str, Any], strict: bool = False
) -> List[Dict[str, Any]]:
    names = metadata.get("screenshots", {})
    preferred = (
        ("trading_data", "个股当日交易数据面板"),
        ("intraday_chart", "个股分时走势图"),
        ("bid_ask_5", "个股买卖五档盘口"),
        ("daily_kline", "个股日K线图"),
    )
    images = []
    missing = []
    for key, description in preferred:
        filename = names.get(key)
        if filename:
            images.append(image_part(stock_dir / filename, description))
        elif strict:
            missing.append(key)
    if strict and missing:
        raise ReviewError(f"个股截图不完整，缺少：{', '.join(missing)}")
    if not images:
        raise ReviewError("个股目录中没有可用的个股截图")
    return images


def collect_board_images(
    stock_dir: Path,
    board_metadata: Optional[Dict[str, Any]],
    strict: bool = False,
) -> List[Dict[str, Any]]:
    if not board_metadata or not board_metadata.get("boards"):
        if strict:
            raise ReviewError("板块截图未提供；请先生成板块截图后再使用 --skip-capture")
        return []
    images: List[Dict[str, Any]] = []
    missing: List[str] = []
    for board_index, (label, info) in enumerate(board_metadata["boards"].items()):
        if not isinstance(info, dict):
            raise ReviewError(f"板块“{label}”截图元数据格式无效")
        prefix = f"【{label}：{info.get('name', '')}/{info.get('code', '')}】"
        screenshots = info.get("screenshots", {})
        if not isinstance(screenshots, dict):
            raise ReviewError(f"板块“{label}”截图清单格式无效")
        for key, description in (
            ("trading_data", "板块行情数据"),
            ("intraday_chart", "板块分时图"),
            ("daily_kline", "板块日K线"),
        ):
            filename = screenshots.get(key)
            if filename:
                images.append(image_part(stock_dir / filename, f"{prefix}{description}"))
            elif strict:
                missing.append(f"{label}/{key}")

        filename = screenshots.get("flow_and_members")
        # The member/flow panel is a data table rather than a chart pattern.
        # Keep it as a local audit artifact, but do not send it to Gemini:
        # ranking scope is easy to misread and leaders must be web-verified.
        if board_index == 0:
            if filename:
                if not (stock_dir / filename).is_file():
                    missing.append(f"{label}/flow_and_members")
            elif strict:
                missing.append(f"{label}/flow_and_members")
    if strict and missing:
        raise ReviewError(f"板块截图不完整，缺少：{', '.join(missing)}")
    return images


def parse_peer_stocks(values: Sequence[str]) -> List[Dict[str, str]]:
    peers: List[Dict[str, str]] = []
    for value in values:
        parts = value.rsplit(":", 1)
        if len(parts) != 2 or not parts[0].strip() or not re.fullmatch(r"\d{6}", parts[1]):
            raise ReviewError(f"核心个股参数格式应为 名称:6位代码，当前为 {value}")
        peers.append({"name": parts[0].strip(), "code": parts[1], "type": "指定候选"})
    return peers


def infer_peer_stocks(
    board_metadata: Optional[Dict[str, Any]],
    stock: Dict[str, Any],
    explicit: Sequence[str] = (),
) -> List[Dict[str, str]]:
    if explicit:
        return parse_peer_stocks(explicit)

    boards = (
        board_metadata.get("boards", {})
        if isinstance(board_metadata, dict)
        and isinstance(board_metadata.get("boards"), dict)
        else {}
    )
    board_names = {
        str(info.get("name", ""))
        for info in boards.values()
        if isinstance(info, dict)
    }
    for theme, peers in DEFAULT_PEER_STOCKS.items():
        if any(theme in name for name in board_names):
            return list(peers)
    return []


def peer_metadata_path(stock_dir: Path) -> Path:
    return stock_dir / "core_peers_metadata.json"


def fetch_stock_daily_bar(
    stock: Dict[str, str], review_date: str, timeout: float
) -> Dict[str, Any]:
    """Fetch one stock's Eastmoney daily bar for the exact review date."""

    secid = stock_secid(stock)
    query = urllib.parse.urlencode(
        {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": (
                "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            ),
            "klt": 101,
            "fqt": 1,
            "beg": review_date.replace("-", ""),
            "end": review_date.replace("-", ""),
        }
    )
    payload = request_json(f"{STOCK_KLINE_API}?{query}", timeout)
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    for raw_line in klines:
        fields = str(raw_line).split(",")
        if len(fields) < 11 or fields[0] != review_date:
            continue
        return {
            "date": fields[0],
            "open": fields[1],
            "close": fields[2],
            "high": fields[3],
            "low": fields[4],
            "volume_hands": fields[5],
            "amount_yuan": fields[6],
            "amplitude_pct": fields[7],
            "change_pct": fields[8],
            "change_yuan": fields[9],
            "turnover_pct": fields[10],
        }
    raise ReviewError(
        f"东方财富未返回 {stock.get('name', '')} 在 {review_date} 的日线数据"
    )


def format_volume(value: str) -> str:
    try:
        hands = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{hands / 10000:.2f} 万手"


def format_amount(value: str) -> str:
    try:
        yuan = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{yuan / 100000000:.2f} 亿元"


def peer_table_html(peers: Sequence[Dict[str, Any]], review_date: str) -> str:
    rows = []
    for peer in peers:
        bar = peer.get("daily_bar", {})
        change_pct = float(bar.get("change_pct", 0))
        color = "#c0392b" if change_pct > 0 else "#1e7e34" if change_pct < 0 else "#333"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(peer.get('type', '核心个股')))}</td>"
            f"<td>{html.escape(str(peer.get('name', '')))}</td>"
            f"<td>{html.escape(str(peer.get('code', '')))}</td>"
            f"<td>{html.escape(str(bar.get('open', '')))}</td>"
            f"<td>{html.escape(str(bar.get('close', '')))}</td>"
            f"<td>{html.escape(str(bar.get('high', '')))}</td>"
            f"<td>{html.escape(str(bar.get('low', '')))}</td>"
            f"<td style='color:{color};font-weight:700'>"
            f"{change_pct:+.2f}%</td>"
            f"<td>{html.escape(str(bar.get('change_yuan', '')))}</td>"
            f"<td>{html.escape(format_volume(str(bar.get('volume_hands', ''))))}</td>"
            f"<td>{html.escape(format_amount(str(bar.get('amount_yuan', ''))))}</td>"
            f"<td>{html.escape(str(bar.get('turnover_pct', '')))}%</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #fff; color: #222; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; }}
  main {{ padding: 28px 34px; width: 1480px; }}
  h1 {{ margin: 0 0 8px; font-size: 30px; }}
  .meta {{ margin: 0 0 20px; color: #555; font-size: 15px; }}
  table {{ border-collapse: collapse; width: 100%; table-layout: fixed; font-size: 15px; }}
  th, td {{ border: 1px solid #d9d9d9; padding: 10px 8px; text-align: right; white-space: nowrap; }}
  th {{ background: #f2f4f7; color: #333; }}
  th:nth-child(-n+3), td:nth-child(-n+3) {{ text-align: left; }}
  tbody tr:nth-child(even) {{ background: #fafafa; }}
  .note {{ margin-top: 14px; color: #666; font-size: 13px; }}
</style>
<meta charset="utf-8">
<body>
<main>
  <h1>板块龙头 / 核心个股交易数据</h1>
  <p class="meta">交易日：{html.escape(review_date)} · 数据来源：东方财富历史日线接口 · 候选来源：Gemini 无联网对话提名</p>
  <table>
    <thead><tr>
      <th>候选类型</th><th>名称</th><th>代码</th><th>开盘</th><th>收盘</th>
      <th>最高</th><th>最低</th><th>涨跌幅</th><th>涨跌额</th>
      <th>成交量</th><th>成交额</th><th>换手率</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p class="note">说明：候选名单不代表当日全板块涨幅排行榜；本表用于比较 Gemini 提名的核心个股在指定交易日的实际表现。</p>
</main>
</body>
</html>"""


def capture_peer_trading_data(
    stock_dir: Path,
    peers: Sequence[Dict[str, str]],
    args: argparse.Namespace,
    review_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a dated Eastmoney trading-data table for board core stocks."""

    timeout = max(args.timeout, 5.0)
    if not review_date:
        raise ReviewError("缺少复盘日期，无法获取核心个股当日交易数据")
    output_dir = stock_dir / "core_peers"
    output_dir.mkdir(parents=True, exist_ok=True)
    enriched_peers: List[Dict[str, Any]] = []
    for peer in peers:
        enriched_peers.append(
            {
                **peer,
                "daily_bar": fetch_stock_daily_bar(peer, review_date, timeout),
            }
        )
    enriched_peers.sort(
        key=lambda item: float(item["daily_bar"].get("change_pct", 0)),
        reverse=True,
    )

    filename = f"{review_date.replace('-', '')}_core_peers_trading_data.png"
    metadata: Dict[str, Any] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "review_date": review_date,
        "source": STOCK_KLINE_API,
        "selection_source": "Gemini 无联网对话提名 + 东方财富历史日线数据核实",
        "screenshot": f"core_peers/{filename}",
        "peers": enriched_peers,
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            viewport={"width": 1480, "height": 900},
            device_scale_factor=float(getattr(args, "device_scale_factor", 2.0)),
        )
        try:
            page = context.new_page()
            page.set_content(peer_table_html(enriched_peers, review_date))
            page.wait_for_load_state("load")
            page.screenshot(
                path=str(output_dir / filename),
                full_page=True,
                timeout=timeout * 1000,
            )
        finally:
            context.close()
            browser.close()

    peer_metadata_path(stock_dir).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def load_peer_metadata(stock_dir: Path) -> Optional[Dict[str, Any]]:
    path = peer_metadata_path(stock_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def peer_metadata_matches(
    metadata: Optional[Dict[str, Any]],
    peers: Sequence[Dict[str, str]],
    stock_dir: Path,
    review_date: Optional[str] = None,
) -> bool:
    if not metadata or not isinstance(metadata.get("peers"), list):
        return False
    if review_date and metadata.get("review_date") != review_date:
        return False
    existing = {
        str(item.get("code", ""))
        for item in metadata["peers"]
        if isinstance(item, dict)
    }
    if existing != {peer["code"] for peer in peers}:
        return False
    return all(
        isinstance(item, dict)
        and (
            stock_dir
            / str(item.get("trading_data", metadata.get("screenshot", "")))
        ).is_file()
        for item in metadata["peers"]
    )


def collect_peer_images(
    stock_dir: Path,
    metadata: Optional[Dict[str, Any]],
    strict: bool = False,
) -> List[Dict[str, Any]]:
    if not metadata or not isinstance(metadata.get("peers"), list):
        return []
    captured_at = str(metadata.get("captured_at", ""))
    review_date = str(metadata.get("review_date", ""))
    images: List[Dict[str, Any]] = []
    missing: List[str] = []
    filename = metadata.get("screenshot")
    if filename:
        images.append(
            image_part(
                stock_dir / filename,
                (
                    "【板块龙头/核心个股候选交易数据表】"
                    f"东方财富历史日线数据（交易日：{review_date}，"
                    f"本地截图生成时间：{captured_at}）"
                ),
            )
        )
    elif strict:
        missing.append("core_peers")
    if strict and missing:
        raise ReviewError(f"核心个股截图不完整，缺少：{', '.join(missing)}")
    return images


def split_template(template: str) -> Tuple[str, str, str, str]:
    """Split the review template into current, board, technical, and closing."""

    def heading_position(text: str, marker: str) -> int:
        position = text.find(marker)
        if position < 0:
            raise ReviewError(f"模板中未找到“{marker}”位置")
        return text.rfind("\n", 0, position) + 1

    board_start = heading_position(template, "一、 板块")
    after_current = template[board_start:]
    technical_start = heading_position(after_current, "二、 技术面与量价状态")
    board = after_current[:technical_start]
    after_board = after_current[technical_start:]
    closing_start = heading_position(after_board, "三、近期大事")
    technical = after_board[:closing_start]
    closing = after_board[closing_start:]
    current = template[:board_start]
    return current, board, technical, closing


def build_prompt_parts(
    template: str,
    stock: Dict[str, Any],
    individual_images: Sequence[Dict[str, Any]],
    board_images: Sequence[Dict[str, Any]],
    holding: Optional[Dict[str, Any]] = None,
    board_metadata: Optional[Dict[str, Any]] = None,
    review_date: Optional[str] = None,
    search_tool_name: str = "Google Search",
    search_evidence: Optional[str] = None,
    peer_images: Optional[Sequence[Dict[str, Any]]] = None,
    peer_metadata: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    current_section, board_section, technical_section, closing_section = split_template(
        template
    )
    instruction = (
        f"你是一名严谨的A股交易复盘助手，本次动态资料检索来源为 {search_tool_name}。"
        "请严格区分“截图证据”和“检索证据”，并按模板输出完整复盘。\n"
        "证据规则：截图主要用于识别行情日期、直接可见的价格/量能数字、"
        "分时与K线形态、均线和关键位置；不得凭图表猜测排行榜、新闻、催化剂或财报。"
        "板块龙头、核心个股表现、板块涨跌原因、持续性逻辑、市场认可度、"
        "板块相对大盘、近期大事、财报、公司事件、行业事件、政策与宏观事件"
        f"必须优先使用 {search_tool_name} 提供的检索证据并交叉核实。\n"
        f"复盘基准日：{review_date or '以截图中的行情日期为准'}"
        "（若与截图行情日期不一致，以截图行情日期为检索基准）；"
        "检索时必须带股票/板块名称、代码和该日期，禁止把当前日期或一级行业数据"
        "误用于核心板块/细分方向。若搜索结果发布日期与基准日不符，需明确说明；"
        "没有可靠来源就写“待核实”，不要用模型记忆补齐。"
        "板块龙头必须写明统计口径（如当日涨幅、成交额或板块影响力）、数据日期和来源；"
        "搜索不到可靠排行时写“待核实”，禁止把成分股表格或一级行业领涨股当成"
        "核心板块/细分方向龙头。检索事实请在正文中标注来源和发布日期。\n"
        "注意：截图行情时间可能早于当前系统日期，复盘“当日”以截图中的行情日期为准。"
        "持仓成本若没有提供，不要虚构；仓位计划中未提供的成本和持股保留原占位。"
        "输出使用 Markdown，保留模板字段并逐项填写，最后补充风险提示。\n"
        "资料会分段提供：先用个股截图完成技术面与量价分析，"
        "再结合板块截图形态与联网检索完成板块分析，"
        f"最后用 {search_tool_name} 的检索证据填写近期大事，"
        "并结合持仓信息完成仓位计划与交易反思。"
        "最终报告必须仍按模板原顺序输出：当前数据、一、板块、二、技术面与量价状态、"
        "三、近期大事、四、仓位管理与计划交易、五、今日交易执行反思。\n\n"
    )
    holding_lines: List[str] = []
    if holding:
        cost = holding.get("cost")
        shares = holding.get("shares")
        plan = holding.get("plan")
        if cost not in (None, ""):
            holding_lines.append(f"- 持仓成本：{cost}")
        if shares not in (None, ""):
            holding_lines.append(f"- 持股数量：{shares}")
        if plan not in (None, ""):
            holding_lines.append(f"- 计划持有时间：{plan}")

    holding_text = (
        "持仓信息（由 my_stock.txt 提供，必须用于“仓位管理与计划交易”）：\n"
        + "\n".join(holding_lines)
        + "\n\n"
        if holding_lines
        else ""
    )
    board_lines: List[str] = []
    if board_metadata and isinstance(board_metadata.get("boards"), dict):
        for label, info in board_metadata["boards"].items():
            if not isinstance(info, dict):
                continue
            board_lines.append(
                f"- {label}：{info.get('name', '')}（{info.get('code', '')}）"
            )
    board_context = (
        "板块上下文（用于搜索和结果归类，不能替代数据核实）：\n"
        + "\n".join(board_lines)
        + "\n\n"
        if board_lines
        else ""
    )
    search_context = (
        "外部联网检索资料（已由智谱 Web Search/search_std 完成，"
        "仅用于动态事实，不能覆盖截图中的行情与形态）：\n"
        + str(search_evidence)
        + "\n\n"
        if search_evidence
        else ""
    )
    peer_lines: List[str] = []
    if peer_metadata and isinstance(peer_metadata.get("peers"), list):
        for item in peer_metadata["peers"]:
            if not isinstance(item, dict):
                continue
            peer_lines.append(
                f"- {item.get('name', '')} / {item.get('code', '')}："
                f"{item.get('type', '核心个股')}"
            )
    peer_context = (
        "板块龙头/核心个股候选（候选身份来自 Gemini 无联网对话，"
        "当日表现必须以下方东方财富交易数据截图为准；截图时间与复盘日不一致时必须分开说明）：\n"
        + "\n".join(peer_lines)
        + "\n\n"
        if peer_lines
        else ""
    )
    head = (
        current_section.replace(
            "股票名称/代码", f"{stock.get('name', '')} / {stock.get('code', '')}", 1
        )
        + holding_text
        + board_context
        + search_context
        + peer_context
        + "下面依次提供该个股的当前数据截图：\n"
    )
    parts: List[Dict[str, Any]] = [{"text": instruction + head}]
    for image in individual_images:
        parts.append({"text": f"（{image['description']}）"})
        parts.append(
            {"inline_data": {"mime_type": image["mime_type"], "data": image["data"]}}
        )

    parts.append(
        {
            "text": (
                "\n【分析分段 1】请基于上方个股行情截图，先完成模板"
                f"“二、 技术面与量价状态”：\n{technical_section}\n"
            )
        }
    )

    parts.append({"text": "\n下面提供板块相关截图：\n"})
    if board_images:
        for image in board_images:
            parts.append({"text": f"（{image['description']}）"})
            parts.append(
                {"inline_data": {"mime_type": image["mime_type"], "data": image["data"]}}
            )
    else:
        parts.append({"text": "\n【板块截图未提供】\n"})

    if peer_images:
        parts.append(
            {
                "text": (
                    "\n下面提供板块龙头/核心个股候选的东方财富交易数据截图。"
                    "请只从这些截图读取价格、涨跌幅、成交量、成交额、换手率和市值，"
                    "用于回答“板块龙头/核心个股表现”；截图时间与复盘基准日不一致时，"
                    "必须明确标注为“核心个股最新表现”，不得写成复盘日表现。\n"
                )
            }
        )
        for image in peer_images:
            parts.append({"text": f"（{image['description']}）"})
            parts.append(
                {"inline_data": {"mime_type": image["mime_type"], "data": image["data"]}}
            )

    parts.append(
        {
            "text": (
                "\n【分析分段 2】请基于上方板块截图，完成模板“一、 板块”：\n"
                "截图用于确认板块指数走势和形态；板块内部强弱、涨跌原因、"
                f"持续性和相对大盘必须先用 {search_tool_name} 提供的证据核实并注明来源。\n"
                "如果提供了核心个股交易数据截图，“板块龙头”和“核心个股表现”"
                "必须优先根据这些截图回答，并写明统计口径是候选股交易面板，"
                "不是当日板块涨幅排行榜。\n"
                + board_section
                + "\n"
            )
        }
    )
    parts.append(
        {
            "text": (
                "\n【分析分段 3】请综合前两段结论、持仓信息和模板要求，完成"
                "后续“三、近期大事”“四、 仓位管理与计划交易”和"
                f"“五、 今日交易执行反思”：\n{closing_section}\n"
                "“三、近期大事”中的每个小节都必须先检索；没有可靠来源时整项写“待核实”，"
                "不要编造事件或预期差。\n"
            )
        }
    )
    return parts


def load_environment_from_env_file() -> None:
    """Load supported API-key and proxy variables from the local .env file."""
    if not ENV_FILE.is_file():
        return
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReviewError(f"读取模型 API 配置失败（{ENV_FILE}）：{exc}") from exc

    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, _, value = entry.removeprefix("export ").partition("=")
        key = key.strip()
        if key not in ENV_FILE_KEYS:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key, value)


def api_key(provider: str = "gemini") -> str:
    load_environment_from_env_file()
    if provider == "zhipu":
        key = os.environ.get("ZHIPU_API_KEY")
        if not key:
            raise ReviewError("未找到智谱 API Key。请设置 ZHIPU_API_KEY 后重试。")
        return key

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ReviewError(
            "未找到 Gemini API Key。请设置 GEMINI_API_KEY 或 GOOGLE_API_KEY 后重试。"
        )
    return key


def resolve_provider(provider: str = "auto", model: Optional[str] = None) -> str:
    """Resolve the model provider, preferring Gemini when both keys exist."""

    if provider not in PROVIDERS:
        raise ReviewError(f"未支持的 AI 服务商：{provider}")
    if provider != "auto":
        return provider
    if model and model.startswith("gemini-"):
        return "gemini"
    load_environment_from_env_file()
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    return "zhipu" if os.environ.get("ZHIPU_API_KEY") else "gemini"


def resolve_model(model: Optional[str], provider: str) -> str:
    if model:
        return model
    return DEFAULT_ZHIPU_MODEL if provider == "zhipu" else DEFAULT_GEMINI_MODEL


def provider_search_tool_name(provider: str) -> str:
    return "智谱 Web Search" if provider == "zhipu" else "Google Search"


def resolve_search_provider(
    search_provider: str = "auto",
    model_provider: str = "gemini",
    disabled: bool = False,
) -> str:
    """Resolve external search, preferring Zhipu's standalone search API."""

    if disabled:
        return "none"
    if search_provider not in SEARCH_PROVIDERS:
        raise ReviewError(f"未支持的搜索服务商：{search_provider}")
    if search_provider != "auto":
        return search_provider
    load_environment_from_env_file()
    return "zhipu" if os.environ.get("ZHIPU_API_KEY") else "model"


def search_tool_name(search_provider: str, model_provider: str) -> str:
    if search_provider == "zhipu":
        return "智谱 Web Search（search_std）"
    if search_provider == "none":
        return "未启用联网检索"
    return provider_search_tool_name(model_provider)


def build_generation_config(enable_search: bool) -> Any:
    """Build the Gemini config, enabling grounded Google Search by default."""

    from google.genai import types as genai_types

    config_options: Dict[str, Any] = {
        "temperature": 0.1,
        "top_p": 0.9,
        "max_output_tokens": 32768,
    }
    if enable_search:
        config_options["tools"] = [
            genai_types.Tool(google_search=genai_types.GoogleSearch())
        ]
    return genai_types.GenerateContentConfig(**config_options)


def grounding_sources(response: Any) -> List[Tuple[str, str]]:
    """Extract deduplicated web sources from Gemini grounding metadata."""

    sources: List[Tuple[str, str]] = []
    seen = set()
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        metadata = getattr(candidate, "grounding_metadata", None)
        chunks = getattr(metadata, "grounding_chunks", None) or []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            uri = str(getattr(web, "uri", "") or "").strip()
            if not uri or uri in seen:
                continue
            title = str(getattr(web, "title", "") or uri).strip()
            seen.add(uri)
            sources.append((title, uri))
    return sources


def zhipu_content_parts(parts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert the provider-neutral prompt parts to Zhipu multimodal content."""

    content: List[Dict[str, Any]] = []
    for part in parts:
        if part.get("text") is not None:
            content.append({"type": "text", "text": str(part["text"])})
            continue
        inline_data = part.get("inline_data") or {}
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{inline_data['mime_type']};base64,"
                        f"{inline_data['data']}"
                    )
                },
            }
        )
    return content


def zhipu_web_search_tool() -> Dict[str, Any]:
    """Build Zhipu's built-in web-search tool configuration."""

    return {
        "type": "web_search",
        "web_search": {
            "enable": True,
            "search_engine": ZHIPU_SEARCH_ENGINE,
            "search_intent": "false",
            "count": 15,
            "search_recency_filter": "noLimit",
            "content_size": "high",
            "search_result": True,
            "require_search": True,
            "result_sequence": "before",
            "search_prompt": (
                "你是严谨的A股信息检索助手。请优先提炼与查询股票、板块、"
                "指定复盘日期、板块龙头、涨跌原因、财报和重大事件直接相关的信息；"
                "保留发布日期和来源，过滤无关、过期或与板块口径不一致的内容。"
            ),
        },
    }


def zhipu_web_search_url() -> str:
    base_url = os.environ.get("ZHIPU_BASE_URL", ZHIPU_API_BASE).rstrip("/")
    if base_url.endswith("/web_search"):
        return base_url
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    return f"{base_url}/web_search"


def zhipu_search_queries(
    stock: Dict[str, Any],
    board_metadata: Optional[Dict[str, Any]],
    review_date: Optional[str],
) -> List[str]:
    """Build targeted queries for facts that cannot be inferred from charts."""

    stock_name = str(stock.get("name", "")).strip()
    stock_code = str(stock.get("code", "")).strip()
    date_text = review_date or "当日"
    boards = (
        board_metadata.get("boards", {})
        if isinstance(board_metadata, dict)
        and isinstance(board_metadata.get("boards"), dict)
        else {}
    )
    board_values = []
    for label in REQUIRED_BOARD_LABELS:
        info = boards.get(label, {})
        if isinstance(info, dict):
            name = str(info.get("name", "")).strip()
            code = str(info.get("code", "")).strip()
            if name or code:
                board_values.append((label, name, code))

    queries = [
        f"{stock_name} {stock_code} {date_text} 财报 营收 净利润 业绩",
        f"{stock_name} {stock_code} {date_text} 公告 重大事件 新闻",
    ]
    for _, name, code in board_values:
        queries.append(
            f"{name} {code} {date_text} 板块指数 涨跌幅 成交量 龙头"
        )

    core_name = next(
        (name for label, name, _ in board_values if label == "核心板块"), ""
    )
    segment_name = next(
        (name for label, name, _ in board_values if label == "细分方向"), ""
    )
    board_theme = " ".join(filter(None, [core_name, segment_name])).strip()
    if board_theme:
        queries.append(
            f"{board_theme} {date_text} 板块 上涨 下跌 原因 消息 政策"
        )
    queries.append(
        f"{stock_name} {stock_code} {date_text} 行业 政策 产业链 消息"
    )
    return queries


def zhipu_search_payload(query: str) -> Dict[str, Any]:
    return {
        "search_engine": ZHIPU_SEARCH_ENGINE,
        "search_query": query,
        "search_intent": False,
        "count": 6,
        "search_recency_filter": "noLimit",
        "content_size": "high",
    }


def fetch_zhipu_search_results(
    stock: Dict[str, Any],
    board_metadata: Optional[Dict[str, Any]],
    review_date: Optional[str],
    timeout: float,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Call Zhipu's standalone Web Search API with search_std."""

    load_environment_from_env_file()
    key = api_key("zhipu")
    grouped_results: List[Dict[str, Any]] = []
    failures: List[str] = []
    for query in zhipu_search_queries(stock, board_metadata, review_date):
        try:
            response = post_zhipu_json(
                zhipu_web_search_url(),
                zhipu_search_payload(query),
                key,
                timeout,
            )
            search_result = response.get("search_result", [])
            items = search_result if isinstance(search_result, list) else []
        except ReviewError as exc:
            failures.append(f"{query}：{exc}")
            continue
        grouped_results.append({"query": query, "items": items})

    if not grouped_results and failures:
        details = "\n".join(failures)
        raise ReviewError(f"智谱 Web Search 全部查询失败：\n{details}")
    return grouped_results, failures


def format_zhipu_search_evidence(
    grouped_results: Sequence[Dict[str, Any]], failures: Sequence[str]
) -> str:
    sections: List[str] = []
    for group in grouped_results:
        query = str(group.get("query", ""))
        items = group.get("items", [])
        if not items:
            sections.append(f"#### 查询：{query}\n- 未返回可用结果；相关字段待核实。")
            continue
        lines = [f"#### 查询：{query}"]
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "未命名结果").strip()
            link = str(item.get("link") or "").strip()
            media = str(item.get("media") or "").strip()
            published = str(item.get("publish_date") or "").strip()
            source = title if not link else f"[{title}]({link})"
            meta = "，".join(filter(None, [media, published]))
            content = str(item.get("content") or "").strip().replace("\n", " ")
            if len(content) > 900:
                content = content[:900] + "…"
            lines.append(f"- {source}" + (f"（{meta}）" if meta else ""))
            if content:
                lines.append(f"  摘要：{content}")
        sections.append("\n".join(lines))

    if failures:
        sections.append(
            "#### 未成功完成的查询\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\n这些查询相关字段必须写“待核实”。"
        )
    return "\n\n".join(sections)


def append_external_search_sources(
    review: str, grouped_results: Sequence[Dict[str, Any]]
) -> str:
    sources: List[str] = []
    seen = set()
    for group in grouped_results:
        for item in group.get("items", []):
            if not isinstance(item, dict):
                continue
            link = str(item.get("link", "")).strip()
            if not link or link in seen:
                continue
            seen.add(link)
            title = str(item.get("title") or link).strip()
            media = str(item.get("media") or "").strip()
            published = str(item.get("publish_date") or "").strip()
            meta = "，".join(filter(None, [media, published]))
            sources.append(
                f"- [{title}]({link})" + (f"（{meta}）" if meta else "")
            )
    if not sources:
        return review
    return (
        review
        + "\n\n### 智谱 Web Search 来源（search_std，自动附加）\n"
        + "\n".join(sources)
    )


def zhipu_chat_url() -> str:
    base_url = (
        os.environ.get("ZHIPU_BASE_URL", ZHIPU_CHAT_API).rstrip("/")
    )
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def zhipu_request_payload(
    parts: Sequence[Dict[str, Any]],
    model: str,
    enable_search: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": zhipu_content_parts(parts)}
        ],
        "temperature": 0.1,
        "max_tokens": 32768,
        "stream": False,
    }
    if enable_search:
        payload["tools"] = [zhipu_web_search_tool()]
    return payload


def post_zhipu_json(
    url: str, payload: Dict[str, Any], key: str, timeout: float
) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(
            f"智谱 API HTTP 错误：{exc.code}：{error_body}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise ReviewError(f"智谱 API 调用失败：{exc}") from exc
    if not isinstance(result, dict):
        raise ReviewError("智谱 API 返回格式异常")
    return result


def zhipu_response_text(response: Dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ReviewError(f"智谱 API 未返回 choices：{response}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        text = "\n".join(str(item.get("text", "")) for item in content)
    else:
        text = str(content or "")
    text = text.strip()
    if not text:
        finish_reason = choices[0].get("finish_reason", "UNKNOWN")
        raise ReviewError(f"智谱返回内容为空，finish_reason={finish_reason}")
    return text


def append_zhipu_sources(review: str, response: Dict[str, Any]) -> str:
    search_results = response.get("web_search")
    if not isinstance(search_results, list) or not search_results:
        return (
            review
            + "\n\n### 智谱检索来源（自动附加）\n"
            + "- 智谱未返回可引用的网页来源；正文中的检索型字段请视为待核实。"
        )

    source_lines = []
    seen = set()
    for item in search_results:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link", "")).strip()
        if not link or link in seen:
            continue
        seen.add(link)
        title = str(item.get("title") or link).strip()
        media = str(item.get("media") or "").strip()
        publish_date = str(item.get("publish_date") or "").strip()
        source_lines.append(
            f"- [{title}]({link})"
            + (f"（{media}，{publish_date}）" if media or publish_date else "")
        )
    if not source_lines:
        return review
    return (
        review
        + "\n\n### 智谱检索来源（自动附加）\n"
        + "\n".join(source_lines)
    )


def call_zhipu(
    parts: Sequence[Dict[str, Any]],
    model: str,
    timeout: float,
    key: str,
    enable_search: bool = True,
) -> str:
    """Call Zhipu's multimodal Chat Completions API directly over HTTP."""

    load_environment_from_env_file()
    response = post_zhipu_json(
        zhipu_chat_url(),
        zhipu_request_payload(parts, model, enable_search),
        key,
        timeout,
    )
    review = zhipu_response_text(response)
    return append_zhipu_sources(review, response) if enable_search else review


def call_gemini(
    parts: Sequence[Dict[str, Any]],
    model: str,
    timeout: float,
    key: str,
    enable_search: bool = True,
) -> str:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    sdk_parts = []
    for part in parts:
        if part.get("text") is not None:
            sdk_parts.append(genai_types.Part.from_text(text=str(part["text"])))
            continue
        inline_data = part.get("inline_data") or {}
        sdk_parts.append(
            genai_types.Part.from_bytes(
                data=base64.b64decode(inline_data["data"]),
                mime_type=str(inline_data["mime_type"]),
            )
        )

    client = genai.Client(
        api_key=key,
        http_options=genai_types.HttpOptions(
            timeout=int(max(timeout, 30.0) * 1000)
        ),
    )

    try:
        response = client.models.generate_content(
            model=model,
            contents=sdk_parts,
            config=build_generation_config(enable_search),
        )
    except genai_errors.APIError as exc:
        raise ReviewError(f"Gemini SDK HTTP 错误：{exc}") from exc
    except Exception as exc:
        raise ReviewError(f"Gemini SDK 调用失败：{type(exc).__name__}：{exc}") from exc

    if not response.text:
        finish = getattr(response.candidates[0], "finish_reason", "UNKNOWN")
        raise ReviewError(f"Gemini 返回内容为空，finishReason={finish}")
    review = response.text.strip()
    if enable_search:
        sources = grounding_sources(response)
        if sources:
            source_lines = "\n".join(
                f"{index}. [{title}]({uri})"
                for index, (title, uri) in enumerate(sources, start=1)
            )
            review += "\n\n### Gemini 检索来源（自动附加）\n" + source_lines
        else:
            review += (
                "\n\n### Gemini 检索来源（自动附加）\n"
                "- Gemini 未返回可引用的网页来源；正文中的检索型字段请视为待核实。"
            )
    return review


def call_model(
    parts: Sequence[Dict[str, Any]],
    model: Optional[str],
    timeout: float,
    provider: str = "auto",
    enable_search: bool = True,
) -> str:
    """Call the selected provider with a provider-appropriate default model."""

    resolved_provider = resolve_provider(provider, model)
    resolved_model = resolve_model(model, resolved_provider)
    key = api_key(resolved_provider)
    if resolved_provider == "zhipu":
        return call_zhipu(
            parts, resolved_model, timeout, key, enable_search=enable_search
        )
    return call_gemini(
        parts, resolved_model, timeout, key, enable_search=enable_search
    )


def run(args: argparse.Namespace) -> Path:
    if args.skip_capture:
        stock_dir = find_latest_stock_dir(args.stock_dir)
    else:
        if load_auth_state(args.auth_state) is None:
            print("未找到东方财富登录状态，正在打开浏览器，请扫码登录。")
            login_and_save(args.auth_state, args.timeout)

        explicit_path = Path(args.stock_dir).expanduser() if args.stock_dir else None
        if explicit_path and explicit_path.is_dir():
            stock_dir = explicit_path.resolve()
        elif args.stock_dir:
            capture_args = argparse.Namespace(
                stock=args.stock_dir,
                output=str(SCREENSHOT_ROOT),
                timeout=args.timeout,
                headed=False,
                device_scale_factor=args.device_scale_factor,
                login=False,
                auth_state=args.auth_state,
            )
            stock_dir = capture_stock_screenshots(capture_args)
        else:
            stock_dir = find_latest_stock_dir(None)

    metadata = load_stock_metadata(stock_dir)
    stock = metadata["resolved_stock"]
    holding = metadata.get("holding") if isinstance(metadata.get("holding"), dict) else None
    template_path = Path(args.template).expanduser()
    if not template_path.is_file():
        raise ReviewError(f"复盘模板不存在：{template_path}")
    template = template_path.read_text(encoding="utf-8")

    board_metadata: Optional[Dict[str, Any]] = None
    if not args.skip_capture:
        secid = stock_secid(stock)
        boards = infer_boards(stock, secid, max(args.timeout, 5.0), args.board)
        print(f"正在补充板块截图：{', '.join(f'{label}/{code}' for label, code in boards)}")
        board_metadata = capture_boards(stock_dir, boards, args)
    else:
        board_metadata = load_boards_metadata(stock_dir)

    individual_images = collect_individual_images(
        stock_dir, metadata, strict=True
    )
    validate_reused_boards(board_metadata, args.board)
    board_images = collect_board_images(stock_dir, board_metadata, strict=True)
    review_date = metadata_review_date(metadata)
    peers = infer_peer_stocks(board_metadata, stock, args.peer_stock)
    peer_metadata: Optional[Dict[str, Any]] = None
    if peers and not args.no_peer_capture:
        existing_peers = load_peer_metadata(stock_dir)
        if peer_metadata_matches(existing_peers, peers, stock_dir, review_date):
            print(f"复用板块核心个股截图：{peer_metadata_path(stock_dir)}")
            peer_metadata = existing_peers
        elif not args.dry_run:
            print(
                "正在补充板块龙头/核心个股交易数据截图："
                + ", ".join(f"{peer['name']}/{peer['code']}" for peer in peers)
            )
            peer_metadata = capture_peer_trading_data(
                stock_dir, peers, args, review_date
            )
    peer_images = collect_peer_images(stock_dir, peer_metadata, strict=True)
    provider = resolve_provider(args.provider, args.model)
    model = resolve_model(args.model, provider)
    selected_search_provider = resolve_search_provider(
        args.search_provider, provider, args.no_web_search
    )
    selected_search_tool_name = search_tool_name(
        selected_search_provider, provider
    )
    grouped_search_results: List[Dict[str, Any]] = []
    search_evidence: Optional[str] = None
    if not args.dry_run and selected_search_provider == "zhipu":
        print("正在使用智谱 Web Search（search_std）检索动态信息...")
        grouped_search_results, search_failures = fetch_zhipu_search_results(
            stock, board_metadata, review_date, args.timeout
        )
        search_evidence = format_zhipu_search_evidence(
            grouped_search_results, search_failures
        )
    parts = build_prompt_parts(
        template,
        stock,
        individual_images,
        board_images,
        holding,
        board_metadata,
        review_date,
        selected_search_tool_name,
        search_evidence,
        peer_images,
        peer_metadata,
    )
    print(
        f"提示词构建完成：个股截图 {len(individual_images)} 张，"
        f"板块截图 {len(board_images)} 张，"
        f"核心个股截图 {len(peer_images)} 张，服务商 {provider}，"
            f"模型 {model}，搜索来源 {selected_search_tool_name}。"
    )

    if args.dry_run:
        manifest = {
            "stock": stock,
            "review_date": review_date,
            "provider": provider,
            "model": model,
            "search_provider": selected_search_provider,
            "search_engine": (
                ZHIPU_SEARCH_ENGINE
                if selected_search_provider == "zhipu"
                else selected_search_provider
            ),
            "web_search": selected_search_provider != "none",
            "individual_images": [image["description"] for image in individual_images],
            "board_images": [image["description"] for image in board_images],
            "peer_images": [image["description"] for image in peer_images],
        }
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return stock_dir

    review = call_model(
        parts,
        args.model,
        args.timeout,
        provider,
        enable_search=selected_search_provider == "model",
    )
    if selected_search_provider == "zhipu":
        review = append_external_search_sources(review, grouped_search_results)
    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else stock_dir / f"{provider}当日复盘.md"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(review + "\n", encoding="utf-8")
    print(f"已生成 {provider} 当日复盘：{output_path}")
    return output_path


def main() -> int:
    args = parse_args()
    try:
        run(args)
    except (ReviewError, CaptureError, AuthStateError) as exc:
        print(f"复盘失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
