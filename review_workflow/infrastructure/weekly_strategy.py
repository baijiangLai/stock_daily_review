"""Weekly strategy generation and daily follow-up for local reviews."""

from __future__ import annotations

import json
import math
import re
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .market_structure import classify_pattern, classify_trend


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


def _target_week_start(review_date: str) -> date:
    parsed = date.fromisoformat(review_date)
    if parsed.weekday() >= 4:  # Friday or weekend prepares the next week.
        return parsed + timedelta(days=(7 - parsed.weekday()))
    return parsed


def _week_files(review_date: str) -> Tuple[Path, Path]:
    target_week = _target_week_start(review_date)
    iso_year, iso_week = _iso_week(target_week.isoformat())
    stem = f"{iso_year}-W{iso_week:02d}"
    directory = WEEKLY_ROOT / stem
    return directory / f"{stem}_交易策略单.json", directory / f"{stem}_交易策略单.md"


def _legacy_week_files(review_date: str) -> Tuple[Path, Path]:
    target_week = _target_week_start(review_date)
    iso_year, iso_week = _iso_week(target_week.isoformat())
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
    if not isinstance(payload, dict):
        return None
    payload["_stock_dir"] = str(sidecar.parent)
    return payload


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


def _moving_average(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(closes) <= period:
        return None
    changes = [current - previous for previous, current in zip(closes, closes[1:])]
    gains = sum(change for change in changes[:period] if change > 0) / period
    losses = sum(-change for change in changes[:period] if change < 0) / period
    for change in changes[period:]:
        gains = (gains * (period - 1) + max(change, 0)) / period
        losses = (losses * (period - 1) + max(-change, 0)) / period
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def _weekly_history(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[int, int], Dict[str, Any]] = {}
    history = payload.get("history", [])
    if not isinstance(history, list):
        return []

    for raw_bar in history:
        if not isinstance(raw_bar, dict):
            continue
        bar_date = str(raw_bar.get("date", ""))[:10]
        try:
            parsed_date = date.fromisoformat(bar_date)
        except ValueError:
            continue
        iso_year, iso_week, _ = parsed_date.isocalendar()
        key = (iso_year, iso_week)
        close = _number(raw_bar.get("close"))
        if close is None:
            continue
        open_price = _number(raw_bar.get("open"))
        high = _number(raw_bar.get("high"))
        low = _number(raw_bar.get("low"))
        volume = _number(raw_bar.get("volume_hands"))
        current = grouped.get(key)
        if current is None:
            grouped[key] = {
                "week_ending": bar_date,
                "open": open_price if open_price is not None else close,
                "close": close,
                "high": high if high is not None else close,
                "low": low if low is not None else close,
                "volume_hands": volume or 0.0,
            }
            continue

        current["week_ending"] = bar_date
        current["close"] = close
        current["high"] = max(_number(current.get("high")) or close, high or close)
        current["low"] = min(_number(current.get("low")) or close, low or close)
        current["volume_hands"] = (_number(current.get("volume_hands")) or 0.0) + (
            volume or 0.0
        )
    return [grouped[key] for key in sorted(grouped)]


def _weekly_indicators(payload: Mapping[str, Any]) -> Dict[str, Any]:
    weekly = _weekly_history(payload)
    closes = [float(bar["close"]) for bar in weekly]
    latest = weekly[-1] if weekly else {}
    return {
        "week_ending": str(latest.get("week_ending") or ""),
        "close": _number(latest.get("close")),
        "ma5": _moving_average(closes, 5),
        "ma10": _moving_average(closes, 10),
        "ma20": _moving_average(closes, 20),
        "rsi12": _rsi(closes, 12),
    }


def _market_value(payload: Mapping[str, Any]) -> float:
    close = _number(payload.get("bar", {}).get("close")) or 0.0
    shares = _number(payload.get("holding", {}).get("shares")) or 0.0
    return close * shares


def _stop_loss_plan(
    close: float,
    support_2: Optional[float],
    weekly_ma10: Optional[float],
) -> Dict[str, Any]:
    candidates: List[Dict[str, Any]] = []
    if support_2 is not None:
        candidates.append(
            {
                "price": round(support_2 * 0.98, 2),
                "reason": "第二支撑下方 2%",
            }
        )
    if weekly_ma10 is not None:
        candidates.append(
            {
                "price": round(weekly_ma10 * 0.98, 2),
                "reason": "10 周线下方 2%",
            }
        )
    risk_cap = math.ceil(close * 0.92 * 100) / 100 if close > 0 else 0.0
    technical_stop = (
        max(item["price"] for item in candidates) if candidates else risk_cap
    )
    stop_loss = max(technical_stop, risk_cap)
    candidates.append(
        {"price": risk_cap, "reason": "不超过本周基准收盘价 8% 的风险预算"}
    )
    if stop_loss >= close:
        stop_loss = risk_cap
    reasons = list(dict.fromkeys(item["reason"] for item in candidates))
    return {
        "price": stop_loss,
        "risk_pct": 1 - stop_loss / close if close else None,
        "rule": "收盘价跌破止损线执行减仓/清仓；盘中跌破先降半仓，收盘确认后执行剩余部分。触发原因：" + "；".join(reasons),
    }


def _buy_plans(
    payload: Mapping[str, Any],
    *,
    close: float,
    supports: Sequence[float],
    resistances: Sequence[float],
    shares: int,
    weak_trend: bool,
    weekly: Mapping[str, Optional[float]],
) -> Dict[str, Dict[str, Any]]:
    indicators = payload.get("indicators", {})
    weekly_close = weekly.get("close")
    weekly_ma10 = weekly.get("ma10")
    weekly_structure_ok = (
        weekly_close is not None
        and weekly_ma10 is not None
        and weekly_close >= weekly_ma10
    )
    left_enabled = weekly_structure_ok and bool(supports)
    left_signal = (
        f"回踩 {_format(supports[-1])} - {_format(supports[0])} 分批承接；"
        "要求量能不高于 5 日均量 80%、RSI6 ≤ 40，且周线收盘不破 10 周线。"
    )
    weekly_below_ma10 = (
        weekly_close is not None and weekly_ma10 is not None and weekly_close < weekly_ma10
    )
    if weekly_below_ma10:
        left_signal = "暂不左侧买入：周线收盘已跌破 10 周线，等待周线趋势修复。"
    elif not weekly_structure_ok:
        left_signal = "暂不左侧买入：周线数据不足或 10 周线待核实。"

    right_trigger = (
        resistances[-1]
        if weak_trend and len(resistances) > 1
        else (resistances[0] if resistances else None)
    )
    volume_ma5 = _number(indicators.get("volume_ma5"))
    volume_text = (
        f"成交量不低于 5 日均量（{_format(volume_ma5, 2)} 手）的 1.2 倍"
        if volume_ma5 is not None
        else "成交量显著放大（5 日均量待核实）"
    )
    right_signal = (
        f"收盘站上 {_format(right_trigger)}，且日收盘位于 MA5/MA10 上方，"
        f"{volume_text}；周线需守住 5 周线或 10 周线。"
    )
    if weak_trend:
        right_signal += "弱势股只做更强确认：未收复 MA5 前禁止执行。"

    return {
        "left_side_buy": {
            "enabled": left_enabled,
            "price_zone": [supports[-1], supports[0]] if supports else [],
            "quantity": _round_lot(shares, 0.10) if left_enabled else 0,
            "signal": left_signal,
        },
        "right_side_buy": {
            "enabled": bool(resistances),
            "trigger_price": right_trigger,
            "quantity": _round_lot(shares, 0.15) if resistances else 0,
            "signal": right_signal,
        },
    }


def _latest_daily_bars(payload: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    history = payload.get("history", [])
    if not isinstance(history, list):
        return []
    bars = [bar for bar in history if isinstance(bar, dict)]
    bars.sort(key=lambda bar: str(bar.get("date", "")))
    return bars[-2:]


def _pattern_and_trend(
    payload: Mapping[str, Any],
    weekly_indicators: Mapping[str, Any],
) -> Dict[str, Dict[str, str]]:
    bar = payload.get("bar", {})
    indicators = payload.get("indicators", {})
    daily_history = _latest_daily_bars(payload)
    current_daily = bar if isinstance(bar, dict) else daily_history[-1]
    previous_daily = daily_history[-2] if len(daily_history) > 1 else None

    weekly_history = _weekly_history(payload)
    current_weekly = weekly_history[-1] if weekly_history else {}
    previous_weekly = weekly_history[-2] if len(weekly_history) > 1 else None
    current_weekly = {**current_weekly, **weekly_indicators}
    weekly_volumes = [float(item["volume_hands"]) for item in weekly_history]
    weekly_volume_ma5 = _moving_average(weekly_volumes, 5)

    return {
        "daily_pattern": classify_pattern(
            current_daily,
            previous_daily,
            _number(indicators.get("volume_ma5")),
        ),
        "daily_trend": classify_trend(
            _number(current_daily.get("close")),
            _number(indicators.get("ma5")),
            _number(indicators.get("ma10")),
            _number(indicators.get("ma20")),
            _number(indicators.get("rsi6")),
        ),
        "weekly_pattern": classify_pattern(
            current_weekly,
            previous_weekly,
            weekly_volume_ma5,
            timeframe="周线",
        ),
        "weekly_trend": classify_trend(
            _number(current_weekly.get("close")),
            _number(current_weekly.get("ma5")),
            _number(current_weekly.get("ma10")),
            _number(current_weekly.get("ma20")),
            _number(current_weekly.get("rsi12")),
            rsi_name="周线RSI12",
        ),
    }


def _strategy_item(payload: Mapping[str, Any], total_market_value: float) -> Dict[str, Any]:
    stock = payload.get("stock", {})
    holding = payload.get("holding", {})
    bar = payload.get("bar", {})
    indicators = payload.get("indicators", {})
    weekly_indicators = _weekly_indicators(payload)
    pattern_and_trend = _pattern_and_trend(payload, weekly_indicators)
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

    buy_plans = _buy_plans(
        payload,
        close=close,
        supports=supports,
        resistances=resistances,
        shares=shares,
        weak_trend=weak_trend,
        weekly=weekly_indicators,
    )
    stop_loss = _stop_loss_plan(
        close,
        supports[1] if len(supports) > 1 else None,
        weekly_indicators.get("ma10"),
    )

    item = {
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
        "buy_quantity": buy_plans["right_side_buy"]["quantity"],
        "daily_signals": {
            "close": close,
            "ma5": _number(indicators.get("ma5")),
            "ma10": _number(indicators.get("ma10")),
            "ma20": _number(indicators.get("ma20")),
            "rsi6": _number(indicators.get("rsi6")),
            "rsi12": _number(indicators.get("rsi12")),
            "volume_hands": _number(payload.get("bar", {}).get("volume_hands")),
            "volume_ma5": _number(indicators.get("volume_ma5")),
        },
        "weekly_signals": weekly_indicators,
        **pattern_and_trend,
        **buy_plans,
        "stop_loss": stop_loss,
    }
    return item


def _new_strategy(review_date: str, payloads: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    total_market_value = sum(_market_value(payload) for payload in payloads)
    items = [_strategy_item(payload, total_market_value) for payload in payloads]
    items.sort(key=lambda item: (-item["weight"] if item["weight"] is not None else 0, item["code"]))
    target_week = _target_week_start(review_date)
    prepared_before_week = target_week.isoformat() > review_date

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
        "signal_schema": 3,
        "week": None,
        "created_on": review_date,
        "target_week_start": target_week.isoformat(),
        "prepared_before_week": prepared_before_week,
        "first_trading_day": (
            not prepared_before_week and _is_first_trading_day(review_date, payloads)
        ),
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


def _chart_filename(stock: Mapping[str, Any], chart_type: str) -> str:
    name = re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "_",
        f"{stock.get('name', '')}_{stock.get('code', '')}",
        flags=re.UNICODE,
    ).strip("_")
    return f"{name or 'stock'}_{chart_type}.png"


def _copy_chart_snapshots(
    strategy: Dict[str, Any],
    payloads: Sequence[Mapping[str, Any]],
    json_path: Path,
) -> None:
    chart_root = json_path.parent / "charts"
    chart_root.mkdir(parents=True, exist_ok=True)
    items_by_code = {
        str(item.get("code")): item for item in strategy.get("items", [])
    }

    for payload in payloads:
        stock = payload.get("stock", {})
        code = str(stock.get("code", ""))
        item = items_by_code.get(code)
        if item is None:
            continue

        stock_dir = Path(str(payload.get("_stock_dir", "")))
        try:
            metadata = json.loads(
                (stock_dir / "metadata.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            metadata = {}
        names = metadata.get("screenshots", {}) if isinstance(metadata, dict) else {}
        if not isinstance(names, dict):
            names = {}

        snapshots: Dict[str, Optional[str]] = {
            "daily_kline": None,
            "weekly_kline": None,
        }
        for chart_type in snapshots:
            filename = names.get(chart_type)
            source = stock_dir / str(filename) if filename else stock_dir / f"04_{chart_type}.png"
            if not source.is_file():
                continue
            target = chart_root / _chart_filename(stock, chart_type)
            shutil.copy2(source, target)
            snapshots[chart_type] = str(target.relative_to(json_path.parent))
        item["chart_snapshots"] = snapshots


def _upgrade_signal_schema(
    strategy: Dict[str, Any],
    review_date: str,
    payloads: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    if strategy.get("signal_schema") == 3:
        return strategy
    if strategy.get("baseline_holdings", {}) != _current_holdings(payloads):
        return strategy

    upgraded = _new_strategy(review_date, payloads)
    upgraded["version"] = strategy.get("version", 1) + 1
    upgraded["created_on"] = strategy.get("created_on", review_date)
    upgraded["first_trading_day"] = strategy.get("first_trading_day", False)
    upgraded["prepared_before_week"] = strategy.get(
        "prepared_before_week", upgraded["prepared_before_week"]
    )
    upgraded["target_week_start"] = strategy.get(
        "target_week_start", upgraded["target_week_start"]
    )
    upgraded["change_log"] = strategy.get("change_log", []) + [
        {
            "date": review_date,
            "from_version": strategy.get("version", 1),
            "to_version": upgraded["version"],
            "reason": "策略格式升级 v3：补充日线/周线形态与趋势定义，并同步左右侧买入与止损线。",
        }
    ]
    upgraded["daily_reviews"] = strategy.get("daily_reviews", [])
    return upgraded


def _save_rendered(strategy: Dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    target_week = str(strategy.get("target_week_start") or strategy["created_on"])
    iso_year, iso_week = _iso_week(target_week)
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


def _stop_loss_price(item: Mapping[str, Any]) -> Optional[float]:
    plan = item.get("stop_loss", {})
    return _number(plan.get("price")) if isinstance(plan, dict) else None


def _item_status(item: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    close = _number(payload.get("bar", {}).get("close"))
    if close is None:
        return "行情待核实"
    stop_loss = _stop_loss_price(item)
    if stop_loss is not None and close <= stop_loss:
        return "已触发止损线"
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
    stop_loss = _stop_loss_price(item)
    if stop_loss is not None and price <= stop_loss:
        return "操作价低于止损线：与周策略风控冲突"
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
        if "支撑/风控" in status or "突破" in status or "止损" in status:
            update_reasons.append(f"{item.get('name', '')} {status}")
        if any(
            keyword in current_operation_status
            for keyword in ("追高", "执行质量偏差", "风控冲突")
        ):
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
        new_strategy = _new_strategy(review_date, payloads)
        new_strategy["version"] = strategy.get("version", 1) + 1
        new_strategy["created_on"] = strategy.get("created_on", review_date)
        new_strategy["first_trading_day"] = strategy.get("first_trading_day", False)
        new_strategy["change_log"] = strategy.get("change_log", []) + [
                {
                    "date": review_date,
                    "from_version": strategy.get("version", 1),
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
    target_week = str(strategy.get("target_week_start") or strategy["created_on"])
    iso_year, iso_week = _iso_week(target_week)
    prepared_before_week = bool(strategy.get("prepared_before_week"))
    lines = [
        f"# {iso_year} 年第 {iso_week} 周交易策略执行单",
        "",
        f"- 策略版本：v{strategy.get('version', 1)}",
        f"- 建立日期：{strategy.get('created_on', '')}",
        f"- 适用周起始日：{target_week}",
        f"- 生成方式：{'周五/周末提前生成下一周策略' if prepared_before_week else '本周运行时生成/更新'}",
        f"- 是否本周第一个交易日：{'是' if strategy.get('first_trading_day') else '否'}",
        "- 成本口径：策略建立当日 `my_stock.txt` 中的成本、股数与持有周期",
        "- 执行原则：先止损和仓位，后左侧/右侧买入；每日复盘读取本策略单并记录是否继续执行或更新。",
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
        stop_loss = item.get("stop_loss", {})
        lines.append(
            f"| {_markdown(item.get('name', ''))} | {_markdown(item.get('core_strategy', ''))} | "
            f"{_format(item.get('support_1'))} | {_format(item.get('support_2'))} | "
            f"{_format(item.get('pressure_1'))} | {_format(item.get('pressure_2'))} | "
            f"减仓 {item.get('reduce_quantity', 0)} 股；{buy_text}；"
            f"止损 {_format(_number(stop_loss.get('price')) if isinstance(stop_loss, dict) else None)} |"
        )

    lines.extend(
        [
            "",
            "## 形态与趋势定义",
            "",
            "| 股票 | 日线形态 | 日线趋势 | 周线形态 | 周线趋势 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in strategy.get("items", []):
        fields = (
            ("daily_pattern", "形态待核实"),
            ("daily_trend", "趋势待核实"),
            ("weekly_pattern", "形态待核实"),
            ("weekly_trend", "趋势待核实"),
        )
        values = []
        for key, fallback_name in fields:
            definition = item.get(key, {})
            if not isinstance(definition, dict):
                definition = {}
            values.append(
                f"{definition.get('name', fallback_name)}："
                f"{definition.get('definition', '定义待核实')}"
            )
        lines.append(
            f"| {_markdown(item.get('name', ''))} | {_markdown(values[0])} | "
            f"{_markdown(values[1])} | {_markdown(values[2])} | "
            f"{_markdown(values[3])} |"
        )

    lines.extend(["", "## 日线/周线截图", ""])
    for item in strategy.get("items", []):
        snapshots = item.get("chart_snapshots", {})
        daily = snapshots.get("daily_kline") if isinstance(snapshots, dict) else None
        weekly = snapshots.get("weekly_kline") if isinstance(snapshots, dict) else None
        lines.append(f"### {_markdown(item.get('name', ''))}")
        if daily:
            lines.append(f"- 日线：![{item.get('name', '')} 日线]({daily})")
        else:
            lines.append("- 日线：待补抓")
        if weekly:
            lines.append(f"- 周线：![{item.get('name', '')} 周线]({weekly})")
        else:
            lines.append("- 周线：待补抓（周五/周末截图会自动生成并复制到本目录）")
        lines.append("")

    lines.extend(
        [
            "## 左右侧买入信号与止损",
            "",
            "| 股票 | 左侧买入 | 右侧突破 | 止损线 |",
            "| --- | --- | --- | ---: |",
        ]
    )
    for item in strategy.get("items", []):
        left = item.get("left_side_buy", {})
        right = item.get("right_side_buy", {})
        stop_loss = item.get("stop_loss", {})
        left_enabled = bool(left.get("enabled")) if isinstance(left, dict) else False
        left_text = (
            f"{'启用' if left_enabled else '停用'}；区间 "
            f"{_format(left.get('price_zone')[0])} - {_format(left.get('price_zone')[-1])}；"
            f"{left.get('signal', '')}"
            if isinstance(left, dict) and left.get("price_zone")
            else "待核实"
        )
        right_text = (
            f"触发 {_format(right.get('trigger_price'))}，买入 {right.get('quantity', 0)} 股；"
            f"{right.get('signal', '')}"
            if isinstance(right, dict)
            else "待核实"
        )
        stop_text = (
            f"{_format(stop_loss.get('price'))}；{stop_loss.get('rule', '')}"
            if isinstance(stop_loss, dict)
            else "待核实"
        )
        lines.append(
            f"| {_markdown(item.get('name', ''))} | {_markdown(left_text)} | "
            f"{_markdown(right_text)} | {_markdown(stop_text)} |"
        )

    lines.extend(
        [
            "",
            "## 指标快照",
            "",
            "| 股票 | 日线收盘 | 日 MA5 / MA10 / MA20 | 日 RSI6 / RSI12 | 日量能 / 5日均量 | 周线收盘 | 周 MA5 / MA10 / MA20 | 周 RSI12 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in strategy.get("items", []):
        daily = item.get("daily_signals", {})
        weekly = item.get("weekly_signals", {})
        if not isinstance(daily, dict):
            daily = {}
        if not isinstance(weekly, dict):
            weekly = {}
        lines.append(
            f"| {_markdown(item.get('name', ''))} | {_format(daily.get('close'))} | "
            f"{_format(daily.get('ma5'))} / {_format(daily.get('ma10'))} / {_format(daily.get('ma20'))} | "
            f"{_format(daily.get('rsi6'))} / {_format(daily.get('rsi12'))} | "
            f"{_format(daily.get('volume_hands'), 2)} 手 / {_format(daily.get('volume_ma5'), 2)} 手 | "
            f"{_format(weekly.get('close'))}（{weekly.get('week_ending', '待核实')}） | "
            f"{_format(weekly.get('ma5'))} / {_format(weekly.get('ma10'))} / {_format(weekly.get('ma20'))} | "
            f"{_format(weekly.get('rsi12'))} |"
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
        strategy = _upgrade_signal_schema(strategy, review_date, payloads)
    else:
        strategy = _new_strategy(review_date, payloads)
        if not strategy["first_trading_day"]:
            strategy["change_log"].append(
                {
                    "date": review_date,
                    "from_version": 0,
                    "to_version": 1,
                    "reason": (
                        "周五/周末收盘后按最新持仓提前生成下一周策略。"
                        if strategy["prepared_before_week"]
                        else "周内首次运行时未找到策略单，按当日持仓补建。"
                    ),
                }
            )

    strategy = _apply_daily_review(strategy, review_date, payloads)
    _copy_chart_snapshots(strategy, payloads, json_path)
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
