#!/usr/bin/env python3
"""Run the Eastmoney capture and multi-provider AI review workflow."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from capture_eastmoney import (
    CaptureError,
    resolve_stock,
    run as capture_stock_screenshots,
    slugify,
)
from eastmoney_auth import AuthStateError, load_auth_state, login_and_save, resolve_auth_state
from run_daily_review import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_ZHIPU_MODEL,
    PROVIDERS,
    SEARCH_PROVIDERS,
    ZHIPU_SEARCH_ENGINE,
    ReviewError,
    build_prompt_parts,
    call_model,
    capture_boards,
    capture_peer_trading_data,
    collect_board_images,
    collect_individual_images,
    collect_peer_images,
    infer_boards,
    infer_peer_stocks,
    load_boards_metadata,
    load_peer_metadata,
    load_stock_metadata,
    metadata_review_date,
    normalize_stock_query,
    append_external_search_sources,
    fetch_zhipu_search_results,
    format_zhipu_search_evidence,
    peer_metadata_matches,
    peer_metadata_path,
    resolve_model,
    resolve_provider,
    resolve_search_provider,
    stock_secid,
    validate_reused_boards,
    search_tool_name,
)


PROJECT_ROOT = Path(__file__).resolve().parent
SCREENSHOT_ROOT = PROJECT_ROOT / "screenshots"
DEFAULT_PORTFOLIO_FILE = PROJECT_ROOT / "my_stock.txt"


@dataclass(frozen=True)
class Holding:
    name: str
    cost: str
    shares: str
    plan: str
    operation: Optional[Dict[str, str]] = None

    def as_dict(self) -> Dict[str, Any]:
        value = {
            "name": self.name,
            "cost": self.cost,
            "shares": self.shares,
            "plan": self.plan,
            "operation": self.operation,
        }
        return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="读取 my_stock.txt，批量截图持仓个股与板块，并生成整日持股复盘。"
    )
    parser.add_argument(
        "portfolio_file",
        nargs="?",
        default=str(DEFAULT_PORTFOLIO_FILE),
        help=f"持仓文件，默认 {DEFAULT_PORTFOLIO_FILE}",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="复盘目录日期，格式 YYYY-MM-DD，默认今天；仅影响目录与标题，不修改行情页面。",
    )
    parser.add_argument(
        "--board",
        action="append",
        default=[],
        metavar="标签:板块代码",
        help="强制指定三个板块，会应用到所有持仓；默认按东方财富接口自动推断。",
    )
    parser.add_argument(
        "--template",
        default=str(PROJECT_ROOT / "个股复盘模板内容.md"),
        help="复盘提示词模板文件",
    )
    parser.add_argument(
        "--output",
        help="整日复盘 Markdown 输出路径，默认保存到 screenshots/YYYY/MM/DD/ 下。",
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
        default=180.0,
        help="页面、联网搜索与模型 API 超时时间（秒）",
    )
    parser.add_argument(
        "--device-scale-factor", type=float, default=2.0, help="截图分辨率倍数"
    )
    parser.add_argument(
        "--auth-state",
        help=f"东方财富登录状态文件，默认 {resolve_auth_state(None)}",
    )
    parser.add_argument("--headed", action="store_true", help="显示个股截图浏览器窗口")
    parser.add_argument(
        "--skip-capture",
        action="store_true",
        help="复用指定日期已有截图，不打开东方财富页面。",
    )
    parser.add_argument(
        "--recapture",
        action="store_true",
        help="指定日期已存在同名截图时，归档旧目录并重新截图。",
    )
    parser.add_argument(
        "--no-portfolio-summary",
        action="store_true",
        help="只拼接各股复盘，不再额外调用模型生成组合级总结。",
    )
    parser.add_argument(
        "--capture-only",
        action="store_true",
        help="只抓取个股与板块截图，不调用模型、不生成 Markdown。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读检查当日既有截图与提示词清单；不联网、不写文件、不调用模型。",
    )
    return parser.parse_args()


def parse_review_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ReviewError(f"复盘日期格式无效：{value}，请使用 YYYY-MM-DD") from exc
    return parsed


def parse_portfolio(path: Path) -> List[Holding]:
    if not path.is_file():
        raise ReviewError(
            f"持仓文件不存在：{path}。请按“股票名称,成本,持股数,计划持有时间”每行一只股票。"
        )

    holdings: List[Holding] = []
    pending_holding: Optional[Holding] = None

    def accept_pending() -> None:
        nonlocal pending_holding
        if pending_holding is not None:
            holdings.append(pending_holding)
            pending_holding = None

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in re.split(r"[,，\t]", line)]
        if len(fields) == 4 and all(fields):
            accept_pending()
            pending_holding = Holding(
                name=fields[0], cost=fields[1], shares=fields[2], plan=fields[3]
            )
            continue

        if pending_holding is not None:
            operation = parse_operation_line(line)
            if operation is None:
                raise ReviewError(
                    f"{path.name} 第 {line_number} 行操作格式无效：{line}。"
                    "股票行后的操作行请写成：成交价,买入数量；"
                    "例如 34.80,买入100 或 34.80,卖出100"
                )
            else:
                pending_holding = Holding(
                    name=pending_holding.name,
                    cost=pending_holding.cost,
                    shares=pending_holding.shares,
                    plan=pending_holding.plan,
                    operation=operation,
                )
                accept_pending()
                continue

        accept_pending()
        if len(fields) in {2, 3} and any(re.search(r"买入|买|卖出|卖", item) for item in fields):
            raise ReviewError(
                f"{path.name} 第 {line_number} 行操作格式无效：{line}。"
                "请紧跟对应股票行写：成交价,买入数量；例如 34.80,买入100"
            )
        if len(fields) == 2 and _looks_like_price_and_quantity(fields):
            raise ReviewError(
                f"{path.name} 第 {line_number} 行操作缺少“买入/卖出”：{line}。"
                "请写成：34.80,买入100 或 34.80,卖出100"
            )
        raise ReviewError(
            f"{path.name} 第 {line_number} 行格式无效：{line}。"
            "请写成：股票名称,成本,持股数,计划持有时间"
        )

    accept_pending()
    if not holdings:
        raise ReviewError(f"持仓文件为空：{path}")
    return holdings


def _looks_like_price_and_quantity(fields: List[str]) -> bool:
    return bool(
        len(fields) == 2
        and re.fullmatch(r"\d+(?:\.\d+)?", fields[0])
        and re.fullmatch(r"\d+(?:股)?", fields[1])
    )


def parse_operation_line(line: str) -> Optional[Dict[str, str]]:
    """Parse a daily trade line following its holding row.

    Accepted examples::

        34.80,买入100
        34.80,买入,100
        买入34.80,100
        34.80 买入 100
        34.80,100  # defaults to buy
    """

    normalized = re.sub(r"[,，\t]", " ", line)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    action_match = re.search(r"(买入|买|卖出|卖)", normalized)
    if not action_match and not _looks_like_price_and_quantity(normalized.split(" ")):
        return None

    action = "买入"
    if action_match:
        action = "卖出" if action_match.group(1) in {"卖出", "卖"} else "买入"
        normalized = normalized.replace(action_match.group(1), " ", 1)

    numbers = re.findall(r"\d+(?:\.\d+)?", normalized)
    if len(numbers) != 2:
        return None
    try:
        price = float(numbers[0])
        quantity = int(float(numbers[1]))
    except ValueError:
        return None
    if price <= 0 or quantity <= 0:
        return None
    return {
        "action": action,
        "price": f"{price:.2f}",
        "quantity": str(quantity),
        "raw": line,
    }


def find_reusable_stock_dir(
    date_root: Path, holding: Holding, seen_codes: set
) -> Optional[Tuple[Path, Dict[str, Any]]]:
    candidates: List[Tuple[float, Path, Dict[str, Any]]] = []
    for metadata_path in date_root.glob("*/metadata.json"):
        stock_dir = metadata_path.parent
        if "_旧_" in stock_dir.name:
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        stock = metadata.get("resolved_stock")
        previous_holding = metadata.get("holding")
        if not isinstance(stock, dict):
            continue
        if not isinstance(previous_holding, dict):
            previous_holding = {}
        holding_query = normalize_stock_query(holding.name)
        values = {
            normalize_stock_query(str(stock.get("name", ""))),
            normalize_stock_query(str(stock.get("code", ""))),
            normalize_stock_query(str(stock.get("quote_id", ""))),
            normalize_stock_query(str(metadata.get("query", ""))),
            normalize_stock_query(str(previous_holding.get("name", ""))),
        }
        matched = holding_query in values
        if matched:
            try:
                board_metadata = load_boards_metadata(stock_dir)
                validate_reused_boards(board_metadata, [])
                collect_individual_images(stock_dir, metadata, strict=True)
                collect_board_images(stock_dir, board_metadata, strict=True)
            except ReviewError:
                continue
            candidates.append((metadata_path.stat().st_mtime, stock_dir, stock))

    if not candidates:
        return None
    _, stock_dir, stock = max(candidates, key=lambda item: item[0])
    code = str(stock.get("code", ""))
    if code in seen_codes:
        raise ReviewError(f"持仓文件中存在重复股票：{holding.name}（{code}）")
    return stock_dir, stock


def prepare_stock_dir(
    date_root: Path,
    holding: Holding,
    timeout: float,
    recapture: bool,
    seen_codes: set,
    skip_capture: bool = False,
) -> Tuple[Path, Dict[str, Any]]:
    if skip_capture or not recapture:
        reusable = find_reusable_stock_dir(date_root, holding, seen_codes)
        if reusable is not None:
            return reusable
        if skip_capture:
            raise ReviewError(
                f"--skip-capture 模式下未找到 {holding.name} 的既有截图目录：{date_root}"
            )

    stock = resolve_stock(holding.name, max(timeout, 5.0))
    code = str(stock.get("code", ""))
    if code in seen_codes:
        raise ReviewError(f"持仓文件中存在重复股票：{holding.name}（{code}）")
    stock_dir = date_root / f"{slugify(stock['name'])}_{stock['code']}"

    if stock_dir.exists():
        archive_suffix = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        archived_dir = stock_dir.with_name(f"{stock_dir.name}_旧_{archive_suffix}")
        stock_dir.rename(archived_dir)
        print(f"已归档旧截图：{stock_dir} -> {archived_dir}")

    return stock_dir, stock


def capture_holding(
    holding: Holding,
    stock_dir: Path,
    review_date: date,
    args: argparse.Namespace,
) -> None:
    capture_args = argparse.Namespace(
        stock=holding.name,
        output=str(SCREENSHOT_ROOT),
        timeout=args.timeout,
        headed=args.headed,
        device_scale_factor=args.device_scale_factor,
        login=False,
        auth_state=args.auth_state,
        review_date=review_date.isoformat(),
    )
    capture_stock_screenshots(capture_args, output_dir=stock_dir)

    metadata_path = stock_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["review_date"] = review_date.isoformat()
    metadata["holding"] = holding.as_dict()
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def ensure_holding_metadata(
    stock_dir: Path, holding: Holding, review_date: date
) -> None:
    metadata_path = stock_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewError(f"读取截图元数据失败：{metadata_path}") from exc

    changed = False
    if metadata.get("holding") != holding.as_dict():
        metadata["holding"] = holding.as_dict()
        changed = True
    if metadata.get("review_date") != review_date.isoformat():
        metadata["review_date"] = review_date.isoformat()
        changed = True
    if changed:
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def board_metadata_for_stock(stock_dir: Path) -> Optional[Dict[str, Any]]:
    path = stock_dir / "boards_metadata.json"
    if not path.is_file():
        return None
    return load_boards_metadata(stock_dir)


def capture_or_reuse_boards(
    stock_dir: Path,
    stock: Dict[str, str],
    args: argparse.Namespace,
) -> Optional[Dict[str, Any]]:
    existing: Optional[Dict[str, Any]] = None
    try:
        existing = board_metadata_for_stock(stock_dir)
        if existing is not None and not args.board:
            validate_reused_boards(existing, args.board)
    except ReviewError as exc:
        print(f"  既有板块截图不可复用，将重新补充：{exc}")
        existing = None

    if existing is not None and not args.board:
        print(f"  复用板块截图：{stock_dir / 'boards_metadata.json'}")
        return existing

    secid = stock_secid(stock)
    boards = infer_boards(stock, secid, max(args.timeout, 5.0), args.board)
    if existing is not None:
        existing_boards = [
            (label, str(info.get("code", "")).upper())
            for label, info in existing.get("boards", {}).items()
        ]
        if boards == existing_boards:
            print(f"  复用板块截图：{stock_dir / 'boards_metadata.json'}")
            return existing
        print("  检测到 --board 强制指定或板块变化，重新补充板块截图。")

    print(f"  补充板块截图：{', '.join(f'{label}/{code}' for label, code in boards)}")
    return capture_boards(stock_dir, boards, args)


def review_one_stock(
    holding: Holding,
    stock_dir: Path,
    template: str,
    args: argparse.Namespace,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int, str]:
    metadata = load_stock_metadata(stock_dir)
    stock = metadata["resolved_stock"]
    board_metadata = board_metadata_for_stock(stock_dir)
    validate_reused_boards(board_metadata, args.board)
    individual_images = collect_individual_images(stock_dir, metadata, strict=True)
    board_images = collect_board_images(stock_dir, board_metadata, strict=True)
    review_date = metadata_review_date(metadata)
    peers = infer_peer_stocks(board_metadata, stock, args.peer_stock)
    peer_metadata = None
    if peers and not args.no_peer_capture:
        existing_peers = load_peer_metadata(stock_dir)
        if peer_metadata_matches(existing_peers, peers, stock_dir, review_date):
            print(f"  复用板块核心个股截图：{peer_metadata_path(stock_dir)}")
            peer_metadata = existing_peers
        elif not args.dry_run and not args.capture_only:
            print(
                "  补充板块龙头/核心个股交易数据截图："
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
    search_evidence = None
    grouped_search_results: List[Dict[str, Any]] = []
    if not args.dry_run and not args.capture_only and selected_search_provider == "zhipu":
        print(f"  使用智谱 Web Search（{ZHIPU_SEARCH_ENGINE}）检索动态信息...")
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
        holding.as_dict(),
        board_metadata,
        review_date,
        selected_search_tool_name,
        search_evidence,
        peer_images,
        peer_metadata,
    )

    if args.dry_run or args.capture_only:
        return stock, individual_images, len(board_images) + len(peer_images), ""
    review = call_model(
        parts,
        args.model,
        args.timeout,
        provider,
        enable_search=selected_search_provider == "model",
    )
    if selected_search_provider == "zhipu":
        review = append_external_search_sources(review, grouped_search_results)
    review_path = stock_dir / f"{provider}当日复盘.md"
    review_path.write_text(review + "\n", encoding="utf-8")
    print(f"  已生成 {provider} 个股复盘：{review_path}")
    return stock, individual_images, len(board_images) + len(peer_images), review


def portfolio_summary_prompt(
    review_date: date,
    stock_reviews: Sequence[Tuple[Holding, Dict[str, Any], str]],
) -> str:
    entries = []
    for holding, stock, review in stock_reviews:
        entries.append(
            f"## {stock.get('name', holding.name)} / {stock.get('code', '')}\n"
            f"- 成本：{holding.cost}\n"
            f"- 持股：{holding.shares}\n"
            f"- 计划持有时间：{holding.plan}\n\n{review}"
        )

    return (
        f"以下是 {review_date.isoformat()} 多只持仓股票的自动复盘结果。"
        "请基于这些内容生成“组合级复盘摘要”，不要新增个股复盘之外的事实。"
        "使用 Markdown，包含：\n"
        "1. 今日组合整体判断\n"
        "2. 持仓强弱排序与核心原因\n"
        "3. 明日优先关注的股票与触发条件\n"
        "4. 组合风险与仓位建议\n"
        "要求简洁、可执行，不确定的信息明确写“待核实”。\n\n"
        + "\n\n".join(entries)
    )


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|")


def render_daily_review(
    review_date: date,
    holdings: Sequence[Holding],
    summary: str,
    stock_reviews: Sequence[Tuple[Holding, Dict[str, Any], str]],
    output_path: Path,
) -> None:
    lines = [
        f"# {review_date.year}.{review_date.month}.{review_date.day} 持股个股复盘",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 请求复盘日期：{review_date.isoformat()}",
        "- 截图行情日期：以截图页面显示为准；若与请求日期不一致，正文以截图日期为准",
        "- 数据来源：东方财富页面截图 + 所选 AI 模型图像理解；动态信息检索状态见正文",
        "",
        "## 持仓总览",
        "",
        "| 股票 | 成本 | 持股数 | 计划持有时间 |",
        "| --- | ---: | ---: | --- |",
    ]
    for holding in holdings:
        lines.append(
            f"| {markdown_cell(holding.name)} | {markdown_cell(holding.cost)} | "
            f"{markdown_cell(holding.shares)} | {markdown_cell(holding.plan)} |"
        )

    lines.extend(["", "## 组合级复盘摘要", ""])
    lines.append(summary.strip() or "未生成组合级摘要。")

    lines.extend(["", "## 个股完整复盘", ""])
    for holding, stock, review in stock_reviews:
        title = f"{stock.get('name', holding.name)} / {stock.get('code', '')}"
        lines.extend([f"### {title}", "", review.strip(), ""])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def default_daily_output(date_root: Path, review_date: date) -> Path:
    compact_date = review_date.strftime("%Y%m%d")
    return date_root / f"{compact_date}_持股个股复盘.md"


def run(args: argparse.Namespace) -> Path:
    if args.dry_run and args.recapture:
        raise ReviewError("--dry-run 是只读检查，不能与 --recapture 同时使用")
    if args.capture_only and args.dry_run:
        raise ReviewError("--capture-only 会抓取截图，不能与 --dry-run 同时使用")
    if args.capture_only and args.skip_capture:
        raise ReviewError("--capture-only 与 --skip-capture 不能同时使用")
    if args.capture_only and args.output:
        raise ReviewError("--capture-only 不生成 Markdown，不能指定 --output")

    review_date = parse_review_date(args.date)
    holdings = parse_portfolio(Path(args.portfolio_file).expanduser())
    date_root = (SCREENSHOT_ROOT / review_date.strftime("%Y/%m/%d")).resolve()
    if not args.dry_run:
        date_root.mkdir(parents=True, exist_ok=True)

    effective_skip_capture = args.skip_capture or args.dry_run
    if not effective_skip_capture and load_auth_state(args.auth_state) is None:
        print("未找到东方财富登录状态，正在打开浏览器，请扫码登录。")
        login_and_save(args.auth_state, args.timeout)

    template_path = Path(args.template).expanduser()
    if not template_path.is_file():
        raise ReviewError(f"复盘模板不存在：{template_path}")
    template = template_path.read_text(encoding="utf-8")
    provider = resolve_provider(args.provider, args.model)
    model = resolve_model(args.model, provider)
    selected_search_provider = resolve_search_provider(
        args.search_provider, provider, args.no_web_search
    )
    print(
        f"AI 服务商：{provider} / {model}；"
        f"动态信息搜索：{search_tool_name(selected_search_provider, provider)}"
    )

    stock_reviews: List[Tuple[Holding, Dict[str, Any], str]] = []
    failures: List[Tuple[Holding, str]] = []
    seen_codes = set()

    for index, holding in enumerate(holdings, start=1):
        try:
            print(f"[{index}/{len(holdings)}] 处理 {holding.name}")
            stock_dir, resolved_stock = prepare_stock_dir(
                date_root,
                holding,
                args.timeout,
                args.recapture and not effective_skip_capture,
                seen_codes,
                effective_skip_capture,
            )
            code = str(resolved_stock.get("code", ""))
            seen_codes.add(code)

            if not stock_dir.is_dir():
                if effective_skip_capture:
                    mode = "--skip-capture" if args.skip_capture else "--dry-run"
                    raise ReviewError(f"{mode} 模式下截图目录不存在：{stock_dir}")
                print(f"  抓取个股截图：{stock_dir.relative_to(SCREENSHOT_ROOT)}")
                capture_holding(holding, stock_dir, review_date, args)
            else:
                print(f"  当天已有完整个股截图，跳过重新截图：{stock_dir}")
                if not args.dry_run:
                    ensure_holding_metadata(stock_dir, holding, review_date)

            if not effective_skip_capture:
                capture_or_reuse_boards(stock_dir, resolved_stock, args)

            stock, individual_images, board_count, review = review_one_stock(
                holding, stock_dir, template, args
            )
            print(
                f"  图片清单：个股 {len(individual_images)} 张，"
                f"板块/核心个股 {board_count} 张"
            )
            if review:
                stock_reviews.append((holding, stock, review))
        except (CaptureError, ReviewError, AuthStateError) as exc:
            failures.append((holding, str(exc)))
            print(f"  失败：{exc}", file=sys.stderr)

    if args.capture_only:
        if failures:
            details = "\n".join(
                f"- {holding.name}：{error}" for holding, error in failures
            )
            raise ReviewError(f"截图抓取存在 {len(failures)} 只股票失败：\n{details}")
        print(f"已完成 {len(holdings)} 只持仓截图抓取：{date_root}")
        return date_root

    if not stock_reviews and not args.dry_run:
        if failures:
            raise ReviewError(f"所有持仓复盘均失败，最后错误：{failures[-1][1]}")
        raise ReviewError("没有可用的个股复盘结果")

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else default_daily_output(date_root, review_date)
    )

    if args.dry_run:
        manifest = {
            "review_date": review_date.isoformat(),
            "date_root": str(date_root),
            "provider": provider,
            "model": model,
            "search_provider": selected_search_provider,
            "search_engine": (
                ZHIPU_SEARCH_ENGINE
                if selected_search_provider == "zhipu"
                else selected_search_provider
            ),
            "web_search": (
                not args.capture_only and selected_search_provider != "none"
            ),
            "holdings": [holding.as_dict() for holding in holdings],
            "failures": [{"stock": holding.name, "error": error} for holding, error in failures],
        }
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        if failures:
            details = "\n".join(
                f"- {holding.name}：{error}" for holding, error in failures
            )
            raise ReviewError(
                f"dry-run 检查发现 {len(failures)} 只股票异常：\n{details}"
            )
        return output_path

    summary = ""
    if not args.no_portfolio_summary and stock_reviews:
        print("生成组合级复盘摘要...")
        summary = call_model(
            [
                {
                    "text": portfolio_summary_prompt(
                        review_date, stock_reviews
                    )
                }
            ],
            args.model,
            args.timeout,
            args.provider,
            enable_search=False,
        )

    render_daily_review(review_date, holdings, summary, stock_reviews, output_path)
    print(f"已生成整日持股复盘：{output_path}")

    if failures:
        details = "\n".join(
            f"- {holding.name}：{error}" for holding, error in failures
        )
        raise ReviewError(
            f"整日复盘已生成，但有 {len(failures)} 只股票处理失败：\n{details}"
        )
    return output_path


def main() -> int:
    args = parse_args()
    try:
        run(args)
    except (CaptureError, ReviewError, AuthStateError) as exc:
        print(f"整日复盘未完整完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
