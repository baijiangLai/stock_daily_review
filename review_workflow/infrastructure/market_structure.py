"""Deterministic candlestick and trend definitions for local reviews."""

from __future__ import annotations

import math
from typing import Any, Dict, Literal, Mapping, Optional


Timeframe = Literal["日线", "周线"]


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid_candle(
    open_price: Optional[float],
    close: Optional[float],
    high: Optional[float],
    low: Optional[float],
) -> bool:
    return (
        None not in (open_price, close, high, low)
        and high is not None
        and low is not None
        and high > low
        and low > 0
        and low <= open_price <= high
        and low <= close <= high
    )


def classify_pattern(
    current: Mapping[str, Any],
    previous: Optional[Mapping[str, Any]] = None,
    volume_ma5: Optional[float] = None,
    timeframe: Timeframe = "日线",
) -> Dict[str, str]:
    """Classify a candle and return an explicit, threshold-based definition."""

    if timeframe not in {"日线", "周线"}:
        return {
            "name": "形态待核实",
            "definition": "定义：当前仅支持日线和周线形态，无法识别传入的周期。",
        }

    open_price = _number(current.get("open"))
    close = _number(current.get("close"))
    high = _number(current.get("high"))
    low = _number(current.get("low"))
    volume = _number(current.get("volume_hands"))
    if not _valid_candle(open_price, close, high, low) or (
        volume is not None and volume < 0
    ):
        return {
            "name": "形态待核实",
            "definition": "OHLC 数据缺失、最高价不高于最低价，或开盘价/收盘价不在最高价与最低价区间内，无法定义有效 K 线形态。",
        }

    period = "日" if timeframe == "日线" else "周"
    current_period = "当日" if timeframe == "日线" else "当周"
    next_period = "次日" if timeframe == "日线" else "下一周"
    volume_average_name = f"5{period}均量（含当期）"

    body = abs(close - open_price)
    upper_shadow = high - max(open_price, close)
    lower_shadow = min(open_price, close) - low
    bullish = close > open_price
    bearish = close < open_price
    volume_ratio = (
        volume / volume_ma5
        if volume is not None and volume >= 0 and volume_ma5 and volume_ma5 > 0
        else None
    )
    volume_text = (
        f"；{current_period}量能约为{volume_average_name} {volume_ratio:.2f} 倍"
        if volume_ratio is not None
        else ""
    )

    if previous is not None:
        prev_open = _number(previous.get("open"))
        prev_close = _number(previous.get("close"))
        prev_high = _number(previous.get("high"))
        prev_low = _number(previous.get("low"))
        prev_body = (
            abs(prev_close - prev_open)
            if prev_open is not None and prev_close is not None
            else None
        )
        previous_valid = _valid_candle(prev_open, prev_close, prev_high, prev_low)
        if (
            bullish
            and prev_open is not None
            and prev_close is not None
            and previous_valid
            and prev_close < prev_open
            and close >= prev_open
            and open_price <= prev_close
            and (prev_body == 0 or body >= prev_body * 0.8)
        ):
            return {
                "name": "阳线反包",
                "definition": f"定义：{current_period}阳线实体覆盖前一根阴线实体的至少 80%，开盘价不高于前阴线收盘价、收盘价不低于前阴线开盘价，并重新站上前一根阴线开盘价" + volume_text + f"；表示空方抛压被承接，需{next_period}继续收在反包实体上方确认。",
            }
        if (
            bearish
            and prev_open is not None
            and prev_close is not None
            and previous_valid
            and prev_close > prev_open
            and close <= prev_open
            and open_price >= prev_close
            and (prev_body == 0 or body >= prev_body * 0.8)
        ):
            return {
                "name": "阴线反包",
                "definition": f"定义：{current_period}阴线实体覆盖前一根阳线实体的至少 80%，开盘价不低于前阳线收盘价、收盘价不高于前阳线开盘价，并跌破前一根阳线开盘价" + volume_text + "；表示多方拉升被反向承接，需防范趋势转弱。",
            }

    if body > 0 and lower_shadow >= body * 2 and lower_shadow > upper_shadow * 1.5:
        return {
            "name": "长下影线",
            "definition": f"定义：下影线至少达到 K 线实体的 2 倍，且至少为上影线 1.5 倍" + volume_text + f"；表示{current_period}下探后出现承接，但需{next_period}不再创新低确认。",
        }
    if body > 0 and upper_shadow >= body * 2 and upper_shadow > lower_shadow * 1.5:
        return {
            "name": "长上影线",
            "definition": f"定义：上影线至少达到 K 线实体的 2 倍，且至少为下影线 1.5 倍" + volume_text + f"；表示{current_period}冲高后遇到抛压，需防{next_period}回落确认。",
        }
    if body / (high - low) <= 0.15:
        return {
            "name": "十字星",
            "definition": f"定义：K 线实体不超过全{period}振幅的 15%，开盘价与收盘价接近" + volume_text + "；表示多空暂时平衡，后续突破方向决定形态含义。",
        }
    if bullish and volume_ratio is not None and volume_ratio >= 1.2:
        return {
            "name": "放量阳线",
            "definition": f"定义：收盘价高于开盘价，且{current_period}成交量达到{volume_average_name} 1.2 倍以上；表示主动买入参与度增强，需结合压力位判断是否有效突破。",
        }
    if bearish and volume_ratio is not None and volume_ratio >= 1.2:
        return {
            "name": "放量阴线",
            "definition": f"定义：收盘价低于开盘价，且{current_period}成交量达到{volume_average_name} 1.2 倍以上；表示抛压放大，需警惕破位或派发风险。",
        }
    if bullish:
        return {
            "name": "普通阳线",
            "definition": "定义：收盘价高于开盘价，但未触发反包、长影线或显著放量条件" + volume_text + "；短线信号需结合均线与关键价位确认。",
        }
    if bearish:
        return {
            "name": "普通阴线",
            "definition": "定义：收盘价低于开盘价，但未触发反包、长影线或显著放量条件" + volume_text + "；短线信号需结合均线与关键价位确认。",
        }
    return {
        "name": "平盘线",
        "definition": "定义：收盘价与开盘价接近或相同" + volume_text + "；多空方向待下一根 K 线确认。",
    }


