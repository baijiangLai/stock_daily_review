"""Deterministic, no-LLM portfolio review backed by public market APIs.

The generator intentionally keeps data collection and report rendering separate:
``collect_stock_data`` talks to public Eastmoney/Sohu endpoints, while the
indicator and Markdown functions are pure and easy to unit-test.  It is not a
replacement for human judgement; it is a stable fallback when model quotas are
unavailable.
"""

from __future__ import annotations

import json
import gzip
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .market_structure import classify_pattern, classify_trend


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")
SOHU_HISTORY_API = "https://q.stock.sohu.com/hisHq"
EASTMONEY_QUOTE_API = "https://push2delay.eastmoney.com/api/qt/stock/get"
EASTMONEY_FINANCIAL_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_ANNOUNCEMENT_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EASTMONEY_SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"
EASTMONEY_F10_API = "https://emweb.securities.eastmoney.com/PC_HSF10"


class LocalReviewError(RuntimeError):
    """Raised when public data cannot be collected or parsed."""


@dataclass(frozen=True)
class DailyBar:
    date: str
    open: float
    close: float
    change: float
    change_pct: float
    low: float
    high: float
    volume_hands: float
    amount_wan: float
    turnover_pct: Optional[float]


@dataclass
class LocalStockData:
    stock: Dict[str, Any]
    holding: Dict[str, Any]
    bar: DailyBar
    history: List[DailyBar]
    indicators: Dict[str, Optional[float]]
    boards: List[Dict[str, Any]]
    valuation: Dict[str, Optional[float]]
    financial: Optional[Dict[str, Any]]
    announcements: List[Dict[str, str]]
    news: List[Dict[str, str]]
    f10: Optional["F10Data"]
    source_note: str


@dataclass
class MarginBalance:
    date: str
    balance_yuan: Optional[float]
    available: bool


@dataclass
class F10Data:
    holder_date: str
    holder_count: Optional[int]
    margin: Optional[MarginBalance]
    errors: List[str]


def _request_json(url: str, timeout: float) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip",
                "Referer": "https://quote.eastmoney.com/",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=max(timeout, 5.0)
            ) as response:
                raw = response.read()
                raw = _decode_response_payload(raw)
                for encoding in (
                    response.headers.get_content_charset(),
                    "utf-8",
                    "gb18030",
                ):
                    if not encoding:
                        continue
                    try:
                        text = raw.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                else:  # pragma: no cover - unusual server response
                    text = raw.decode("utf-8", errors="replace")
                return json.loads(text)
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise LocalReviewError(f"公开行情接口请求失败：{url}：{last_error}")


def _decode_response_payload(raw: bytes) -> bytes:
    if raw.startswith(b"\x1f\x8b"):
        return gzip.decompress(raw)
    return raw


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "" or value == "-":
        return default
    try:
        result = float(str(value).replace(",", "").replace("%", ""))
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _fmt(value: Optional[float], digits: int = 2, suffix: str = "") -> str:
    return "待核实" if value is None else f"{value:.{digits}f}{suffix}"


def _fmt_wan(value: Optional[float]) -> str:
    if value is None:
        return "待核实"
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.2f}万亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.2f}亿"
    return f"{value:.2f}万"


def _fmt_yuan(value: Optional[float]) -> str:
    return "待核实" if value is None else _fmt_wan(value / 10000)


def _fmt_percent(value: Optional[float], digits: int = 2) -> str:
    return "待核实" if value is None else f"{value * 100:.{digits}f}%"


def _fmt_percentage_points(value: Optional[float], digits: int = 2) -> str:
    return "待核实" if value is None else f"{value * 100:.{digits}f}个百分点"


def _fmt_hands(value: Optional[float]) -> str:
    return "待核实" if value is None else f"{value / 10000:.2f}万手"


