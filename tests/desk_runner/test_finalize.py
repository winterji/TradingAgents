"""Finalize: the decision file, pending fills and memory entries, offline (prices stubbed)."""

import json
import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from desk_runner.book import PendingFill, add_pending, save_book
from desk_runner.config import (
    DEFAULT_CONFIG_PATH,
    DeskConfig,
    Paths,
    load_config,
    write_json_atomic,
)
from desk_runner.decision_file import DecisionFile, SymbolDecision, new_run_id
from desk_runner.finalize import FinalizeError, finalize
from desk_runner.market import MarketDataError
from desk_runner.sizing import Order
from tradingagents.portfolio import PortfolioContext, Position

DATE = "2026-10-17"
NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
PRICES = {"AAPL": 343.39, "GOOGL": 250.0, "AMZN": 220.0}


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path / "var")


@pytest.fixture
def cfg(paths) -> DeskConfig:
    return load_config(DEFAULT_CONFIG_PATH).model_copy(update={"var_dir": paths.var_dir})


def _price(overrides: dict[str, Exception] | None = None):
    failures = overrides or {}

    def price(symbol: str, trade_date: str) -> float:
        assert trade_date == DATE
        if symbol in failures:
            raise failures[symbol]
        return PRICES[symbol]

    return price


def _complete(paths: Paths, symbol: str, rating: str = "Overweight", action: str = "Buy") -> None:
    """A symbol whose pipeline finished: state file with a final decision plus structured outputs."""
    _write_state(paths, symbol, rating=rating, decision=f"{symbol} final decision")
    _write_structured(
        paths,
        symbol,
        "research_manager",
        {"recommendation": rating, "rationale": "r", "strategic_actions": "s"},
    )
    _write_structured(
        paths,
        symbol,
        "trader",
        {"action": action, "reasoning": "r", "position_sizing": "5% of portfolio"},
    )
    _write_structured(
        paths,
        symbol,
        "portfolio_manager",
        {"rating": rating, "executive_summary": "e", "investment_thesis": "t"},
    )


def _write_state(
    paths: Paths, symbol: str, *, rating: str = "Overweight", decision: str = "final"
) -> None:
    path = paths.state_file(DATE, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"messages": [], "final_rating": rating, "final_trade_decision": decision})
    )


def _write_structured(paths: Paths, symbol: str, node: str, data: dict) -> None:
    write_json_atomic(paths.symbol_dir(DATE, symbol) / "structured" / f"{node}.json", data)


def _three_complete(paths: Paths) -> None:
    for symbol in ("AAPL", "GOOGL", "AMZN"):
        _complete(paths, symbol)


def _by_symbol(decision_file: DecisionFile) -> dict[str, SymbolDecision]:
    return {d.symbol: d for d in decision_file.decisions}


def test_finalize_writes_valid_file(paths, cfg):
    _three_complete(paths)

    result = finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    written = DecisionFile.model_validate_json(paths.decisions_file(DATE).read_text())
    assert written == result
    assert written.trade_date == DATE
    assert written.book_before.cash == 100000
    assert written.equity_before == 100000
    aapl = _by_symbol(written)["AAPL"]
    assert aapl.status == "order"
    assert aapl.order == Order(side="buy", qty=14)
    assert aapl.reference_price == 343.39
    assert aapl.state_file == "runs/2026-10-17/AAPL/state.json"


def test_finalize_adds_pending_and_memory_entries(paths, cfg):
    _three_complete(paths)

    finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    pending = json.loads(paths.pending_fills_file.read_text())
    assert len(pending) == 3
    aapl = next(entry for entry in pending if entry["symbol"] == "AAPL")
    assert aapl == {"trade_date": DATE, "symbol": "AAPL", "side": "buy", "qty": 14}
    memory = paths.memory_log.read_text()
    assert f"[{DATE} | AAPL | Overweight | pending]" in memory


def test_incomplete_symbol_is_review(paths, cfg):
    _complete(paths, "AAPL")
    _complete(paths, "AMZN")
    _write_state(paths, "GOOGL", decision="")

    result = finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    googl = _by_symbol(result)["GOOGL"]
    assert googl.status == "review"
    assert googl.reason == "pipeline incomplete"
    assert googl.order is None
    memory = paths.memory_log.read_text() if paths.memory_log.exists() else ""
    assert "GOOGL |" not in memory
    assert "AAPL |" in memory


