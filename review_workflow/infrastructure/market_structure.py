"""Deterministic candlestick and trend definitions for local reviews."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple


Timeframe = Literal["日线", "周线"]
TrendDirection = Literal["加强", "稳定", "减弱", "反转", "待核实"]

# 趋势强度 / 趋势变化的判定参考量。这些只是“变化是否显著”的参考尺度，
# 不参与任何买卖触发判断，买卖条件仍由原有左右侧与止损规则决定。
_FLAT_SLOPE = 0.002
_SLOPE_CHANGE_REFERENCE = 0.01
_DISTANCE_CHANGE_REFERENCE = 0.02
_RSI_CHANGE_REFERENCE = 8.0
_VOLUME_CHANGE_REFERENCE = 0.3
_TREND_CHANGE_THRESHOLD = 8.0
_MA20_REVERSAL_FLOOR = 0.003
_OVERHEAT_DISTANCE = 0.12
_MIN_TREND_BARS = 21


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


def _average(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi_value(closes: Sequence[float], period: int) -> Optional[float]:
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


def _slope_state(value: Optional[float]) -> str:
    if value is None:
        return "待核实"
    if value > _FLAT_SLOPE:
        return "上升"
    if value < -_FLAT_SLOPE:
        return "下降"
    return "走平"


def _average_through_index(
    values: Sequence[float], end_index: int, period: int
) -> Optional[float]:
    start_index = end_index - period + 1
    if start_index < 0:
        return None
    return sum(values[start_index:end_index + 1]) / period


def _percent_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def _trend_sign(name: str) -> int:
    if name in {"强上升趋势", "回升趋势"}:
        return 1
    if name in {"空头下跌趋势", "弱势破位"}:
        return -1
    return 0


def _clamp_unit(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _trend_orientation(current_sign: int, previous_sign: int) -> int:
    """趋势变化以当前方向为主；转入震荡时沿用前期方向，用于衡量动能是否衰减。"""

    if current_sign != 0:
        return current_sign
    return previous_sign


def _ma20_reversal(
    current_slopes: Mapping[str, Optional[float]],
    previous_slopes: Mapping[str, Optional[float]],
    current_sign: int,
) -> bool:
    """中期均线斜率由升转降（或由降转升）视为趋势反转信号。"""

    if current_sign == 0:
        return False
    current = current_slopes.get("ma20")
    previous = previous_slopes.get("ma20")
    if current is None or previous is None or current * previous >= 0:
        return False
    return abs(current) >= _MA20_REVERSAL_FLOOR and abs(previous) >= _MA20_REVERSAL_FLOOR


def _trend_change_momentum(
    *,
    orientation: int,
    current_slopes: Mapping[str, Optional[float]],
    previous_slopes: Mapping[str, Optional[float]],
    price_distance_to_ma20: Optional[float],
    previous_price_distance: Optional[float],
    rsi: Optional[float],
    previous_rsi: Optional[float],
    rsi_name: str,
    volume_change: Optional[float],
    compare_bars: int,
) -> Tuple[Optional[float], List[str]]:
    """比较当前窗口与前 N 期窗口，输出趋势变化的动能得分与依据。

    得分只用于把变化归入「加强 / 稳定 / 减弱」，不代表买卖信号。
    数据不足时返回 ``None``，由调用方输出「待核实」。
    """

    if orientation == 0:
        return 0.0, ["当前结构未形成单边趋势，趋势变化按稳定处理。"]

    momentum = 0.0
    components = 0
    evidence: List[str] = []

    for period, weight in ((5, 6.0), (10, 4.0), (20, 3.0)):
        current_slope = current_slopes.get(f"ma{period}")
        previous_slope = previous_slopes.get(f"ma{period}")
        if current_slope is None or previous_slope is None:
            continue
        delta = current_slope - previous_slope
        momentum += _clamp_unit(delta / _SLOPE_CHANGE_REFERENCE) * weight * orientation
        components += 1
        if abs(delta) < _SLOPE_CHANGE_REFERENCE * 0.25:
            state = "基本一致"
        elif (delta > 0) == (orientation > 0):
            state = "加快"
        else:
            state = "放缓"
        evidence.append(f"MA{period} 斜率较前期{state}（{delta:+.2%}）")

    if price_distance_to_ma20 is not None and previous_price_distance is not None:
        aligned_delta = (price_distance_to_ma20 - previous_price_distance) * orientation
        momentum += _clamp_unit(aligned_delta / _DISTANCE_CHANGE_REFERENCE) * 4.0
        components += 1
        if abs(aligned_delta) < _DISTANCE_CHANGE_REFERENCE * 0.25:
            state = "基本一致"
        else:
            state = "扩大" if aligned_delta > 0 else "收窄"
        evidence.append(f"价格与 MA20 距离较前期{state}（{aligned_delta:+.2%}）")

    if rsi is not None and previous_rsi is not None:
        aligned_delta = (rsi - previous_rsi) * orientation
        momentum += _clamp_unit(aligned_delta / _RSI_CHANGE_REFERENCE) * 5.0
        components += 1
        if aligned_delta > 1:
            state = "上升"
        elif aligned_delta < -1:
            state = "下降"
        else:
            state = "基本持平"
        evidence.append(f"{rsi_name} 较前期{state}（{rsi - previous_rsi:+.2f}）")

    if volume_change is not None:
        momentum += (
            _clamp_unit(volume_change * orientation / _VOLUME_CHANGE_REFERENCE) * 3.0
        )
        components += 1
        if volume_change > 0.05:
            state = "放大"
        elif volume_change < -0.05:
            state = "缩小"
        else:
            state = "基本持平"
        evidence.append(f"成交量较前{compare_bars}期均量{state}（{volume_change:+.2%}）")

    if components == 0:
        return None, evidence
    return momentum, evidence


def _trend_strength_score(
    state: Mapping[str, str],
    close: float,
    ma_values: Sequence[Optional[float]],
    slopes: Mapping[str, Optional[float]],
    rsi: Optional[float],
    price_distance_to_ma20: Optional[float],
) -> Tuple[int, bool]:
    sign = _trend_sign(state["name"])
    if sign == 0:
        score = 8
    elif state["name"] in {"强上升趋势", "空头下跌趋势"}:
        score = 28
    else:
        score = 18

    for value in ma_values:
        if value is not None and (close - value) * sign > 0:
            score += 5
    if ma_values[0] is not None and ma_values[1] is not None:
        if (ma_values[0] - ma_values[1]) * sign > 0:
            score += 6
    if ma_values[1] is not None and ma_values[2] is not None:
        if (ma_values[1] - ma_values[2]) * sign > 0:
            score += 6
    for slope in slopes.values():
        if slope is not None and slope * sign > 0:
            score += 5
    if rsi is not None and (
        (sign > 0 and rsi >= 55) or (sign < 0 and rsi <= 45)
    ):
        score += 5

    extended = False
    if price_distance_to_ma20 is not None:
        distance = abs(price_distance_to_ma20)
        if distance <= _OVERHEAT_DISTANCE:
            score += min(15, round(distance / _OVERHEAT_DISTANCE * 15))
        else:
            extended = True
            score += 8
    return min(100, max(0, score)), extended


def _strength_name(score: Optional[int]) -> str:
    if score is None:
        return "待核实"
    if score >= 70:
        return "强"
    if score >= 40:
        return "中"
    return "弱"


def analyze_trend_from_bars(
    bars: Sequence[Mapping[str, Any]],
    *,
    timeframe: Timeframe = "日线",
    rsi_period: int = 6,
    compare_bars: int = 5,
) -> Dict[str, Any]:
    """Build trend state, strength, and change from deterministic bar series."""

    valid_bars = [
        bar
        for bar in bars
        if _number(bar.get("close")) is not None and _number(bar.get("close")) > 0
    ]
    valid_bars.sort(key=lambda bar: str(bar.get("date", bar.get("week_ending", ""))))

    def insufficient(reason: str) -> Dict[str, Any]:
        return {
            **classify_trend(None, None, None, None),
            "timeframe": timeframe,
            "strength_score": None,
            "strength": "待核实",
            "direction": "待核实",
            "direction_text": "待核实",
            "price_distance_to_ma20": None,
            "ma_slopes": {},
            "rsi_change": None,
            "volume_change": None,
            "evidence": [reason],
            "interpretation": "趋势强度与趋势变化待核实：" + reason,
            "overheated": False,
            "oversold": False,
        }

    if len(valid_bars) < _MIN_TREND_BARS:
        return insufficient(
            f"K 线仅 {len(valid_bars)} 根，不足以计算 MA20 与前后窗口对比。"
        )

    closes = [float(_number(bar.get("close")) or 0.0) for bar in valid_bars]
    current = valid_bars[-1]
    current_index = len(valid_bars) - 1
    previous_index = max(0, current_index - compare_bars)
    previous_closes = closes[: previous_index + 1]

    ma5 = _average(closes, 5)
    ma10 = _average(closes, 10)
    ma20 = _average(closes, 20)
    previous_ma5 = _average(previous_closes, 5)
    previous_ma10 = _average(previous_closes, 10)
    previous_ma20 = _average(previous_closes, 20)
    ma_values = (ma5, ma10, ma20)
    slopes = {
        "ma5": (ma5 - previous_ma5) / previous_ma5 if ma5 is not None and previous_ma5 else None,
        "ma10": (ma10 - previous_ma10) / previous_ma10 if ma10 is not None and previous_ma10 else None,
        "ma20": (ma20 - previous_ma20) / previous_ma20 if ma20 is not None and previous_ma20 else None,
    }
    close = closes[-1]
    rsi_name = "RSI6" if timeframe == "日线" else "周线RSI12"
    rsi = _rsi_value(closes, rsi_period)
    previous_rsi = _rsi_value(previous_closes, rsi_period)
    distance = (close - ma20) / ma20 if ma20 else None
    previous_distance = (
        (previous_closes[-1] - previous_ma20) / previous_ma20
        if previous_ma20
        else None
    )
    state = classify_trend(close, ma5, ma10, ma20, rsi, rsi_name=rsi_name)
    score, extended = _trend_strength_score(
        state, close, ma_values, slopes, rsi, distance
    )
    # 偏离 MA20 超过阈值时区分「短期过热」与「短期超跌」，两者都不能直接当成买卖理由。
    overheated = bool(extended and distance is not None and distance > 0)
    oversold = bool(extended and distance is not None and distance < 0)

    previous_state = classify_trend(
        previous_closes[-1],
        previous_ma5,
        previous_ma10,
        previous_ma20,
        previous_rsi,
        rsi_name=rsi_name,
    )
    def previous_slope(period: int) -> Optional[float]:
        current_value = _average_through_index(
            previous_closes, previous_index, period
        )
        baseline = _average_through_index(
            previous_closes, previous_index - compare_bars, period
        )
        return _percent_change(current_value, baseline)

    previous_slopes = {
        "ma5": previous_slope(5),
        "ma10": previous_slope(10),
        "ma20": previous_slope(20),
    }
    volumes = [
        _number(bar.get("volume_hands"))
        for bar in valid_bars
        if _number(bar.get("volume_hands")) is not None
    ]
    volume_change: Optional[float] = None
    if len(volumes) > compare_bars and volumes[-1] is not None:
        previous_average_volume = sum(volumes[-(compare_bars + 1):-1]) / compare_bars
        if previous_average_volume > 0:
            volume_change = volumes[-1] / previous_average_volume - 1

    current_sign = _trend_sign(state["name"])
    previous_sign = _trend_sign(previous_state["name"])
    orientation = _trend_orientation(current_sign, previous_sign)
    momentum_change, change_evidence = _trend_change_momentum(
        orientation=orientation,
        current_slopes=slopes,
        previous_slopes=previous_slopes,
        price_distance_to_ma20=distance,
        previous_price_distance=previous_distance,
        rsi=rsi,
        previous_rsi=previous_rsi,
        rsi_name=rsi_name,
        volume_change=volume_change,
        compare_bars=compare_bars,
    )

    if momentum_change is None:
        direction = "待核实"
    elif _ma20_reversal(slopes, previous_slopes, current_sign):
        direction = "反转"
    elif momentum_change >= _TREND_CHANGE_THRESHOLD:
        direction = "加强"
    elif momentum_change <= -_TREND_CHANGE_THRESHOLD:
        direction = "减弱"
    else:
        direction = "稳定"

    evidence: List[str] = []
    if ma5 is not None and ma10 is not None and ma20 is not None:
        if ma5 > ma10 > ma20:
            evidence.append("MA5 > MA10 > MA20，均线多头排列")
        elif ma5 < ma10 < ma20:
            evidence.append("MA5 < MA10 < MA20，均线空头排列")
        else:
            evidence.append("MA5/MA10/MA20 位置交错，未形成单边排列")
    evidence.extend(
        f"MA{period} {_slope_state(slope)}"
        for period, slope in ((5, slopes["ma5"]), (10, slopes["ma10"]), (20, slopes["ma20"]))
    )
    if distance is not None:
        position = "上方" if distance >= 0 else "下方"
        evidence.append(f"价格位于 MA20 {position} {abs(distance):.2%}")
    if rsi is not None:
        evidence.append(f"{rsi_name} 为 {rsi:.2f}")
    evidence.extend(change_evidence)

    strength = _strength_name(score)
    # 箭头表示趋势推进的方向，而不是单纯的动能升降：
    # 上升趋势加强记 ↑、减弱记 ↓；下跌趋势加强记 ↓、减弱记 ↑。
    rising = orientation >= 0
    direction_text = {
        "加强": "↑ 加强" if rising else "↓ 加强",
        "减弱": "↓ 减弱" if rising else "↑ 减弱",
        "反转": "↻ 反转",
        "待核实": "待核实",
    }.get(direction, "→ 稳定")
    interpretation = (
        f"{timeframe}趋势状态为{state['name']}，趋势强度{strength}，趋势变化{direction}。"
    )
    if overheated:
        interpretation += (
            f"价格距离 MA20 超过 {_OVERHEAT_DISTANCE:.0%}，"
            "趋势虽强但短期过热，不宜把偏离度简单理解为可以追买。"
        )
    if oversold:
        interpretation += (
            f"价格低于 MA20 超过 {_OVERHEAT_DISTANCE:.0%}，属于短期超跌状态，"
            "超跌反弹不等同于趋势反转。"
        )
    if direction == "减弱":
        interpretation += (
            "上涨动能较前期减弱，但不因此直接改变趋势状态，仍需观察支撑区是否失守。"
            if rising
            else "下跌动能较前期减弱，但尚未出现趋势反转确认，超跌反弹不等于趋势修复。"
        )
    if direction == "加强":
        interpretation += (
            "上涨动能较前期增强，是否加仓仍取决于压力区是否被有效突破。"
            if rising
            else "下跌动能较前期增强，应优先控制仓位与风险，不因超跌而左侧补仓。"
        )
    if direction == "反转":
        interpretation += "中期均线斜率已经反向，趋势存在反转风险，应按趋势状态与支撑压力区重新评估。"
    return {
        **state,
        "timeframe": timeframe,
        "strength_score": score,
        "strength": strength,
        "direction": direction,
        "direction_text": direction_text,
        "price_distance_to_ma20": distance,
        "ma_slopes": slopes,
        "rsi_change": rsi - previous_rsi if rsi is not None and previous_rsi is not None else None,
        "volume_change": volume_change,
        "evidence": evidence,
        "interpretation": interpretation,
        "overheated": overheated,
        "oversold": oversold,
    }


def _zone_price(value: float) -> float:
    digits = 2 if value >= 10 else (3 if value >= 1 else 4)
    return round(value, digits)


def _new_zone(
    center: float,
    zone_type: str,
    reason: str,
    *,
    kind: str = "extreme",
    touches: int = 1,
    prominent: bool = False,
    long_term: bool = False,
    volume_dense: bool = False,
    half_width_pct: float = 0.004,
) -> Dict[str, Any]:
    width = center * half_width_pct
    return {
        "zone_type": zone_type,
        "zone_low": _zone_price(center - width),
        "zone_high": _zone_price(center + width),
        "reasons": [reason],
        "kinds": [kind],
        "touches": touches,
        "prominent": prominent,
        "long_term": long_term,
        "volume_dense": volume_dense,
    }


def _merge_zones(zones: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(
        zones,
        key=lambda zone: (zone["zone_low"] + zone["zone_high"]) / 2,
    )
    merged: List[Dict[str, Any]] = []
    for zone in ordered:
        if not merged:
            merged.append(dict(zone))
            continue
        target = merged[-1]
        target_center = (target["zone_low"] + target["zone_high"]) / 2
        new_center = (zone["zone_low"] + zone["zone_high"]) / 2
        next_low = min(target["zone_low"], zone["zone_low"])
        next_high = max(target["zone_high"], zone["zone_high"])
        can_merge = (
            zone["zone_low"] <= target["zone_high"] * 1.002
            and zone["zone_high"] >= target["zone_low"] * 0.998
            and (next_high - next_low) / max(next_low, 1) <= 0.045
            and abs(new_center - target_center) / max(target_center, 1) <= 0.025
        )
        if can_merge:
            target["zone_low"] = _zone_price(next_low)
            target["zone_high"] = _zone_price(next_high)
            target["reasons"] = list(dict.fromkeys(target["reasons"] + zone["reasons"]))
            target["kinds"] = list(
                dict.fromkeys(target.get("kinds", []) + zone.get("kinds", []))
            )
            target["touches"] = target.get("touches", 1) + zone.get("touches", 1)
            target["prominent"] = target.get("prominent", False) or zone.get("prominent", False)
            target["long_term"] = target.get("long_term", False) or zone.get("long_term", False)
            target["volume_dense"] = (
                target.get("volume_dense", False) or zone.get("volume_dense", False)
            )
        else:
            merged.append(dict(zone))
    return merged


_KIND_WEIGHTS = {"extreme": 1, "ma": 1, "volume": 1}


def _zone_strength(zone: Mapping[str, Any]) -> int:
    """强度 1~5：综合来源类型、历史反应次数、价格反应幅度、时间跨度与均线重合。

    均线重合只作为加分项之一：只有均线作为来源时最高只能得到 2 分，
    避免把 MA20 直接当成支撑位。
    """

    strength = 1
    kinds = set(zone.get("kinds", []))
    strength += sum(_KIND_WEIGHTS[kind] for kind in kinds if kind in _KIND_WEIGHTS)
    if "extreme" in kinds:
        touches = int(zone.get("touches", 1))
        if touches >= 2:
            strength += 1
        if touches >= 3:
            strength += 1
    if zone.get("prominent"):
        strength += 1
    if zone.get("long_term"):
        strength += 1
    return min(5, max(1, strength))


def _finalize_zone(zone: Mapping[str, Any]) -> Dict[str, Any]:
    zone_type = str(zone.get("zone_type", "RESISTANCE"))
    low = float(zone["zone_low"])
    high = float(zone["zone_high"])
    if zone_type == "RESISTANCE":
        confirmation = {
            "close_above": high,
            "volume_ratio_min": 1.2,
            "additional_conditions": [
                "收盘位于 MA5/MA10 上方",
                "周线守住 5 周线或 10 周线",
            ],
        }
    else:
        confirmation = {
            "close_below": low,
            "additional_conditions": ["跌破后需要收盘确认，不只用盘中低点判断"],
        }
    reasons = list(dict.fromkeys(zone.get("reasons", [])))
    touches = zone.get("touches", 1)
    if touches >= 2:
        if zone_type == "RESISTANCE":
            reasons.append("多次冲高回落")
        else:
            reasons.append("多次探底回升")
    return {
        "zone_type": zone_type,
        "zone_low": low,
        "zone_high": high,
        "strength": _zone_strength(zone),
        "reasons": list(dict.fromkeys(reasons)),
        "breakout_confirmation": confirmation,
    }


def _fallback_zone(
    center: float,
    zone_type: str,
    reason: str,
) -> Dict[str, Any]:
    return _new_zone(center, zone_type, reason)


def _volume_dense_center(
    bars: Sequence[Mapping[str, Any]],
) -> Optional[Tuple[float, float, float]]:
    rows = [
        (_number(bar.get("close")), _number(bar.get("volume_hands")))
        for bar in bars
    ]
    rows = [(close, volume) for close, volume in rows if close and volume and volume > 0]
    if len(rows) < 20:
        return None
    median_close = sorted(close for close, _ in rows)[len(rows) // 2]
    bin_width = median_close * 0.01
    buckets: Dict[int, Tuple[float, float]] = {}
    for close, volume in rows:
        index = int(close / bin_width)
        total, quantity = buckets.get(index, (0.0, 0.0))
        buckets[index] = (total + volume, close)
    average_volume = sum(value[0] for value in buckets.values()) / len(buckets)
    selected = max(buckets.items(), key=lambda item: item[1][0])
    if selected[1][0] < average_volume * 1.8:
        return None
    selected_rows = [
        (close, volume)
        for close, volume in rows
        if int(close / bin_width) == selected[0]
    ]
    selected_volume = sum(volume for _, volume in selected_rows)
    center = sum(close * volume for close, volume in selected_rows) / selected_volume
    return center, bin_width, selected_volume


def synthesize_zone(center: float, zone_type: str, reason: str) -> Dict[str, Any]:
    """Build a finalized zone from a single price level (legacy fallback)."""

    return _finalize_zone(_new_zone(center, zone_type, reason))


def _pad_zones(
    zones: Sequence[Dict[str, Any]],
    close: float,
    current_extreme: Optional[float],
    zone_type: str,
) -> List[Dict[str, Any]]:
    """补足两个区域：先补当日高/低点，再补相对锚定价位的规则位。

    当日高/低点只作为补充来源，避免短期噪音挤占带多次反应或均线重合的结构性区域。
    """

    padded = list(zones)
    if current_extreme is not None and len(padded) < 2:
        label = "当日高点" if zone_type == "RESISTANCE" else "当日低点"
        # 与已有区域重叠时先合并，避免出现两个几乎相同的区域。
        padded = _merge_zones(
            padded + [_new_zone(current_extreme, zone_type, label, kind="intraday")]
        )
    if len(padded) >= 2:
        return padded

    if zone_type == "RESISTANCE":
        anchor = max([close] + [float(zone["zone_high"]) for zone in padded])
        ratios = (1.02, 1.04)
        labels = ("收盘价上方 2% 规则位", "收盘价上方 4% 规则位")
    else:
        anchor = min([close] + [float(zone["zone_low"]) for zone in padded])
        ratios = (0.98, 0.95)
        labels = ("收盘价下方 2% 规则位", "收盘价下方 5% 规则位")
    for ratio, label in zip(ratios, labels):
        if len(padded) >= 2:
            break
        padded.append(_fallback_zone(anchor * ratio, zone_type, label))
    return padded


def _clip_zone(
    zone: Mapping[str, Any],
    close: float,
    zone_type: str,
) -> Optional[Dict[str, Any]]:
    """把区域限制在当前收盘价的同一侧，避免支撑区与压力区重叠。

    支撑区上沿不超过收盘价，压力区下沿不低于收盘价；裁剪后区间退化
    （完全落在另一侧）时返回 None，由补位逻辑补足两个区域。
    """

    low = float(zone["zone_low"])
    high = float(zone["zone_high"])
    if zone_type == "SUPPORT":
        high = min(high, close)
    else:
        low = max(low, close)
    low = _zone_price(low)
    high = _zone_price(high)
    if high <= low:
        return None
    clipped = dict(zone)
    clipped["zone_low"] = low
    clipped["zone_high"] = high
    return clipped


# 同侧相邻区域之间保留的间隔，避免「支撑区2 上沿」压到「支撑区1 下沿」。
_SIDE_GAP = 0.999


def _separate_side(
    zones: Sequence[Mapping[str, Any]],
    zone_type: str,
) -> List[Dict[str, Any]]:
    """同一侧由近到远排列，保证相邻区域不重叠。

    传入顺序必须按离收盘价的远近排列：支撑区由高到低、压力区由低到高。
    近端区域保持不变，远端区域收窄到近端内侧，避免出现两档互相覆盖。
    """

    separated: List[Dict[str, Any]] = []
    for zone in zones:
        current = dict(zone)
        if separated:
            previous = separated[-1]
            if zone_type == "SUPPORT":
                ceiling = previous["zone_low"]
                if current["zone_high"] >= ceiling:
                    current["zone_high"] = _zone_price(ceiling * _SIDE_GAP)
            else:
                floor = previous["zone_high"]
                if current["zone_low"] <= floor:
                    current["zone_low"] = _zone_price(floor / _SIDE_GAP)
            if current["zone_high"] <= current["zone_low"]:
                continue
        separated.append(current)
    return separated


def build_price_zones(
    history: Sequence[Mapping[str, Any]],
    close: Optional[float],
    moving_averages: Optional[Mapping[str, Optional[float]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Build deterministic support/resistance zones and merge nearby sources."""

    if close is None or close <= 0:
        return {"support": [], "resistance": []}
    valid_bars = [
        bar
        for bar in history
        if _number(bar.get("high")) is not None
        and _number(bar.get("low")) is not None
        and _number(bar.get("high")) > _number(bar.get("low")) > 0
    ]
    valid_bars.sort(key=lambda bar: str(bar.get("date", bar.get("week_ending", ""))))
    supports: List[Dict[str, Any]] = []
    resistances: List[Dict[str, Any]] = []

    def add_source(level: float, reason: str, **kwargs: Any) -> None:
        """按来源价位相对收盘价的位置决定归属。

        价位在收盘价上方记为压力，下方记为支撑：前高被突破后转为支撑、
        前低被跌破后转为压力，避免出现「支撑区叠在压力区上方」。
        """

        zone_type = "RESISTANCE" if level >= close else "SUPPORT"
        zone = _new_zone(level, zone_type, reason, **kwargs)
        (resistances if zone_type == "RESISTANCE" else supports).append(zone)

    current = valid_bars[-1] if valid_bars else {}
    current_low = _number(current.get("low"))
    current_high = _number(current.get("high"))

    pivots = valid_bars[-121:-1]
    for index in range(2, len(pivots) - 2):
        bar = pivots[index]
        high = _number(bar.get("high"))
        low = _number(bar.get("low"))
        if high is None or low is None:
            continue
        left_highs = [_number(item.get("high")) for item in pivots[index - 2:index]]
        right_highs = [_number(item.get("high")) for item in pivots[index + 1:index + 3]]
        left_lows = [_number(item.get("low")) for item in pivots[index - 2:index]]
        right_lows = [_number(item.get("low")) for item in pivots[index + 1:index + 3]]
        if all(value is not None and high >= value for value in left_highs + right_highs):
            neighbors = [
                value for value in left_highs + right_highs if value is not None
            ]
            add_source(
                high,
                "前期高点",
                prominent=bool(neighbors) and (high - max(neighbors)) / high >= 0.005,
            )
        if all(value is not None and low <= value for value in left_lows + right_lows):
            neighbors = [value for value in left_lows + right_lows if value is not None]
            add_source(
                low,
                "前期低点",
                prominent=bool(neighbors)
                and (min(neighbors) - low) / min(neighbors) >= 0.005,
            )

    for period in (20, 60, 120):
        window = valid_bars[:-1][-period:]
        if not window:
            continue
        highs = [_number(bar.get("high")) for bar in window]
        lows = [_number(bar.get("low")) for bar in window]
        high = max((value for value in highs if value is not None), default=None)
        low = min(
            (value for value in lows if value is not None),
            default=None,
        )
        long_term = period >= 60
        if high:
            add_source(high, f"{period}日高点", long_term=long_term)
        if low:
            add_source(low, f"{period}日低点", long_term=long_term)

    moving_averages = moving_averages or {}
    for name, period in (
        ("ma5", 5),
        ("ma10", 10),
        ("ma20", 20),
        ("ma60", 60),
    ):
        value = moving_averages.get(name)
        if value is None or value <= 0:
            continue
        add_source(value, f"MA{period}附近", kind="ma")

    dense = _volume_dense_center(valid_bars[:-1])
    if dense:
        center, width, _ = dense
        add_source(
            center,
            "前期成交密集区域",
            kind="volume",
            volume_dense=True,
            half_width_pct=max(0.004, width / center / 2),
        )

    support_zones = [
        zone
        for zone in (
            _clip_zone(item, close, "SUPPORT") for item in _merge_zones(supports)
        )
        if zone is not None
    ]
    resistance_zones = [
        zone
        for zone in (
            _clip_zone(item, close, "RESISTANCE") for item in _merge_zones(resistances)
        )
        if zone is not None
    ]
    support_zones.sort(
        key=lambda zone: (zone["zone_low"] + zone["zone_high"]) / 2,
        reverse=True,
    )
    resistance_zones.sort(
        key=lambda zone: (zone["zone_low"] + zone["zone_high"]) / 2
    )

    support_zones = _pad_zones(support_zones, close, current_low, "SUPPORT")
    resistance_zones = _pad_zones(resistance_zones, close, current_high, "RESISTANCE")
    # 补位可能引入贴近收盘价的当日高/低点，再裁剪一次保证两侧不重叠。
    support_zones = [
        zone
        for zone in (_clip_zone(item, close, "SUPPORT") for item in support_zones)
        if zone is not None
    ]
    resistance_zones = [
        zone
        for zone in (_clip_zone(item, close, "RESISTANCE") for item in resistance_zones)
        if zone is not None
    ]
    support_zones.sort(
        key=lambda zone: (zone["zone_low"] + zone["zone_high"]) / 2,
        reverse=True,
    )
    resistance_zones.sort(
        key=lambda zone: (zone["zone_low"] + zone["zone_high"]) / 2
    )
    support_zones = _separate_side(support_zones, "SUPPORT")
    resistance_zones = _separate_side(resistance_zones, "RESISTANCE")

    return {
        "support": [_finalize_zone(zone) for zone in support_zones[:2]],
        "resistance": [_finalize_zone(zone) for zone in resistance_zones[:2]],
    }