def parse_sohu_history(payload: Any, review_date: str) -> List[DailyBar]:
    """Parse Sohu's daily-history JSON into ascending ``DailyBar`` records."""

    if not isinstance(payload, list) or not payload:
        raise LocalReviewError("搜狐历史行情返回为空或格式异常")
    record = payload[0]
    if not isinstance(record, dict) or record.get("status") != 0:
        message = record.get("message", "status 非 0") if isinstance(record, dict) else "格式异常"
        raise LocalReviewError(f"搜狐历史行情返回异常：{message}")
    rows = record.get("hq")
    if not isinstance(rows, list) or not rows:
        raise LocalReviewError(f"搜狐未返回 {review_date} 附近的历史行情")

    parsed: List[DailyBar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 9:
            continue
        bar = DailyBar(
            date=str(row[0]),
            open=_number(row[1], 0.0) or 0.0,
            close=_number(row[2], 0.0) or 0.0,
            change=_number(row[3], 0.0) or 0.0,
            change_pct=_number(str(row[4]).replace("%", ""), 0.0) or 0.0,
            low=_number(row[5], 0.0) or 0.0,
            high=_number(row[6], 0.0) or 0.0,
            volume_hands=_number(row[7], 0.0) or 0.0,
            amount_wan=_number(row[8], 0.0) or 0.0,
            turnover_pct=_number(row[9]) if len(row) > 9 else None,
        )
        parsed.append(bar)
    parsed.sort(key=lambda item: item.date)
    if not parsed:
        raise LocalReviewError("搜狐历史行情没有可解析记录")
    return parsed


def _prior_friday(review_date: str) -> date:
    parsed = date.fromisoformat(review_date)
    if parsed.weekday() < 5:
        return parsed
    return parsed - timedelta(days=parsed.weekday() - 4)


def fetch_stock_history(stock: Mapping[str, Any], review_date: str, timeout: float) -> List[DailyBar]:
    code = str(stock.get("code", "")).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise LocalReviewError(f"股票代码格式无效：{code}")
    end = date.fromisoformat(review_date)
    start = end - timedelta(days=300)
    query = urllib.parse.urlencode(
        {
            "code": f"cn_{code}",
            "start": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "stat": 1,
            "order": "D",
            "period": "d",
        }
    )
    payload = _request_json(f"{SOHU_HISTORY_API}?{query}", timeout)
    history = parse_sohu_history(payload, review_date)
    if history[-1].date != review_date:
        if date.fromisoformat(review_date).weekday() >= 5:
            latest_trading_date = _prior_friday(review_date)
            if history[-1].date != latest_trading_date.isoformat():
                snapshot = fetch_stock_snapshot_bar(
                    stock, latest_trading_date.isoformat(), timeout
                )
                if snapshot is not None:
                    history.append(snapshot)
            return history
        snapshot = fetch_stock_snapshot_bar(stock, review_date, timeout)
        if snapshot is None:
            raise LocalReviewError(
                f"搜狐未返回 {stock.get('name', code)} 在 {review_date} 的交易数据，"
                "且东方财富没有匹配复盘日的实时快照"
            )
        if history[-1].date >= review_date:
            raise LocalReviewError(
                f"{stock.get('name', code)} 的历史行情日期序列异常："
                f"{history[-1].date} >= {review_date}"
            )
        history.append(snapshot)
    return history


def fetch_stock_snapshot_bar(
    stock: Mapping[str, Any], review_date: str, timeout: float
) -> Optional[DailyBar]:
    """Fetch an exact-date Eastmoney snapshot for Sohu's lagging daily row."""

    quote_id = str(stock.get("quote_id", "")).strip()
    code = str(stock.get("code", "")).strip()
    secid = quote_id or (
        f"1.{code}" if code.startswith(("6", "9")) else f"0.{code}"
    )
    query = urllib.parse.urlencode(
        {
            "secid": secid,
            "fields": ",".join(
                [
                    "f43", "f44", "f45", "f46", "f47", "f48",
                    "f59", "f60", "f86", "f168", "f169", "f170",
                ]
            ),
        }
    )
    payload = _request_json(f"{EASTMONEY_QUOTE_API}?{query}", timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not _snapshot_is_for_date(data, review_date):
        return None
    decimals = int(data.get("f59", 2) or 2)
    close = _scaled(data.get("f43"), decimals)
    prev_close = _scaled(data.get("f60"), decimals)
    if close is None or prev_close is None:
        return None
    amount_yuan = _number(data.get("f48"))
    return DailyBar(
        date=review_date,
        open=_scaled(data.get("f46"), decimals) or close,
        close=close,
        change=_scaled(data.get("f169"), decimals) or close - prev_close,
        change_pct=_scaled(data.get("f170"), 2) or 0.0,
        high=_scaled(data.get("f44"), decimals) or close,
        low=_scaled(data.get("f45"), decimals) or close,
        volume_hands=_number(data.get("f47")) or 0.0,
        amount_wan=amount_yuan / 10000 if amount_yuan is not None else 0.0,
        turnover_pct=_scaled(data.get("f168"), 2),
    )


def moving_average(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def rsi(closes: Sequence[float], period: int) -> Optional[float]:
    if len(closes) <= period or period <= 0:
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


def calculate_indicators(history: Sequence[DailyBar]) -> Dict[str, Optional[float]]:
    closes = [bar.close for bar in history]
    volumes = [bar.volume_hands for bar in history]
    return {
        "ma5": moving_average(closes, 5),
        "ma10": moving_average(closes, 10),
        "ma20": moving_average(closes, 20),
        "ma60": moving_average(closes, 60),
        "volume_ma5": moving_average(volumes, 5),
        "volume_ma10": moving_average(volumes, 10),
        "rsi6": rsi(closes, 6),
        "rsi12": rsi(closes, 12),
        "rsi24": rsi(closes, 24),
    }


def _scaled(value: Any, exponent: int) -> Optional[float]:
    number = _number(value)
    return number / (10**exponent) if number is not None else None


def _snapshot_is_for_date(
    data: Mapping[str, Any],
    review_date: str,
    *,
    allow_weekend_prior: bool = False,
) -> bool:
    timestamp = _number(data.get("f86"))
    if timestamp is None:
        return False
    snapshot_date = datetime.fromtimestamp(
        timestamp, tz=SHANGHAI_TIMEZONE
    ).date().isoformat()
    if snapshot_date == review_date:
        return True
    if not allow_weekend_prior:
        return False
    parsed_review_date = date.fromisoformat(review_date)
    if parsed_review_date.weekday() < 5:
        return False
    prior_friday = _prior_friday(review_date)
    return snapshot_date == prior_friday.isoformat()


def fetch_valuation(
    stock: Mapping[str, Any], review_date: str, timeout: float
) -> Dict[str, Optional[float]]:
    code = str(stock.get("code", ""))
    market = "1" if code.startswith(("6", "9")) else "0"
    fields = ",".join(
        [
            "f43", "f59", "f86", "f116", "f117", "f162", "f167",
            "f168", "f169", "f170", "f50",
        ]
    )
    query = urllib.parse.urlencode({"secid": f"{market}.{code}", "fields": fields})
    payload = _request_json(f"{EASTMONEY_QUOTE_API}?{query}", timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {key: None for key in (
            "close", "market_cap", "float_market_cap", "pe", "pb",
            "turnover_pct", "change", "change_pct", "volume_ratio",
        )}
    if not _snapshot_is_for_date(data, review_date, allow_weekend_prior=True):
        return {
            key: None
            for key in (
                "close", "market_cap", "float_market_cap", "pe", "pb",
                "turnover_pct", "change", "change_pct", "volume_ratio",
            )
        }

    return {
        "close": _scaled(data.get("f43"), int(data.get("f59", 2) or 2)),
        "market_cap": _number(data.get("f116")),
        "float_market_cap": _number(data.get("f117")),
        "pe": _scaled(data.get("f162"), 2),
        "pb": _scaled(data.get("f167"), 2),
        "turnover_pct": _scaled(data.get("f168"), 2),
        "change": _scaled(data.get("f169"), 2),
        "change_pct": _scaled(data.get("f170"), 2),
        "volume_ratio": _scaled(data.get("f50"), 2),
    }


def fetch_board_quotes(
    board_metadata: Mapping[str, Any], review_date: str, timeout: float
) -> List[Dict[str, Any]]:
    boards = board_metadata.get("boards", {}) if isinstance(board_metadata, dict) else {}
    result: List[Dict[str, Any]] = []
    for label, raw_info in boards.items():
        if not isinstance(raw_info, dict):
            continue
        code = str(raw_info.get("code", ""))
        query = urllib.parse.urlencode(
            {
                "secid": f"90.{code}",
                "fields": "f43,f47,f48,f50,f57,f58,f59,f60,f86,f168,f169,f170",
            }
        )
        try:
            payload = _request_json(f"{EASTMONEY_QUOTE_API}?{query}", timeout)
            data = payload.get("data") if isinstance(payload, dict) else {}
            if not isinstance(data, dict):
                raise LocalReviewError(f"板块 {code} 返回格式异常")
            if not _snapshot_is_for_date(
                data, review_date, allow_weekend_prior=True
            ):
                raise LocalReviewError(
                    f"板块实时快照日期与复盘日 {review_date} 不一致"
                )
            decimals = int(data.get("f59", 2) or 2)
            result.append(
                {
                    "label": label,
                    "name": str(data.get("f58") or raw_info.get("name", "")),
                    "code": code,
                    "close": _scaled(data.get("f43"), decimals),
                    "prev_close": _scaled(data.get("f60"), decimals),
                    "change": _scaled(data.get("f169"), decimals),
                    "change_pct": _scaled(data.get("f170"), 2),
                    "volume_hands": _number(data.get("f47")),
                    "amount_yuan": _number(data.get("f48")),
                    "turnover_pct": _scaled(data.get("f168"), 2),
                    "volume_ratio": _scaled(data.get("f50"), 2),
                }
            )
        except Exception as exc:
            result.append(
                {
                    "label": label,
                    "name": str(raw_info.get("name", "")),
                    "code": code,
                    "error": str(exc),
                }
            )
    return result


def fetch_market_indices(review_date: str, timeout: float) -> List[Dict[str, Any]]:
    definitions = (
        ("上证指数", "zs_000001", "1.000001"),
        ("深证成指", "zs_399001", "0.399001"),
        ("创业板指", "zs_399006", "0.399006"),
    )
    result: List[Dict[str, Any]] = []
    end = date.fromisoformat(review_date)
    start = end - timedelta(days=30)
    for name, index_code, secid in definitions:
        query = urllib.parse.urlencode(
            {
                "code": index_code,
                "start": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "stat": 1,
                "order": "D",
                "period": "d",
            }
        )
        try:
            payload = _request_json(f"{SOHU_HISTORY_API}?{query}", timeout)
            history = parse_sohu_history(payload, review_date)
            if history[-1].date != review_date:
                if date.fromisoformat(review_date).weekday() >= 5:
                    latest_trading_date = _prior_friday(review_date)
                    index_bar = {
                        "name": name,
                        "close": history[-1].close,
                        "change_pct": history[-1].change_pct / 100,
                    }
                    if history[-1].date != latest_trading_date.isoformat():
                        snapshot = fetch_market_index_snapshot(
                            name,
                            secid,
                            latest_trading_date.isoformat(),
                            timeout,
                        )
                        if snapshot is not None:
                            index_bar = snapshot
                    result.append(index_bar)
                    continue
                snapshot = fetch_market_index_snapshot(
                    name, secid, review_date, timeout
                )
                if snapshot is None:
                    continue
                result.append(snapshot)
            else:
                result.append(
                    {
                        "name": name,
                        "close": history[-1].close,
                        "change_pct": history[-1].change_pct / 100,
                    }
                )
        except Exception:
            continue
    return result


def fetch_market_index_snapshot(
    name: str, secid: str, review_date: str, timeout: float
) -> Optional[Dict[str, Any]]:
    """Fetch an exact-date index snapshot when Sohu's daily row is delayed."""

    query = urllib.parse.urlencode(
        {
            "secid": secid,
            "fields": "f43,f57,f58,f59,f60,f86,f169,f170",
        }
    )
    payload = _request_json(f"{EASTMONEY_QUOTE_API}?{query}", timeout)
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict) or not _snapshot_is_for_date(
        data, review_date, allow_weekend_prior=True
    ):
        return None
    decimals = int(data.get("f59", 2) or 2)
    close = _scaled(data.get("f43"), decimals)
    change_pct = _scaled(data.get("f170"), 2)
    if close is None or change_pct is None:
        return None
    return {
        "name": name,
        "close": close,
        "change_pct": change_pct / 100,
    }


def _f10_module(stock: Mapping[str, Any], module: str, timeout: float) -> Dict[str, Any]:
    code = str(stock.get("code", "")).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise LocalReviewError(f"F10 股票代码格式无效：{code}")
    market = "SH" if code.startswith(("6", "9")) else "SZ"
    query = urllib.parse.urlencode({"code": f"{market}{code}"})
    payload = _request_json(
        f"{EASTMONEY_F10_API}/{module}/PageAjax?{query}", timeout
    )
    return payload if isinstance(payload, dict) else {}


def _rows_as_of(
    rows: Sequence[Mapping[str, Any]], date_field: str, review_date: str
) -> List[Dict[str, Any]]:
    valid = [
        dict(row)
        for row in rows
        if str(row.get(date_field, ""))[:10] <= review_date
    ]
    if not valid:
        return []
    latest_date = max(str(row.get(date_field, ""))[:10] for row in valid)
    return [row for row in valid if str(row.get(date_field, ""))[:10] == latest_date]


def _mapping_rows(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


def fetch_f10_profile(
    stock: Mapping[str, Any], review_date: str, timeout: float
) -> F10Data:
    """Collect the two F10 facts used by reviews: holders and margin balance."""

    errors: List[str] = []

    def safe_holder_module() -> Dict[str, Any]:
        try:
            return _f10_module(stock, "ShareholderResearch", timeout)
        except Exception as exc:
            errors.append(f"ShareholderResearch: {exc}")
            return {}

    holder_payload = safe_holder_module()
    holder_rows = _rows_as_of(
        _mapping_rows(holder_payload.get("gdrs")), "END_DATE", review_date
    )
    holder = holder_rows[0] if holder_rows else {}
    holder_count = _number(holder.get("HOLDER_TOTAL_NUM"))

    try:
        margin = fetch_margin_trading_balance(stock, review_date, timeout)
    except Exception as exc:
        errors.append(f"MarginTrading: {exc}")
        margin = None

    return F10Data(
        holder_date=str(holder.get("END_DATE", ""))[:10],
        holder_count=int(holder_count) if holder_count is not None else None,
        margin=margin,
        errors=errors,
    )


def fetch_margin_trading_balance(
    stock: Mapping[str, Any], review_date: str, timeout: float
) -> MarginBalance:
    """Fetch the latest margin-trading balance on or before ``review_date``."""

    code = str(stock.get("code", "")).strip()
    if not re.fullmatch(r"\d{6}", code):
        raise LocalReviewError(f"融资融券股票代码格式无效：{code}")
    query = urllib.parse.urlencode(
        {
            "reportName": "RPTA_WEB_RZRQ_GGMX",
            "columns": "DATE,RZRQYE",
            "filter": f'(SCODE="{code}")(DATE<=\'{review_date} 00:00:00\')',
            "pageNumber": 1,
            "pageSize": 1,
            "sortColumns": "DATE",
            "sortTypes": -1,
        }
    )
    payload = _request_json(f"{EASTMONEY_FINANCIAL_API}?{query}", timeout)
    if not isinstance(payload, dict):
        raise LocalReviewError(f"融资融券接口返回格式异常：{code}")

    result = payload.get("result") if isinstance(payload, dict) else None
    no_data = payload.get("code") == 9201 or payload.get("message") == "返回数据为空"
    if not isinstance(result, dict):
        if no_data:
            return MarginBalance(date="", balance_yuan=None, available=False)
        raise LocalReviewError(f"融资融券接口返回格式异常：{code}")

    rows = result.get("data")
    if rows is None:
        if no_data or payload.get("success", True) is not False:
            return MarginBalance(date="", balance_yuan=None, available=False)
        raise LocalReviewError(f"融资融券接口返回格式异常：{code}")

    if not isinstance(rows, list):
        raise LocalReviewError(f"融资融券接口返回格式异常：{code}")

    if not rows:
        return MarginBalance(date="", balance_yuan=None, available=False)

    row = (
        rows[0]
        if isinstance(rows, list) and rows and isinstance(rows[0], dict)
        else None
    )
    if row is None:
        raise LocalReviewError(f"融资融券余额未返回 {code} 数据")
    balance = _number(row.get("RZRQYE"))
    if balance is None:
        raise LocalReviewError(f"融资融券余额格式异常：{code}")
    margin_date = str(row.get("DATE", ""))[:10]
    try:
        parsed_margin_date = date.fromisoformat(margin_date)
        parsed_review_date = date.fromisoformat(review_date)
    except ValueError as exc:
        raise LocalReviewError(f"融资融券日期格式异常：{code}") from exc
    if parsed_margin_date > parsed_review_date:
        raise LocalReviewError(f"融资融券日期晚于复盘日：{code}")
    return MarginBalance(
        date=margin_date,
        balance_yuan=balance,
        available=True,
    )


def fetch_financial_report(
    stock: Mapping[str, Any], review_date: str, timeout: float
) -> Optional[Dict[str, Any]]:
    code = str(stock.get("code", ""))
    query = urllib.parse.urlencode(
        {
            "reportName": "RPT_LICO_FN_CPD",
            "columns": "ALL",
            "filter": (
                f'(SECURITY_CODE="{code}")'
                f'(REPORTDATE<=\'{review_date}\')'
                f'(NOTICE_DATE<=\'{review_date}\')'
            ),
            "pageNumber": 1,
            "pageSize": 1,
            "sortColumns": "REPORTDATE",
            "sortTypes": -1,
        }
    )
    try:
        payload = _request_json(f"{EASTMONEY_FINANCIAL_API}?{query}", timeout)
        rows = (
            payload.get("result", {}).get("data", [])
            if isinstance(payload, dict)
            else []
        )
        return rows[0] if rows else None
    except Exception:
        return None


def fetch_announcements(
    stock: Mapping[str, Any], review_date: str, timeout: float, limit: int = 3
) -> List[Dict[str, str]]:
    code = str(stock.get("code", ""))
    query = urllib.parse.urlencode(
        {
            "sr": -1,
            "page_size": 20,
            "page_index": 1,
            "ann_type": "A",
            "client_source": "web",
            "stock_list": code,
        }
    )
    try:
        payload = _request_json(f"{EASTMONEY_ANNOUNCEMENT_API}?{query}", timeout)
        rows = payload.get("data", {}).get("list", [])
        announcements = [
            {
                "date": str(item.get("notice_date", ""))[:10],
                "title": str(item.get("title", "")),
                "code": str(item.get("art_code", "")),
            }
            for item in rows
            if isinstance(item, dict)
        ]
        return [
            item for item in announcements if item["date"] <= review_date
        ][:limit]
    except Exception:
        return []


def fetch_news(
    stock: Mapping[str, Any], review_date: str, timeout: float, limit: int = 3
) -> List[Dict[str, str]]:
    name = str(stock.get("name", ""))
    code = str(stock.get("code", ""))
    param = {
        "uid": "",
        "keyword": f"{name} {code}".strip(),
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "time",
                "pageIndex": 1,
                "pageSize": 20,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    query = urllib.parse.urlencode(
        {"cb": "localReview", "param": json.dumps(param, ensure_ascii=False, separators=(",", ":"))}
    )
    try:
        # The endpoint always returns JSONP. Parse just the JSON body.
        request = urllib.request.Request(
            f"{EASTMONEY_SEARCH_API}?{query}",
            headers={"User-Agent": USER_AGENT, "Referer": "https://so.eastmoney.com/"},
        )
        with urllib.request.urlopen(request, timeout=max(timeout, 5.0)) as response:
            text = response.read().decode("utf-8", errors="replace")
        match = re.fullmatch(r"localReview\((.*)\)\s*;?", text, flags=re.S)
        if not match:
            return []
        payload = json.loads(match.group(1))
        rows = payload.get("result", {}).get("cmsArticleWebOld", [])
        articles = [
            {
                "date": str(item.get("date", ""))[:16],
                "title": str(item.get("title", "")),
                "media": str(item.get("mediaName", "")),
                "url": str(item.get("url", "")),
                "summary": str(item.get("content", "")),
            }
            for item in rows
            if isinstance(item, dict)
        ]
        return [item for item in articles if item["date"][:10] <= review_date][:limit]
    except Exception:
        return []


def collect_stock_data(
    holding: Mapping[str, str],
    stock_dir: Path,
    config: Mapping[str, Any],
) -> LocalStockData:
    metadata_path = stock_dir / "metadata.json"
    boards_path = stock_dir / "boards_metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        board_metadata = json.loads(boards_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalReviewError(f"读取截图元数据失败：{exc}") from exc

    stock = metadata.get("resolved_stock")
    if not isinstance(stock, dict):
        raise LocalReviewError(f"{metadata_path} 缺少 resolved_stock")
    review_date = str(config.get("review_date", ""))
    timeout = float(config.get("timeout", 180.0))
    history = fetch_stock_history(stock, review_date, timeout)
    indicators = calculate_indicators(history)
    no_news = bool(config.get("no_web_search"))
    return LocalStockData(
        stock=stock,
        holding=dict(holding),
        bar=history[-1],
        history=history,
        indicators=indicators,
        boards=fetch_board_quotes(board_metadata, review_date, timeout),
        valuation=fetch_valuation(stock, review_date, timeout),
        financial=fetch_financial_report(stock, review_date, timeout),
        announcements=[] if no_news else fetch_announcements(stock, review_date, timeout),
        news=[] if no_news else fetch_news(stock, review_date, timeout),
        f10=fetch_f10_profile(stock, review_date, timeout),
        source_note=(
            "行情与均线：搜狐公开日线；估值/板块：东方财富当日公开快照；"
            "财报/公告/新闻/F10：东方财富公开接口。"
        ),
    )


def _nearest_levels(bar: DailyBar, indicators: Mapping[str, Optional[float]]) -> Tuple[List[float], List[float]]:
    close = bar.close
    levels = {
        "今日低点": bar.low,
        "今日开盘": bar.open,
        "MA5": indicators.get("ma5"),
        "MA10": indicators.get("ma10"),
        "MA20": indicators.get("ma20"),
        "MA60": indicators.get("ma60"),
        "今日高点": bar.high,
    }
    below = sorted({value for value in levels.values() if value is not None and value < close}, reverse=True)
    above = sorted({value for value in levels.values() if value is not None and value > close})
    if not below:
        below = [bar.low]
    if not above:
        above = [bar.high]
    if len(below) < 2:
        below.append(round(below[0] * 0.98, 2))
    if len(above) < 2:
        above.append(round(max(above[0], close) * 1.02, 2))
    return below[:2], above[:2]


def _volume_ratio(bar: DailyBar, indicators: Mapping[str, Optional[float]]) -> Optional[float]:
    average = indicators.get("volume_ma5")
    return bar.volume_hands / average if average else None


def _market_state(bar: DailyBar, indicators: Mapping[str, Optional[float]]) -> str:
    ratio = _volume_ratio(bar, indicators)
    if bar.change_pct <= -1 and ratio and ratio >= 1.5:
        return "放量下跌，抛压明显"
    if bar.change_pct <= -1:
        return "缩量或温和放量下跌，短线仍偏弱"
    if bar.change_pct >= 1 and ratio and ratio >= 1.5:
        return "显著放量上涨，短线资金参与度高"
    if bar.change_pct >= 1:
        return "上涨但量能扩展一般，需确认持续性"
    return "窄幅震荡，方向待选择"


def _render_f10_section(data: LocalStockData) -> List[str]:
    f10 = data.f10
    if f10 is None:
        return ["### F10 关键数据", "", "- F10 数据未获取。", ""]

    if f10.margin is None:
        margin_value, margin_date = "待核实", "待核实"
    elif f10.margin.available:
        margin_value = _fmt_yuan(f10.margin.balance_yuan)
        margin_date = f10.margin.date or "待核实"
    else:
        margin_value, margin_date = "无数据", "—"

    lines = [
        "### F10 关键数据",
        "",
        "| 指标 | 数值 | 数据日期 |",
        "| --- | ---: | --- |",
        f"| 最新股东人数 | {_fmt(f10.holder_count, 0, '户')} | {f10.holder_date or '待核实'} |",
        f"| 融资融券余额 | {margin_value} | {margin_date} |",
    ]
    if f10.errors:
        lines.extend(["", "- F10 部分接口未获取成功：" + "；".join(f10.errors) + "。"])
    lines.append("")
    return lines


def _execution_grade(
    operation: Mapping[str, Any],
    bar: Mapping[str, Any],
    average_price: Optional[float],
) -> str:
    price = _number(operation.get("price"))
    high = _number(bar.get("high"))
    low = _number(bar.get("low"))
    close = _number(bar.get("close"))
    if price is None or high is None or low is None or close is None:
        return "待核实"
    if price > high or price < low:
        return "数据异常"

    position = (price - low) / (high - low) if high > low else 0.5
    is_buy = operation.get("action") == "买入"
    if average_price is not None:
        if is_buy and price <= average_price * 0.99:
            return "优秀：低于当日均价约1%以上"
        if not is_buy and price >= average_price * 1.01:
            return "优秀：高于当日均价约1%以上"
    if is_buy:
        if position <= 0.35:
            return "良好：接近当日低位区"
        if position >= 0.80:
            return "偏差：明显追高"
    else:
        if position >= 0.65:
            return "良好：接近当日高位区"
        if position <= 0.20:
            return "偏差：明显低位卖出"
    return "合格：接近日内中性价格"


def _render_operation_section(data: LocalStockData) -> List[str]:
    operation = data.holding.get("operation")
    supports, resistances = _nearest_levels(data.bar, data.indicators)
    bar = asdict(data.bar)
    lines = ["### 今日实际操作复盘", ""]
    if not isinstance(operation, dict):
        return lines + [
            f"- 未记录实际操作；后续按周策略执行，支撑 **{supports[0]:.2f} / {supports[1]:.2f}**，"
            f"压力 **{resistances[0]:.2f} / {resistances[1]:.2f}**。",
            "",
        ]

    price = _number(operation.get("price"))
    quantity = _number(operation.get("quantity"))
    close = _number(bar.get("close"))
    volume_shares = (_number(bar.get("volume_hands")) or 0) * 100
    amount_yuan = (_number(bar.get("amount_wan")) or 0) * 10000
    average_price = amount_yuan / volume_shares if volume_shares else None
    action = str(operation.get("action", "待核实"))
    grade = _execution_grade(operation, bar, average_price)
    immediate_pnl = (
        quantity * (close - price)
        if price is not None and quantity is not None and close is not None
        else None
    )
    if action == "卖出" and immediate_pnl is not None:
        immediate_pnl = -immediate_pnl

    lines.extend(
        [
            f"- 操作：**{action} {quantity or 0:.0f} 股，成交价 {price or 0:.2f} 元**；",
            f"- 执行评价：**{grade}**；",
        ]
    )
    if average_price is not None:
        lines.append(f"- 当日估算均价：**{average_price:.2f} 元**（成交额 / 成交量）；")
    cost = _number(data.holding.get("cost"))
    if price is not None and cost is not None:
        if action == "买入":
            cost_effect = "低于综合成本，有助于摊低持仓成本" if price < cost else "高于综合成本，会抬升持仓成本"
            lines.append(
                f"- 成本影响：成交价相对综合成本 **{price - cost:+.2f} 元**，{cost_effect}。"
            )
        else:
            realized = quantity * (price - cost) if quantity is not None else None
            if realized is not None:
                lines.append(
                    f"- 成本对照：相对综合成本，本次卖出估算盈亏 **{realized:+.2f} 元**。"
                )
    if quantity is not None and quantity > (_number(data.holding.get("shares")) or 0):
        lines.append("- 数据警告：操作数量大于当前记录总持股，请核对总持仓和操作行。")
    if immediate_pnl is not None:
        direction = "买后浮盈" if immediate_pnl >= 0 else "买后浮亏"
        if action == "卖出":
            direction = "卖出后少亏/锁定优势" if immediate_pnl >= 0 else "卖出后股价上行"
        lines.append(
            f"- 相对收盘结果：{direction} **{immediate_pnl:+.2f} 元**"
            f"（按收盘价与成交价差估算）。"
        )

    low = _number(bar.get("low"))
    high = _number(bar.get("high"))
    if price is not None and (price > high or price < low):
        lines.append(
            "- 数据校验失败：成交价不在当日最高价与最低价之间，请核对操作记录。"
        )

    if action == "买入":
        if price is not None and price >= resistances[0]:
            lines.append(
                "- 纪律评价：买入位置接近压力区，短线安全边际不足；后续必须用收盘站稳压力来验证。"
            )
        elif price is not None and price <= supports[1]:
            lines.append(
                "- 纪律评价：买入位置接近支撑区，价格有安全边际，但需防止弱势股左侧接飞刀。"
            )
        else:
            lines.append("- 纪律评价：买入位置处于支撑与压力之间，属于中性执行区。")

        if close is not None and price is not None and close >= price:
            lines.extend(
                [
                    "",
                    "### 后续操作",
                    "",
                    f"- 当前买后处于正确状态。若收盘跌破 **{supports[0]:.2f}**，先减仓；"
                    f"若冲高至 **{resistances[0]:.2f}-{resistances[1]:.2f}** 且量能衰减，锁定部分利润。",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "### 后续操作",
                    "",
                    f"- 买后暂时被套，不能因为已买入而放宽风控。收盘跌破 **{supports[0]:.2f}** 必须降仓；"
                    f"只有重新站上 **{resistances[0]:.2f}** 才视为买回正确。",
                    "",
                ]
            )
    elif action == "卖出":
        if price is not None and price >= resistances[0]:
            lines.append(
                "- 纪律评价：卖出位置接近压力区，属于利用强势降低仓位，执行质量较好。"
            )
        elif price is not None and price <= supports[1]:
            lines.append(
                "- 纪律评价：卖出位置接近支撑区，属于恐慌性低位卖出，纪律质量偏差。"
            )
        else:
            lines.append("- 纪律评价：卖出位置处于中性区间。")

        if close is not None and price is not None and close <= price:
            lines.extend(
                [
                    "",
                    "### 后续操作",
                    "",
                    f"- 卖出后收盘不高于成交价，卖出目前有效。不要急于买回；"
                    f"重新站上 **{resistances[0]:.2f}** 后再评估右侧买回。",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "### 后续操作",
                    "",
                    f"- 卖出后股价继续上行，说明卖出偏早。禁止追高补回；"
                    f"等待回踩 **{supports[0]:.2f}-{supports[1]:.2f}** 且止跌后再评估。",
                    "",
                ]
            )
    else:
        lines.append("- 操作类型无效，请核对买入/卖出标记。")
    return lines


def _daily_pnl(holding: Mapping[str, Any], bar: DailyBar) -> Optional[float]:
    shares = _number(holding.get("shares"))
    if shares is None:
        return None
    operation = holding.get("operation")
    if not isinstance(operation, dict):
        return bar.change * shares

    quantity = _number(operation.get("quantity"))
    price = _number(operation.get("price"))
    if quantity is None or price is None or quantity <= 0:
        return bar.change * shares

    action = str(operation.get("action", ""))
    full_position_pnl = bar.change * shares
    if action == "买入":
        # 股票行记录的是操作后总持仓；先剔除全日涨跌中重复计入的新仓，
        # 再按成交价到收盘计算新仓当日盈亏。
        return full_position_pnl + quantity * (bar.close - price - bar.change)
    if action == "卖出":
        # 股票行记录的是卖出后剩余持仓；已卖出部分仍计入当日实际盈亏。
        return full_position_pnl + quantity * (price - bar.close + bar.change)
    return full_position_pnl


def render_stock_review(data: LocalStockData) -> str:
    bar = data.bar
    holding = data.holding
    indicators = data.indicators
    valuation = data.valuation
    cost = _number(holding.get("cost")) or 0.0
    shares = _number(holding.get("shares")) or 0.0
    market_value = bar.close * shares
    cost_value = cost * shares
    pnl = market_value - cost_value
    pnl_pct = pnl / cost_value if cost_value else None
    daily_pnl = _daily_pnl(holding, bar)
    supports, resistances = _nearest_levels(bar, indicators)
    ma_values = [(name, indicators.get(key)) for name, key in (
        ("MA5", "ma5"), ("MA10", "ma10"), ("MA20", "ma20"), ("MA60", "ma60")
    )]
    below_all = all(value is None or bar.close < value for _, value in ma_values)
    above_short = all(
        value is not None and bar.close >= value
        for value in (indicators.get("ma5"), indicators.get("ma10"), indicators.get("ma20"))
    )
    current_pattern = classify_pattern(
        asdict(data.history[-1]),
        asdict(data.history[-2]) if len(data.history) > 1 else None,
        indicators.get("volume_ma5"),
    )
    current_trend = classify_trend(
        bar.close,
        indicators.get("ma5"),
        indicators.get("ma10"),
        indicators.get("ma20"),
        indicators.get("rsi6"),
    )

    lines: List[str] = [
        f"## {data.stock.get('name', '')} {data.stock.get('code', '')}",
        "",
        "### 数据与结论",
        "",
        f"- 收盘 **{bar.close:.2f}**，涨跌 **{bar.change:+.2f} / {bar.change_pct:+.2f}%**；",
        f"- 持仓 {shares:.0f} 股，成本 {cost:.2f} 元，计划周期 {holding.get('plan', '待核实')}，"
        f"市值 **{market_value:.2f} 元**，"
        f"浮动盈亏 **{pnl:+.2f} 元 / {_fmt_percent(pnl_pct)}**；",
        f"- 今日持仓盈亏 **{daily_pnl:+.2f} 元**；",
        f"- 状态判断：**{_market_state(bar, indicators)}**。",
        "",
        *_render_operation_section(data),
        "### 当日交易数据",
        "",
        "| 项目 | 数值 |",
        "| --- | ---: |",
        f"| 昨收/今开 | {bar.close - bar.change:.2f} / {bar.open:.2f} |",
        f"| 最高/最低 | {bar.high:.2f} / {bar.low:.2f} |",
        f"| 收盘 | {bar.close:.2f} |",
        f"| 成交量 | {bar.volume_hands / 10000:.2f}万手 |",
        f"| 成交额 | {_fmt_wan(bar.amount_wan)} |",
        f"| 换手率 | {_fmt(bar.turnover_pct, 2, '%')} |",
        f"| 总市值 | {_fmt_yuan(valuation.get('market_cap'))} |",
        f"| 流通市值 | {_fmt_yuan(valuation.get('float_market_cap'))} |",
        f"| 动态PE / PB | {_fmt(valuation.get('pe'))} / {_fmt(valuation.get('pb'))} |",
        "",
        "### 技术面与量价",
        "",
        "| 指标 | 数值 | 收盘相对位置 |",
        "| --- | ---: | --- |",
    ]
    for name, value in ma_values:
        position = "待核实" if value is None else ("上方" if bar.close >= value else "下方")
        lines.append(f"| {name} | {_fmt(value)} | 收盘在其{position} |")
    lines.extend(
        [
            f"| 5日均量 | {_fmt_hands(indicators.get('volume_ma5'))} | 今日量能 / 5日均量 = {_fmt(_volume_ratio(bar, indicators), 2, '倍')} |",
            f"| RSI6/12/24 | {_fmt(indicators.get('rsi6'))} / {_fmt(indicators.get('rsi12'))} / {_fmt(indicators.get('rsi24'))} | 短线强弱参考 |",
            "",
            f"当前形态：**{current_pattern['name']}**。{current_pattern['definition']}",
            f"趋势定义：**{current_trend['name']}**。{current_trend['definition']}",
            f"技术结论：{'收盘低于主要均线，趋势仍偏弱。' if below_all else '收盘站上短期主要均线，短线结构修复。' if above_short else '均线位置分化，趋势尚未一致。'}",
            "",
            "### 板块表现",
            "",
            "| 层级 | 板块 | 涨跌幅 | 成交额 | 状态 |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    if data.boards:
        for board in data.boards:
            if board.get("error"):
                lines.append(f"| {board.get('label')} | {board.get('name')} | 待核实 | 待核实 | {board['error']} |")
            else:
                state = "强" if (board.get("change_pct") or 0) > 0 else "弱"
                lines.append(
                    f"| {board.get('label')} | {board.get('name')} | "
                    f"{_fmt(board.get('change_pct'), 2, '%')} | "
                    f"{_fmt_wan((board.get('amount_yuan') or 0) / 10000)} | {state} |"
                )
    else:
        lines.append("| - | 未获取 | 待核实 | 待核实 | 待核实 |")
    core_board = next(
        (
            board
            for board in data.boards
            if board.get("label") == "核心板块" and not board.get("error")
        ),
        None,
    )
    if core_board and core_board.get("change_pct") is not None:
        relative_to_core = bar.change_pct - float(core_board["change_pct"])
        if relative_to_core >= 1:
            strength = "明显强于核心板块"
        elif relative_to_core <= -1:
            strength = "明显弱于核心板块"
        else:
            strength = "与核心板块弹性接近"
        lines.extend(
            [
                "",
                f"个股相对核心板块：本股 **{bar.change_pct:+.2f}%**，"
                f"{core_board.get('name')} **{float(core_board['change_pct']):+.2f}%**，"
                f"差值 **{relative_to_core:+.2f}个百分点**，{strength}。",
            ]
        )

    lines.extend(["", *_render_f10_section(data)])

    lines.extend(
        [
            "",
            "### 财报与公告",
            "",
        ]
    )
    if data.financial:
        financial = data.financial
        income = _number(financial.get("TOTAL_OPERATE_INCOME"))
        profit = _number(financial.get("PARENT_NETPROFIT"))
        lines.extend(
            [
                f"- 最新报告期：{financial.get('REPORTDATE', '')[:10]}（{financial.get('DATATYPE', '')}）；",
                f"- 营业收入：{income / 100000000:.2f}亿元，同比 {_fmt(_number(financial.get('YSTZ')), 2, '%')}；",
                f"- 归母净利润：{profit / 100000000:.2f}亿元，同比 {_fmt(_number(financial.get('SJLTZ')), 2, '%')}；",
                f"- 毛利率：{_fmt(_number(financial.get('XSMLL')), 2, '%')}；每股经营现金流：{_fmt(_number(financial.get('MGJYXJJE')))}。",
            ]
        )
    else:
        lines.append("- 最新财报：待核实。")
    if data.announcements:
        lines.append("- 近期公告：")
        lines.extend(f"  - {item['date']}：{item['title']}" for item in data.announcements)
    else:
        lines.append("- 近期公告：待核实。")

    lines.extend(["", "### 公开新闻线索", ""])
    if data.news:
        stock_name = str(data.stock.get("name", ""))
        stock_code = str(data.stock.get("code", ""))
        has_stock_specific_news = any(
            stock_name in item["title"] or stock_code in item["title"]
            for item in data.news
        )
        if not has_stock_specific_news:
            lines.append("- 以下为相关板块/行业检索结果，标题未直接指向公司，需人工核实关联性。")
        for item in data.news:
            lines.append(f"- {item['date']}｜{item['media']}｜[{item['title']}]({item['url']})")
    else:
        lines.append("- 未获取到可靠新闻或已按配置关闭动态检索。")

    lines.extend(
        [
            "",
            "### 关键价位与预案",
            "",
            f"- 第一/第二支撑：**{supports[0]:.2f}** / **{supports[1]:.2f}**；",
            f"- 第一/第二压力：**{resistances[0]:.2f}** / **{resistances[1]:.2f}**；",
            f"- 若收盘跌破第一支撑，优先降低风险，不等待解释；",
            f"- 若放量站稳第一压力，可继续持有观察；冲高到第二压力且量能衰减时，优先降低单票集中度；",
            "- 禁止因为超卖或大涨而临时放宽风控；所有价位需在下一交易日开盘前复核。",
            "",
            f"> 数据来源：{data.source_note}本节为规则生成，不构成投资建议。",
            "",
        ]
    )
    return "\n".join(lines)


def generate_local_stock_review(
    holding: Mapping[str, str],
    stock_dir: Path,
    config: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Path]:
    data = collect_stock_data(holding, stock_dir, config)
    payload = asdict(data)
    data_path = stock_dir / "local_review_data.json"
    review_path = stock_dir / "local当日复盘.md"
    data_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    review_path.write_text(render_stock_review(data), encoding="utf-8")
    return payload, review_path


def _load_local_data(stock_dir: Path) -> Optional[Dict[str, Any]]:
    path = stock_dir / "local_review_data.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _load_bar(payload: Mapping[str, Any]) -> DailyBar:
    return DailyBar(**payload["bar"])


def render_portfolio_summary(
    review_date: str,
    items: Sequence[Mapping[str, Any]],
    indices: Optional[Sequence[Mapping[str, Any]]] = None,
) -> str:
    rows: List[Dict[str, Any]] = []
    for item in items:
        stock_dir = Path(str(item.get("review_path", ""))).parent
        data = _load_local_data(stock_dir)
        if not data:
            continue
        bar = _load_bar(data)
        holding = data.get("holding", {})
        cost = _number(holding.get("cost")) or 0.0
        shares = _number(holding.get("shares")) or 0.0
        market_value = bar.close * shares
        rows.append(
            {
                "name": data.get("stock", {}).get("name", holding.get("name", "")),
                "bar": bar,
                "cost": cost,
                "shares": shares,
                "market_value": market_value,
                "cost_value": cost * shares,
                "daily_pnl": _daily_pnl(holding, bar),
                "indicators": data.get("indicators", {}),
            }
        )
    if not rows:
        return "本地规则复盘数据缺失，无法生成组合摘要。"

    total_market = sum(row["market_value"] for row in rows)
    total_cost = sum(row["cost_value"] for row in rows)
    total_pnl = total_market - total_cost
    daily_pnl = sum(row["daily_pnl"] for row in rows)
    daily_pct = daily_pnl / (total_market - daily_pnl) if total_market != daily_pnl else None
    largest = max(rows, key=lambda row: row["market_value"])
    weak = [
        row for row in rows
        if row["bar"].close < (row["indicators"].get("ma5") or row["bar"].close)
    ]
    crowded = [row for row in rows if (row["bar"].turnover_pct or 0) >= 10]

    lines: List[str] = [
        f"### {review_date} 本地组合摘要",
        "",
        "| 股票 | 收盘 | 今日涨跌 | 市值 | 今日盈亏 | 浮动盈亏 | 权重 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['bar'].close:.2f} | "
            f"{row['bar'].change_pct:+.2f}% | {row['market_value']:.2f} | "
            f"{row['daily_pnl']:+.2f} | {row['market_value'] - row['cost_value']:+.2f} | "
            f"{row['market_value'] / total_market:.2%} |"
        )
    lines.extend(
        [
            f"| **合计** |  |  | **{total_market:.2f}** | **{daily_pnl:+.2f}** | **{total_pnl:+.2f}** | **100%** |",
            "",
            f"- 组合市值 **{total_market:.2f} 元**，相对成本 **{total_pnl:+.2f} 元 / {total_pnl / total_cost:.2%}**；",
            f"- 今日盈亏 **{daily_pnl:+.2f} 元 / {_fmt_percent(daily_pct)}**；",
            f"- 最大单票暴露：**{largest['name']} {largest['market_value'] / total_market:.2%}**；",
            f"- 收盘低于 MA5 的持仓：**{', '.join(row['name'] for row in weak) or '无'}**；",
            f"- 换手率不低于 10% 的高波动持仓：**{', '.join(row['name'] for row in crowded) or '无'}**。",
            "",
            "### 指数对照",
            "",
            "| 指数 | 今日涨跌幅 | 组合相对表现 |",
            "| --- | ---: | ---: |",
        ]
    )
    for index in indices or []:
        index_pct = index.get("change_pct")
        relative = (
            daily_pct - index_pct
            if daily_pct is not None and index_pct is not None
            else None
        )
        lines.append(
            f"| {index.get('name', '')} | {_fmt_percent(index_pct)} | "
            f"{_fmt_percentage_points(relative)} |"
        )
    if not indices:
        lines.append("| 待核实 | 待核实 | 待核实 |")

    lines.extend(
        [
            "",
            "### 规则化风险结论",
            "",
        ]
    )
    risk_lines: List[str] = []
    if largest["market_value"] / total_market >= 0.4:
        risk_lines.append(
            f"1. {largest['name']} 权重偏高，组合收益与回撤被单票主导，强势时应优先降低集中度。"
        )
    else:
        risk_lines.append("1. 单票集中度尚可，但仍需在次日复核权重变化。")
    if weak:
        risk_lines.append(
            "2. " + "、".join(row["name"] for row in weak)
            + " 收盘低于 MA5，短线趋势偏弱，禁止左侧补仓。"
        )
    else:
        risk_lines.append("2. 所有持仓均未收在 MA5 下方，短线趋势尚未集体破坏。")
    if crowded:
        risk_lines.append(
            "3. " + "、".join(row["name"] for row in crowded)
            + " 换手率过高，波动可能放大，冲高应优先考虑风险控制。"
        )
    else:
        risk_lines.append("3. 未出现换手率不低于 10% 的极端交易拥挤信号。")
    lines.extend(risk_lines)
    lines.extend(
        [
            "",
            "> 本摘要完全由公开行情数据与本地规则生成，不调用 Gemini/智谱，也不构成投资建议。",
            "",
        ]
    )
    return "\n".join(lines)


def generate_portfolio_summary(
    review_date: str,
    reviews: Sequence[Mapping[str, Any]],
    timeout: float = 30.0,
) -> str:
    """Render the portfolio summary and optionally add same-day index context."""

    try:
        indices = fetch_market_indices(review_date, timeout)
    except Exception:
        indices = []
    return render_portfolio_summary(review_date, reviews, indices)
