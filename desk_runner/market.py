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


def _price(value: str) -> float:
    """A CSV price cell as a float; an empty or non-numeric cell is 0 (no usable price)."""
    try:
        return float(value)
    except ValueError:
        return 0.0


def _daily_rows(symbol: str, trade_date: str, end: str) -> list[dict[str, str]]:
    """Vendor OHLCV rows for ``symbol`` from the lookback window up to ``end``."""
    start = (date.fromisoformat(trade_date) - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    text = route_to_vendor("get_stock_data", symbol, start, end)
    # The vendor prepends ``#`` header lines and a blank line before the CSV.
    body = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(body))))


def last_close(symbol: str, trade_date: str) -> float:
    """Close of the latest session before ``trade_date`` (the previous close)."""
    closes = [
        (row["Date"][:10], _price(row["Close"]))
        for row in _daily_rows(symbol, trade_date, trade_date)
        if row["Date"][:10] < trade_date and _price(row["Close"]) > 0
    ]
    if not closes:
        raise MarketDataError(f"no closing price for {symbol} before {trade_date}")
    return max(closes)[1]


def open_on(symbol: str, day: str) -> float | None:
    """Opening price of ``symbol`` on ``day``, or None if that row is absent or not positive."""
    for row in _daily_rows(symbol, day, day):
        if row["Date"][:10] == day:
            open_price = _price(row["Open"])
            return open_price if open_price > 0 else None
    return None
