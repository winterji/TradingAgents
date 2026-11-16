"""Market-implied Fed policy path from 30-day Fed funds futures on Yahoo Finance.

Each CBOT 30-day Fed funds contract (``ZQ<month><yy>.CBT``) settles on the
average effective Fed funds rate over its month, so ``100 - price`` is the rate
the market prices for that month. Read along the strip, the contracts give the
priced path of policy: what CME FedWatch derives its cut/hike odds from, with
real money behind it. Unlike prediction markets it is not a gambling site, so
no national block applies.
"""
from datetime import date, datetime, timedelta

import yfinance as yf

from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo.common import yf_retry

# CME futures month codes, January through December.
MONTH_CODES = "FGHJKMNQUVXZ"

# Contracts read by default: the analysis month and the eleven after it.
DEFAULT_MONTHS = 12

# Daily volume under which a contract's price is a thin, less reliable signal.
THIN_VOLUME = 100

# Calendar days searched back for a contract's latest close (covers holidays).
LOOKBACK_DAYS = 10


def contract_symbols(as_of_date: str, months: int) -> list[tuple[str, str]]:
    """``(YYYY-MM, Yahoo symbol)`` for the contract month of ``as_of_date`` and the ones after it."""
    first = datetime.strptime(as_of_date, "%Y-%m-%d")
    out = []
    for i in range(months):
        year, month = divmod(first.month - 1 + i, 12)
        year += first.year
        out.append((f"{year}-{month + 1:02d}", f"ZQ{MONTH_CODES[month]}{year % 100:02d}.CBT"))
    return out


def _latest_bar(symbol: str, as_of: date):
    """The contract's last ``(date, close, volume)`` on or before ``as_of``, or None."""
    bars = yf_retry(lambda: yf.Ticker(symbol).history(
        start=(as_of - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
        end=(as_of + timedelta(days=1)).strftime("%Y-%m-%d"),
    ))
    if bars is None or bars.empty:
        return None
    # Rows after the analysis date would be look-ahead in a historical run.
    bars = bars[[d.date() <= as_of for d in bars.index]]
    if bars.empty:
        return None
    last = bars.iloc[-1]
    return bars.index[-1].date(), float(last["Close"]), int(last["Volume"])


def get_fed_rate_expectations(as_of_date: str | None = None, months: int | None = None) -> str:
    """The Fed funds rate path priced by 30-day Fed funds futures as of ``as_of_date``.

    Args:
        as_of_date: The analysis date (yyyy-mm-dd); ``None`` means today.
        months: Contract months to read, starting with the analysis month;
            ``None`` uses DEFAULT_MONTHS.

    Returns:
        A markdown table of each month's implied average rate and its change
        against the first quoted month, in basis points and in 25bp moves.

    Raises:
        VendorUnavailableError: Yahoo served no quote for any contract.
    """
    as_of_date = as_of_date or get_current_date()
    months = months or DEFAULT_MONTHS
    as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()

    quoted, missing = [], []
    for month, symbol in contract_symbols(as_of_date, months):
        bar = _latest_bar(symbol, as_of)
        if bar is None:
            missing.append(month)
        else:
            quoted.append((month, symbol, *bar))
    if not quoted:
        raise VendorUnavailableError(f"Yahoo Finance served no Fed funds futures quote as of {as_of_date}")

    base_month, _, _, base_price, _ = quoted[0]
    lines = [
        f"## Fed funds futures: market-implied policy path as of {as_of_date}",
        "Each month's rate is 100 minus its 30-day Fed funds futures price: the average "
        "effective Fed funds rate the market prices for that month. A month holding an "
        "FOMC meeting blends the rates before and after it, so a move shows fully only "
        "in the month after the meeting. A positive change prices hikes, a negative "
        "one cuts. These are traded prices, not a forecast.",
        "",
        f"| Month | Implied rate | vs {base_month} (bp) | 25bp moves | Volume | Last close |",
        "|---|---|---|---|---|---|",
    ]
    for month, symbol, day, price, volume in quoted:
        # Price points are rate points, inverted; round off float noise.
        bp = round((base_price - price) * 100, 1) + 0.0
        thin = " (thin)" if volume < THIN_VOLUME else ""
        lines.append(
            f"| {month} | {100 - price:.2f}% | {bp:+.0f} | {bp / 25:+.1f} | {volume}{thin} | {day} ({symbol}) |"
        )
    if missing:
        lines += ["", f"Months with no quote on Yahoo (expired or not listed): {', '.join(missing)}."]
    return "\n".join(lines) + "\n"
