"""NYSE trading-day check and daily price helpers.

Prices come from the upstream vendor router, so the configured vendor chain
decides where data is fetched from. Nothing here talks to a broker.
"""

import csv
import io
from datetime import date, timedelta

import exchange_calendars as xcals

from tradingagents.dataflows.router import route_to_vendor

_NYSE = xcals.get_calendar("XNYS")
# Calendar days requested before the trade date, so the previous session is found
# across weekends and holidays.
_LOOKBACK_DAYS = 10


class MarketDataError(Exception):
    """No usable daily price data for a symbol."""


def is_trading_day(day: str) -> bool:
    """Whether ``day`` (YYYY-MM-DD) is an NYSE trading session."""
    return bool(_NYSE.is_session(day))


def _price(value: str | None) -> float:
    """A CSV price cell as a float; an empty or non-numeric cell is 0 (no usable price)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


_REQUIRED_COLUMNS = ("Date", "Open", "Close")


def _daily_rows(symbol: str, trade_date: str, end: str) -> list[dict[str, str]]:
    """Vendor OHLCV rows for ``symbol`` from the lookback window up to ``end``.

    Every failure to get usable rows (a vendor error, a sentinel instead of CSV,
    a CSV without the required columns) is raised as MarketDataError.
    """
    start = (date.fromisoformat(trade_date) - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    try:
        text = route_to_vendor("get_stock_data", symbol, start, end)
    except Exception as exc:
        raise MarketDataError(f"vendor request failed for {symbol}: {exc}") from exc
    # The vendor prepends ``#`` header lines and a blank line before the CSV.
    body = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(body)))
    fieldnames = reader.fieldnames or []
    missing = [column for column in _REQUIRED_COLUMNS if column not in fieldnames]
    if missing:
        raise MarketDataError(f"vendor data for {symbol} has no {', '.join(missing)} column")
    return list(reader)


def last_close(symbol: str, trade_date: str) -> float:
    """Close of the latest session before ``trade_date`` (the previous close)."""
    closes = [
        ((row["Date"] or "")[:10], _price(row["Close"]))
        for row in _daily_rows(symbol, trade_date, trade_date)
        if (row["Date"] or "")[:10] < trade_date and _price(row["Close"]) > 0
    ]
    if not closes:
        raise MarketDataError(f"no closing price for {symbol} before {trade_date}")
    return max(closes)[1]


def open_on(symbol: str, day: str) -> float | None:
    """Opening price of ``symbol`` on ``day``, or None if that row is absent or not positive.

    Returns None as well when the vendor data is unusable (MarketDataError), so the
    caller leaves the fill pending. A price is never guessed.
    """
    try:
        rows = _daily_rows(symbol, day, day)
    except MarketDataError:
        return None
    for row in rows:
        if (row["Date"] or "")[:10] == day:
            open_price = _price(row["Open"])
            return open_price if open_price > 0 else None
    return None
