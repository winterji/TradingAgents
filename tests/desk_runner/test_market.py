"""NYSE trading-day check and price helpers, with the upstream vendor router stubbed."""

import pytest

from desk_runner import market
from desk_runner.market import MarketDataError, is_trading_day, last_close, open_on

CSV_TEMPLATE = (
    "# Stock data for AAPL from 2026-10-06 to 2026-10-19\n"
    "# Total records: {count}\n"
    "\n"
    "Date,Open,High,Low,Close,Volume,Dividends,Stock Splits\n"
    "{rows}"
)

ROWS = {
    "2026-10-15": "2026-10-15,330,335,329,333.1,1000,0.0,0.0\n",
    "2026-10-16": "2026-10-16,334,344,333,343.39,1200,0.0,0.0\n",
    "2026-10-19": "2026-10-19,345,350,344,348.5,900,0.0,0.0\n",
}


def _stub_vendor(monkeypatch, dates: list[str], calls: list[tuple] | None = None) -> None:
    body = "".join(ROWS[d] for d in dates)
    csv_text = CSV_TEMPLATE.format(count=len(dates), rows=body)

    def fake_route(method, symbol, start, end):
        if calls is not None:
            calls.append((method, symbol, start, end))
        return csv_text

    monkeypatch.setattr(market, "route_to_vendor", fake_route)


def test_last_close_excludes_trade_date_row(monkeypatch):
    _stub_vendor(monkeypatch, ["2026-10-15", "2026-10-16", "2026-10-19"])

    assert last_close("AAPL", "2026-10-19") == 343.39


def test_last_close_requests_ten_days_before_trade_date(monkeypatch):
    calls: list[tuple] = []
    _stub_vendor(monkeypatch, ["2026-10-16"], calls)

    last_close("AAPL", "2026-10-19")

    assert calls == [("get_stock_data", "AAPL", "2026-10-09", "2026-10-19")]


def test_open_on_present_row(monkeypatch):
    _stub_vendor(monkeypatch, ["2026-10-15", "2026-10-16"])

    assert open_on("AAPL", "2026-10-16") == 334.0


def test_open_on_missing_row_is_none(monkeypatch):
    _stub_vendor(monkeypatch, ["2026-10-15", "2026-10-16"])

    assert open_on("AAPL", "2026-10-19") is None


def test_open_on_non_positive_open_is_none(monkeypatch):
    csv_text = CSV_TEMPLATE.format(count=1, rows="2026-10-19,0,0,0,348.5,900,0.0,0.0\n")
    monkeypatch.setattr(market, "route_to_vendor", lambda *a, **k: csv_text)

    assert open_on("AAPL", "2026-10-19") is None


def test_last_close_no_data_raises(monkeypatch):
    _stub_vendor(monkeypatch, [])

    with pytest.raises(MarketDataError):
        last_close("AAPL", "2026-10-19")


def test_last_close_skips_non_positive_close(monkeypatch):
    csv_text = CSV_TEMPLATE.format(
        count=2,
        rows=ROWS["2026-10-16"] + "2026-10-15,330,335,329,0,1000,0.0,0.0\n",
    )
    monkeypatch.setattr(market, "route_to_vendor", lambda *a, **k: csv_text)

    assert last_close("AAPL", "2026-10-19") == 343.39


def test_trading_days():
    assert is_trading_day("2026-10-19") is True
    assert is_trading_day("2026-10-17") is False
    assert is_trading_day("2026-11-26") is False


def test_empty_price_cells_are_skipped(monkeypatch):
    csv_text = CSV_TEMPLATE.format(
        count=2,
        rows=ROWS["2026-10-16"] + "2026-10-19,,,,,900,0.0,0.0\n",
    )
    monkeypatch.setattr(market, "route_to_vendor", lambda *a, **k: csv_text)

    assert last_close("AAPL", "2026-10-20") == 343.39
    assert open_on("AAPL", "2026-10-19") is None