def classify_trend(
    close: Optional[float],
    ma5: Optional[float],
    ma10: Optional[float],
    ma20: Optional[float],
    rsi: Optional[float] = None,
    rsi_name: str = "RSI6",
) -> Dict[str, str]:
    """Classify trend by price, moving-average alignment, and RSI state."""

    if close is None or None in (ma5, ma10, ma20):
        return {
            "name": "趋势待核实",
            "definition": "定义：收盘价或 MA5/MA10/MA20 数据不足，无法确认趋势结构。",
        }

    rsi_value = f"{rsi:.2f}" if rsi is not None else "待核实"
    rsi_text = f"{rsi_name} 为 {rsi_value}；"
    if rsi is None:
        rsi_state = f"{rsi_name}待核实。"
    elif rsi >= 70:
        rsi_state = f"{rsi_name}进入强势过热区，趋势仍强但追高风险上升。"
    elif rsi <= 30:
        rsi_state = f"{rsi_name}进入超卖区，存在修复弹性，但不代表趋势自动反转。"
    else:
        rsi_state = f"{rsi_name}位于中性区，趋势延续概率主要取决于均线排列。"

    above_all = close >= ma5 and close >= ma10 and close >= ma20
    below_all = close < ma5 and close < ma10 and close < ma20
    if above_all and ma5 > ma10 > ma20:
        return {
            "name": "强上升趋势",
            "definition": "定义：收盘价站上 MA5/MA10/MA20，且 MA5>MA10>MA20 形成多头排列。" + rsi_text + rsi_state,
        }
    if above_all:
        return {
            "name": "回升趋势",
            "definition": "定义：收盘价已站上 MA5/MA10/MA20，但三条均线尚未完成多头排列。" + rsi_text + rsi_state,
        }
    if below_all and ma5 < ma10 < ma20:
        return {
            "name": "空头下跌趋势",
            "definition": "定义：收盘价低于 MA5/MA10/MA20，且 MA5<MA10<MA20 形成空头排列。" + rsi_text + rsi_state,
        }
    if below_all:
        return {
            "name": "弱势破位",
            "definition": "定义：收盘价已跌破 MA5/MA10/MA20，但均线仍处交错状态。" + rsi_text + rsi_state,
        }
    return {
        "name": "震荡整理",
        "definition": "定义：收盘价与 MA5/MA10/MA20 的相对位置交错，未形成单边排列。" + rsi_text + rsi_state,
    }