def test_missing_price_is_review(paths, cfg):
    _three_complete(paths)

    result = finalize(
        paths,
        cfg,
        DATE,
        price=_price({"AMZN": MarketDataError("no data")}),
        now=lambda: NOW,
    )

    amzn = _by_symbol(result)["AMZN"]
    assert amzn.status == "review"
    assert "no reference price" in amzn.reason
    assert amzn.reference_price is None
    assert _by_symbol(result)["AAPL"].status == "order"


def test_held_symbol_without_price_makes_equity_unknown(paths, cfg):
    save_book(
        paths,
        PortfolioContext(
            cash=50000,
            currency="USD",
            positions=[Position(ticker="TSLA", quantity=10, average_price=200)],
        ),
    )
    _complete(paths, "AAPL")

    result = finalize(
        paths, cfg, DATE, price=_price({"TSLA": MarketDataError("no data")}), now=lambda: NOW
    )

    aapl = _by_symbol(result)["AAPL"]
    assert aapl.status == "review"
    assert "no reference price" in aapl.reason
    assert result.equity_before is None


def test_finalize_is_idempotent(paths, cfg):
    _three_complete(paths)
    finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)
    before = paths.decisions_file(DATE).read_bytes()

    with pytest.raises(FinalizeError):
        finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    assert paths.decisions_file(DATE).read_bytes() == before


def test_order_iff_status_order():
    with pytest.raises(ValidationError):
        SymbolDecision(
            symbol="AAPL",
            status="review",
            reason="r",
            current_qty=0,
            state_file="runs/2026-10-17/AAPL/state.json",
            order=Order(side="buy", qty=1),
        )
    with pytest.raises(ValidationError):
        SymbolDecision(
            symbol="AAPL",
            status="order",
            reason="r",
            current_qty=0,
            state_file="runs/2026-10-17/AAPL/state.json",
        )


def test_new_run_id_format():
    run_id = new_run_id(NOW)

    assert re.fullmatch(r"2026-10-17T12:00:00Z-[0-9a-f]{6}", run_id)


def test_earlier_unfilled_order_blocks_symbol(paths, cfg):
    """C1: day-1 orders still pending (their open was unknown) make day-2 review, not order."""
    day1 = [
        PendingFill(trade_date="2026-10-16", symbol=symbol, side="buy", qty=5)
        for symbol in ("AAPL", "GOOGL", "AMZN")
    ]
    add_pending(paths, day1)
    before = paths.pending_fills_file.read_text()
    _three_complete(paths)

    result = finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    for symbol in ("AAPL", "GOOGL", "AMZN"):
        decision = _by_symbol(result)[symbol]
        assert decision.status == "review"
        assert decision.reason == "earlier order not yet filled"
        assert decision.order is None
    assert json.loads(paths.pending_fills_file.read_text()) == json.loads(before)


def test_earlier_unfilled_order_blocks_only_its_symbol(paths, cfg):
    add_pending(paths, [PendingFill(trade_date="2026-10-16", symbol="AAPL", side="buy", qty=5)])
    _three_complete(paths)

    result = finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    assert _by_symbol(result)["AAPL"].status == "review"
    assert _by_symbol(result)["GOOGL"].status == "order"


def test_crash_after_pending_write_rerun_has_one_copy(paths, cfg, monkeypatch):
    """I6: a crash on the decision file write leaves no decision file; a rerun completes the day."""
    import desk_runner.finalize as finalize_module

    _three_complete(paths)
    real_write = finalize_module.write_json_atomic
    failures = {"left": 1}

    def crash_once_on_decision_file(path, data):
        if path == paths.decisions_file(DATE) and failures["left"] > 0:
            failures["left"] -= 1
            raise OSError("simulated crash")
        return real_write(path, data)

    monkeypatch.setattr(finalize_module, "write_json_atomic", crash_once_on_decision_file)
    with pytest.raises(OSError, match="simulated crash"):
        finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)
    assert not paths.decisions_file(DATE).exists()
    assert len(json.loads(paths.pending_fills_file.read_text())) == 3

    finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    assert paths.decisions_file(DATE).exists()
    pending = json.loads(paths.pending_fills_file.read_text())
    assert sorted(entry["symbol"] for entry in pending) == ["AAPL", "AMZN", "GOOGL"]
    assert len(pending) == 3


def test_rerun_after_success_still_refuses_and_keeps_one_copy(paths, cfg):
    _three_complete(paths)
    finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    with pytest.raises(FinalizeError):
        finalize(paths, cfg, DATE, price=_price(), now=lambda: NOW)

    assert len(json.loads(paths.pending_fills_file.read_text())) == 3
