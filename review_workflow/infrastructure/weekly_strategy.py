"""Weekly strategy generation and daily follow-up for local reviews."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEEKLY_ROOT = PROJECT_ROOT / "screenshots" / "weekly"


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _format(value: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "待核实"
    return f"{value:.{digits}f}{suffix}"


def _percent(value: Optional[float]) -> str:
    return "待核实" if value is None else f"{value * 100:.2f}%"


def _markdown(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _round_lot(shares: int, ratio: float) -> int:
    if shares <= 0:
        return 0
    quantity = max(1, round(shares * ratio))
    return max(1, ((quantity + 99) // 100) * 100 if shares >= 100 else quantity)


def _iso_week(review_date: str) -> Tuple[int, int]:
    parsed = date.fromisoformat(review_date)
    iso_year, iso_week, _ = parsed.isocalendar()
    return iso_year, iso_week


def _week_files(review_date: str) -> Tuple[Path, Path]:
    iso_year, iso_week = _iso_week(review_date)
    stem = f"{iso_year}-W{iso_week:02d}"
    directory = WEEKLY_ROOT / stem
    return directory / f"{stem}_交易策略单.json", directory / f"{stem}_交易策略单.md"


def _legacy_week_files(review_date: str) -> Tuple[Path, Path]:
    iso_year, iso_week = _iso_week(review_date)
    stem = f"{iso_year}-W{iso_week:02d}"
    directory = WEEKLY_ROOT / str(iso_year)
    return directory / f"{stem}_交易策略单.json", directory / f"{stem}_交易策略单.md"


def _load_sidecar(review_path: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    path = Path(str(review_path.get("review_path", "")))
    sidecar = path.parent / "local_review_data.json"
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_reviews(reviews: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for item in reviews:
        payload = _load_sidecar(item)
        if payload:
            result.append(payload)
    return result


def _is_first_trading_day(review_date: str, payloads: Sequence[Mapping[str, Any]]) -> bool:
    parsed = date.fromisoformat(review_date)
    week_start = parsed - timedelta(days=parsed.weekday())
    prior_dates = set()
    for payload in payloads:
        history = payload.get("history", [])
        for raw_bar in history:
            if not isinstance(raw_bar, dict):
                continue
            bar_date = str(raw_bar.get("date", ""))[:10]
            try:
                parsed_bar_date = date.fromisoformat(bar_date)
            except ValueError:
                continue
            if week_start <= parsed_bar_date < parsed:
                prior_dates.add(bar_date)
    return not prior_dates


def _levels(payload: Mapping[str, Any]) -> Tuple[List[float], List[float]]:
    bar = payload.get("bar", {})
    indicators = payload.get("indicators", {})
    close = _number(bar.get("close"))
    if close is None:
        return [0.0, 0.0], [0.0, 0.0]
    values = [
        _number(bar.get("low")),
        _number(bar.get("open")),
        _number(indicators.get("ma5")),
        _number(indicators.get("ma10")),
        _number(indicators.get("ma20")),
        _number(indicators.get("ma60")),
        _number(bar.get("high")),
    ]
    below = sorted({value for value in values if value is not None and value < close}, reverse=True)
    above = sorted({value for value in values if value is not None and value > close})
    if not below:
        below = [_number(bar.get("low")) or close]
    if not above:
        above = [_number(bar.get("high")) or close]
    if len(below) < 2:
        below.append(round(below[0] * 0.98, 2))
    if len(above) < 2:
        above.append(round(max(above[0], close) * 1.02, 2))
    return below[:2], above[:2]


def _market_value(payload: Mapping[str, Any]) -> float:
    close = _number(payload.get("bar", {}).get("close")) or 0.0
    shares = _number(payload.get("holding", {}).get("shares")) or 0.0
    return close * shares


def _strategy_item(payload: Mapping[str, Any], total_market_value: float) -> Dict[str, Any]:
    stock = payload.get("stock", {})
    holding = payload.get("holding", {})
    bar = payload.get("bar", {})
    indicators = payload.get("indicators", {})
    close = _number(bar.get("close")) or 0.0
    cost = _number(holding.get("cost")) or 0.0
    shares = int(_number(holding.get("shares")) or 0)
    market_value = close * shares
    pnl = market_value - cost * shares
    pnl_pct = pnl / (cost * shares) if cost and shares else None
    supports, resistances = _levels(payload)
    ma5 = _number(indicators.get("ma5"))
    ma10 = _number(indicators.get("ma10"))
    ma20 = _number(indicators.get("ma20"))
    weight = market_value / total_market_value if total_market_value else None
    strong_trend = (
        ma5 is not None
        and ma10 is not None
        and ma20 is not None
        and close >= ma5
        and close >= ma10
        and close >= ma20
    )
    weak_trend = ma5 is not None and close < ma5

    if weak_trend and pnl_pct is not None and pnl_pct < -0.05:
        core_strategy = "弱势持仓：反弹降仓，不左侧补仓"
        reduce_ratio = 0.30
    elif weak_trend:
        core_strategy = "弱势观察：等待收复 MA5"
        reduce_ratio = 0.20
    elif strong_trend and weight is not None and weight >= 0.40:
        core_strategy = "趋势持有：利用强势降低单票集中度"
        reduce_ratio = 0.20
    elif strong_trend:
        core_strategy = "趋势持有：不追高，回踩确认"
        reduce_ratio = 0.15
    else:
        core_strategy = "震荡观察：等待方向选择"
        reduce_ratio = 0.20

    f10 = payload.get("f10") or {}
    businesses = [
        row for row in f10.get("main_business", [])
        if _number(row.get("income_ratio")) is not None
    ]
    businesses.sort(key=lambda row: float(row["income_ratio"]), reverse=True)
    themes = [
        item.get("keyword", "")
        for item in f10.get("concepts", [])
        if item.get("classification") in {"主营业务", "行业背景"}
    ][:4]

    return {
        "code": str(stock.get("code", "")),
        "name": str(stock.get("name", holding.get("name", ""))),
        "plan": str(holding.get("plan", "")),
        "baseline_cost": cost,
        "shares": shares,
        "baseline_close": close,
        "baseline_pnl": pnl,
        "baseline_pnl_pct": pnl_pct,
        "weight": weight,
        "support_1": supports[0],
        "support_2": supports[1],
        "pressure_1": resistances[0],
        "pressure_2": resistances[1],
        "core_strategy": core_strategy,
        "reduce_quantity": _round_lot(shares, reduce_ratio),
        "buy_quantity": 0 if weak_trend else _round_lot(shares, 0.15),
        "main_business": businesses[0].get("name", "待核实") if businesses else "待核实",
        "themes": themes,
    }


def _new_strategy(review_date: str, payloads: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    total_market_value = sum(_market_value(payload) for payload in payloads)
    items = [_strategy_item(payload, total_market_value) for payload in payloads]
    items.sort(key=lambda item: (-item["weight"] if item["weight"] is not None else 0, item["code"]))

    largest = items[0] if items else None
    weak_items = [item for item in items if "弱势" in item["core_strategy"]]
    crowded_items = [
        payload for payload in payloads
        if (_number(payload.get("bar", {}).get("turnover_pct")) or 0) >= 10
    ]
    rules: List[str] = []
    if largest and largest["weight"] is not None and largest["weight"] >= 0.40:
        rules.append(
            f"{largest['name']} 权重 {_percent(largest['weight'])}，强势冲高时优先降低单票集中度。"
        )
    if weak_items:
        rules.append(
            "、".join(item["name"] for item in weak_items)
            + " 处于弱势策略，禁止左侧补仓。"
        )
    if crowded_items:
        names = [
            str(payload.get("stock", {}).get("name", ""))
            for payload in crowded_items
        ]
        rules.append("、".join(names) + " 换手率过高，冲高时优先考虑部分止盈。")
    if not rules:
        rules.append("暂未触发集中度、弱势或高换手警告，按个股条件单执行。")

    return {
        "version": 1,
        "week": None,
        "created_on": review_date,
        "first_trading_day": _is_first_trading_day(review_date, payloads),
        "baseline_holdings": {
            str(payload.get("stock", {}).get("code", "")): {
                "name": str(payload.get("stock", {}).get("name", "")),
                "cost": str(payload.get("holding", {}).get("cost", "")),
                "shares": str(payload.get("holding", {}).get("shares", "")),
                "plan": str(payload.get("holding", {}).get("plan", "")),
            }
            for payload in payloads
        },
        "items": items,
        "rules": rules,
        "change_log": [],
        "daily_reviews": [],
    }


def _save_rendered(strategy: Dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    iso_year, iso_week = _iso_week(strategy["created_on"])
    strategy["week"] = f"{iso_year}-W{iso_week:02d}"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(strategy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(_render(strategy), encoding="utf-8")


def _current_holdings(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, str]]:
    return {
        str(payload.get("stock", {}).get("code", "")): {
            "name": str(payload.get("stock", {}).get("name", "")),
            "cost": str(payload.get("holding", {}).get("cost", "")),
            "shares": str(payload.get("holding", {}).get("shares", "")),
            "plan": str(payload.get("holding", {}).get("plan", "")),
        }
        for payload in payloads
    }


def _item_status(item: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    close = _number(payload.get("bar", {}).get("close"))
    if close is None:
        return "行情待核实"
    if close <= float(item.get("support_1", 0)):
        return "已触发支撑/风控区"
    if close >= float(item.get("pressure_2", 0)):
        return "已触发突破区"
    if close >= float(item.get("pressure_1", 0)):
        return "进入压力/减仓区"
    return "未触发关键条件"


def _operation_status(item: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    operation = payload.get("holding", {}).get("operation")
    if not isinstance(operation, dict):
        return "未记录操作"

    price = _number(operation.get("price"))
    if price is None:
        return "操作价格待核实"
    action = str(operation.get("action", ""))
    if action == "买入":
        if price <= float(item.get("support_1", 0)):
            return "买入接近支撑：执行有安全边际"
        if price >= float(item.get("pressure_1", 0)):
            return "买入接近压力：存在追高风险"
        return "买入位于中性区间"
    if action == "卖出":
        if price >= float(item.get("pressure_1", 0)):
            return "卖出接近压力：执行质量较好"
        if price <= float(item.get("support_1", 0)):
            return "卖出接近支撑：执行质量偏差"
        return "卖出位于中性区间"
    return "操作类型待核实"


def _review_daily(
    strategy: Dict[str, Any],
    review_date: str,
    payloads: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    current_holdings = _current_holdings(payloads)
    baseline_holdings = strategy.get("baseline_holdings", {})
    holding_changed = current_holdings != baseline_holdings
    item_statuses: List[Dict[str, Any]] = []
    update_reasons: List[str] = []

    if holding_changed:
        update_reasons.append("当前持仓与本周策略单基准持仓不一致")
    payload_by_code = {
        str(payload.get("stock", {}).get("code", "")): payload for payload in payloads
    }
    for item in strategy.get("items", []):
        code = str(item.get("code", ""))
        payload = payload_by_code.get(code)
        if payload is None:
            item_statuses.append(
                {"code": code, "name": item.get("name", ""), "status": "当日无数据"}
            )
            update_reasons.append(f"{item.get('name', '')} 当日无数据")
            continue
        status = _item_status(item, payload)
        current_operation_status = _operation_status(item, payload)
        item_statuses.append(
            {
                "code": code,
                "name": item.get("name", ""),
                "close": _number(payload.get("bar", {}).get("close")),
                "status": status,
                "operation_status": current_operation_status,
            }
        )
        if "支撑/风控" in status or "突破" in status:
            update_reasons.append(f"{item.get('name', '')} {status}")
        if "追高" in current_operation_status or "执行质量偏差" in current_operation_status:
            update_reasons.append(
                f"{item.get('name', '')} {current_operation_status}"
            )

    for code in current_holdings:
        if code not in baseline_holdings:
            update_reasons.append(f"新增持仓 {current_holdings[code]['name']}")
    for code in baseline_holdings:
        if code not in current_holdings:
            update_reasons.append(f"移除持仓 {baseline_holdings[code]['name']}")

    review = {
        "date": review_date,
        "holding_changed": holding_changed,
        "statuses": item_statuses,
        "update_reasons": list(dict.fromkeys(update_reasons)),
        "decision": "已更新" if holding_changed else "继续执行",
    }
    return review, holding_changed


def _apply_daily_review(
    strategy: Dict[str, Any],
    review_date: str,
    payloads: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    review, holding_changed = _review_daily(strategy, review_date, payloads)
    if holding_changed:
        old_version = {
            "version": strategy.get("version", 1),
            "created_on": strategy.get("created_on", review_date),
            "baseline_holdings": strategy.get("baseline_holdings", {}),
            "items": strategy.get("items", []),
            "rules": strategy.get("rules", []),
        }
        new_strategy = _new_strategy(review_date, payloads)
        new_strategy["version"] = strategy.get("version", 1) + 1
        new_strategy["created_on"] = strategy.get("created_on", review_date)
        new_strategy["first_trading_day"] = strategy.get("first_trading_day", False)
        new_strategy["change_log"] = strategy.get("change_log", []) + [
            {
                "date": review_date,
                "from_version": old_version["version"],
                "to_version": new_strategy["version"],
                "reason": "持仓与上一版策略基准不一致，自动生成新版策略。",
            }
        ]
        new_strategy["daily_reviews"] = strategy.get("daily_reviews", [])
        strategy = new_strategy

    reviews = [item for item in strategy.get("daily_reviews", []) if item.get("date") != review_date]
    reviews.append(review)
    reviews.sort(key=lambda item: str(item.get("date", "")))
    strategy["daily_reviews"] = reviews
    return strategy


def _render(strategy: Mapping[str, Any]) -> str:
    iso_year, iso_week = _iso_week(strategy["created_on"])
    lines = [
        f"# {iso_year} 年第 {iso_week} 周交易策略执行单",
        "",
        f"- 策略版本：v{strategy.get('version', 1)}",
        f"- 建立日期：{strategy.get('created_on', '')}",
        f"- 是否本周第一个交易日：{'是' if strategy.get('first_trading_day') else '否'}",
        "- 成本口径：策略建立当日 `my_stock.txt` 中的成本、股数与持有周期",
        "- 执行原则：先风控，后收益；每日复盘读取本策略单并记录是否继续执行或更新。",
        "",
        "## 基准持仓",
        "",
        "| 股票 | 代码 | 成本 | 股数 | 计划周期 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    holdings = strategy.get("baseline_holdings", {})
    items_by_code = {str(item.get("code")): item for item in strategy.get("items", [])}
    for code, holding in holdings.items():
        item = items_by_code.get(code, {})
        lines.append(
            f"| {_markdown(holding.get('name', item.get('name', '')))} | {code} | "
            f"{_markdown(holding.get('cost', ''))} | {_markdown(holding.get('shares', ''))} | "
            f"{_markdown(holding.get('plan', ''))} |"
        )

    lines.extend(
        [
            "",
            "## 个股策略执行单",
            "",
            "| 股票 | 核心策略 | 风控/支撑 | 支撑观察 | 压力减仓 | 突破确认 | 建议数量 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in strategy.get("items", []):
        buy_quantity = item.get("buy_quantity", 0)
        buy_text = f"{buy_quantity} 股（仅右侧确认）" if buy_quantity else "不左侧买入"
        lines.append(
            f"| {_markdown(item.get('name', ''))} | {_markdown(item.get('core_strategy', ''))} | "
            f"{_format(item.get('support_1'))} | {_format(item.get('support_2'))} | "
            f"{_format(item.get('pressure_1'))} | {_format(item.get('pressure_2'))} | "
            f"减仓 {item.get('reduce_quantity', 0)} 股；{buy_text} |"
        )

    lines.extend(["", "## 组合级执行规则", ""])
    lines.extend(f"- {rule}" for rule in strategy.get("rules", []))

    changes = strategy.get("change_log", [])
    if changes:
        lines.extend(["", "## 策略更新记录", ""])
        for change in changes:
            lines.append(
                f"- {change.get('date', '')}：v{change.get('from_version')} -> "
                f"v{change.get('to_version')}，{change.get('reason', '')}"
            )

    reviews = strategy.get("daily_reviews", [])
    if reviews:
        lines.extend(
            [
                "",
                "## 每日执行回顾",
                "",
                "| 日期 | 决策 | 操作执行 | 触发/更新原因 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for review in reviews:
            reasons = "；".join(review.get("update_reasons", [])) or "关键条件未触发"
            operations = "；".join(
                f"{item.get('name', '')}：{item.get('operation_status', '未记录操作')}"
                for item in review.get("statuses", [])
            ) or "未记录操作"
            lines.append(
                f"| {review.get('date', '')} | {_markdown(review.get('decision', ''))} | "
                f"{_markdown(operations)} | {_markdown(reasons)} |"
            )

    lines.extend(
        [
            "",
            "> 本策略单由公开行情、持仓成本与规则引擎自动生成，仅用于个人复盘记录，不构成投资建议。",
            "",
        ]
    )
    return "\n".join(lines)


def review_weekly_strategy(
    review_date: str,
    reviews: Sequence[Mapping[str, Any]],
) -> Tuple[str, Path, Path]:
    """Load or create the ISO-week strategy, append a daily review, and render it."""

    payloads = _load_reviews(reviews)
    if not payloads:
        return "本周交易策略单未生成：缺少本地个股数据。", *reversed(_week_files(review_date))

    json_path, markdown_path = _week_files(review_date)
    legacy_json_path, legacy_markdown_path = _legacy_week_files(review_date)
    source_json_path = json_path if json_path.is_file() else legacy_json_path
    if source_json_path.is_file():
        try:
            strategy = json.loads(source_json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"本周交易策略单读取失败：{exc}", json_path, markdown_path
        if not isinstance(strategy, dict):
            return "本周交易策略单格式无效。", json_path, markdown_path
    else:
        strategy = _new_strategy(review_date, payloads)
        if not strategy["first_trading_day"]:
            strategy["change_log"].append(
                {
                    "date": review_date,
                    "from_version": 0,
                    "to_version": 1,
                    "reason": "周内首次运行时未找到策略单，按当日持仓补建。",
                }
            )

    strategy = _apply_daily_review(strategy, review_date, payloads)
    _save_rendered(strategy, json_path, markdown_path)
    if source_json_path != json_path:
        source_json_path.unlink(missing_ok=True)
        legacy_markdown_path.unlink(missing_ok=True)
        try:
            legacy_json_path.parent.rmdir()
        except OSError:
            # 目录中可能仍保留人工文件；自动策略只清理自己生成的两个文件。
            pass
    return markdown_path.read_text(encoding="utf-8"), json_path, markdown_path
