"""30-day Fed funds futures vendor: contract symbols, the implied rate path,
look-ahead safety, missing contracts, and router integration.

All Yahoo access is mocked, so these run without a network connection.
"""
import pandas as pd
import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import fed_futures


def _bars(rows):
    """A Yahoo history frame from ``[(date, close, volume), ...]``."""
    index = pd.DatetimeIndex([d for d, _, _ in rows]).tz_localize("America/Chicago")
    return pd.DataFrame({"Close": [c for _, c, _ in rows], "Volume": [v for _, _, v in rows]},
                        index=index)


def _yahoo(monkeypatch, prices):
    """Serve ``prices[symbol]`` as that contract's history; unknown symbols answer None."""
    asked = []

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, start, end):
            asked.append((self.symbol, start, end))
            return prices.get(self.symbol)

    monkeypatch.setattr(fed_futures.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(fed_futures, "yf_retry", lambda fn: fn())
    return asked


@pytest.mark.unit
def test_contract_symbols_roll_over_the_year():
    assert fed_futures.contract_symbols("2026-11-03", 4) == [
        ("2026-11", "ZQX26.CBT"), ("2026-12", "ZQZ26.CBT"),
        ("2027-01", "ZQF27.CBT"), ("2027-02", "ZQG27.CBT"),
    ]


@pytest.mark.unit
def test_report_gives_the_implied_rate_path_against_the_current_month(monkeypatch):
    _yahoo(monkeypatch, {
        "ZQX26.CBT": _bars([("2026-11-03", 96.1175, 1830)]),
        "ZQZ26.CBT": _bars([("2026-11-03", 95.8675, 1749)]),
        "ZQF27.CBT": _bars([("2026-11-03", 96.6175, 2592)]),
    })
    out = fed_futures.get_fed_rate_expectations("2026-11-03", months=3)
    assert "| 2026-11 | 3.88% | +0 | +0.0 |" in out
    assert "| 2026-12 | 4.13% | +25 | +1.0 |" in out
    assert "| 2027-01 | 3.38% | -50 | -2.0 |" in out


@pytest.mark.unit
def test_rows_after_the_analysis_date_are_never_read(monkeypatch):
    _yahoo(monkeypatch, {
        "ZQF26.CBT": _bars([("2026-01-02", 96.0, 500), ("2026-01-05", 99.0, 500)]),
    })
    out = fed_futures.get_fed_rate_expectations("2026-01-02", months=1)
    assert "4.00%" in out and "1.00%" not in out


@pytest.mark.unit
def test_thin_contracts_are_flagged(monkeypatch):
    _yahoo(monkeypatch, {
        "ZQX26.CBT": _bars([("2026-11-03", 96.0, 1830)]),
        "ZQZ26.CBT": _bars([("2026-11-03", 96.0, 2)]),
    })
    out = fed_futures.get_fed_rate_expectations("2026-11-03", months=2)
    assert "| 2026-12 | 4.00% | +0 | +0.0 | 2 (thin) |" in out


@pytest.mark.unit
def test_contracts_yahoo_no_longer_lists_are_skipped_and_named(monkeypatch):
    # Yahoo drops expired contracts, so a historical run may miss its first months.
    _yahoo(monkeypatch, {"ZQZ26.CBT": _bars([("2026-11-03", 96.0, 900)])})
    out = fed_futures.get_fed_rate_expectations("2026-11-03", months=2)
    assert "| 2026-12 | 4.00% | +0 |" in out
    assert "2026-11" in out and "no quote" in out


@pytest.mark.unit
def test_no_contract_at_all_is_a_vendor_outage(monkeypatch):
    _yahoo(monkeypatch, {})
    with pytest.raises(VendorUnavailableError):
        fed_futures.get_fed_rate_expectations("2026-11-03", months=2)


@pytest.mark.unit
def test_router_degrades_an_outage_to_a_sentinel(monkeypatch):
    _yahoo(monkeypatch, {})
    out = router.route_to_vendor("get_fed_rate_expectations", "2026-11-03")
    assert out.startswith("DATA_UNAVAILABLE")


@pytest.mark.unit
def test_news_analyst_is_offered_the_tool():
    from tradingagents.agents.analysts.news_analyst import TOOLS
    from tradingagents.agents.tools import get_fed_rate_expectations

    assert get_fed_rate_expectations in TOOLS
