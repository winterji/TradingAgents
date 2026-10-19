"""Virtual book: signed positions, average prices, and next-open fills (open prices stubbed)."""

import json

import pytest

from desk_runner.book import (
    PendingFill,
    add_pending,
    apply_fill,
    equity,
    load_book,
    process_pending,
    render_for_agents,
    save_book,
)
from desk_runner.config import Paths, load_config
from tradingagents.portfolio import PortfolioContext, Position


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "var")


@pytest.fixture
def cfg():
    return load_config()


def _book(cash: float, *positions: Position) -> PortfolioContext:
    return PortfolioContext(cash=cash, currency="USD", positions=list(positions))


def _position(book: PortfolioContext, ticker: str) -> Position | None:
    return book.position_in(ticker)


def test_new_book_starts_with_initial_cash(paths, cfg):
    book = load_book(paths, cfg)

    assert book.cash == 100000
    assert book.currency == "USD"
    assert book.positions == []
    assert paths.book_file.exists()


def test_buy_then_add_weighted_average():
    book = apply_fill(_book(100000), "AAPL", "buy", 10, 100)
    book = apply_fill(book, "AAPL", "buy", 10, 110)

    position = _position(book, "AAPL")
    assert position.quantity == 20
    assert position.average_price == pytest.approx(105)
    assert book.cash == pytest.approx(97900)


def test_long_to_short_crossing():
    book = _book(100000, Position(ticker="AAPL", quantity=10, average_price=100))

    book = apply_fill(book, "AAPL", "sell", 15, 120)

    position = _position(book, "AAPL")
    assert position.quantity == -5
    assert position.average_price == pytest.approx(120)
    assert book.cash == pytest.approx(100000 + 1800)


def test_short_cover_to_zero_removes():
    book = _book(101800, Position(ticker="AAPL", quantity=-5, average_price=120))

    book = apply_fill(book, "AAPL", "buy", 5, 110)

    assert _position(book, "AAPL") is None
    assert book.cash == pytest.approx(101800 - 550)


def test_sell_part_of_long_keeps_average_price():
    book = _book(100000, Position(ticker="AAPL", quantity=10, average_price=100))

    book = apply_fill(book, "AAPL", "sell", 4, 150)

    position = _position(book, "AAPL")
    assert position.quantity == 6
    assert position.average_price == pytest.approx(100)


def test_add_to_short_is_weighted_average_of_absolute_quantity():
    book = _book(100000, Position(ticker="AAPL", quantity=-5, average_price=120))

    book = apply_fill(book, "AAPL", "sell", 5, 100)

    position = _position(book, "AAPL")
    assert position.quantity == -10
    assert position.average_price == pytest.approx(110)


def test_equity_counts_shorts_negative():
    book = _book(101800, Position(ticker="AAPL", quantity=-5, average_price=120))

    assert equity(book, {"AAPL": 130}) == pytest.approx(101150)


def test_render_mentions_shorts():
    book = _book(101800, Position(ticker="AAPL", quantity=-5, average_price=120))

    assert "Short positions are allowed" in render_for_agents(book, "AAPL")


def test_apply_fill_rejects_non_positive_quantity():
    with pytest.raises(ValueError):
        apply_fill(_book(100000), "AAPL", "buy", 0, 100)


def test_same_day_fill_stays_pending(paths, cfg):
    add_pending(paths, [PendingFill(trade_date="2026-10-17", symbol="AAPL", side="buy", qty=10)])
    calls: list[tuple[str, str]] = []

    def open_price(symbol: str, day: str) -> float | None:
        calls.append((symbol, day))
        return 100.0

    report = process_pending(paths, cfg, "2026-10-17", open_price)

    assert calls == []
    assert [fill.symbol for fill in report.still_pending] == ["AAPL"]
    assert report.filled == []
    assert _position(load_book(paths, cfg), "AAPL") is None
    assert len(json.loads(paths.pending_fills_file.read_text())) == 1


def test_missing_open_stays_pending(paths, cfg):
    add_pending(paths, [PendingFill(trade_date="2026-10-16", symbol="AAPL", side="buy", qty=10)])

    report = process_pending(paths, cfg, "2026-10-17", lambda symbol, day: None)

    assert [fill.symbol for fill in report.missing_price] == ["AAPL"]
    assert report.filled == []
    assert _position(load_book(paths, cfg), "AAPL") is None
    assert len(json.loads(paths.pending_fills_file.read_text())) == 1


def test_process_pending_fills_past_dates(paths, cfg):
    add_pending(
        paths,
        [
            PendingFill(trade_date="2026-10-16", symbol="AAPL", side="buy", qty=3),
            PendingFill(trade_date="2026-10-17", symbol="AAPL", side="buy", qty=1),
        ],
    )

    report = process_pending(paths, cfg, "2026-10-17", lambda symbol, day: 334.0)

    assert [fill.qty for fill in report.filled] == [3]
    assert [fill.qty for fill in report.still_pending] == [1]
    book = load_book(paths, cfg)
    assert _position(book, "AAPL").quantity == 3
    assert _position(book, "AAPL").average_price == pytest.approx(334)
    assert book.cash == pytest.approx(100000 - 3 * 334)
    remaining = json.loads(paths.pending_fills_file.read_text())
    assert [entry["trade_date"] for entry in remaining] == ["2026-10-17"]


def test_process_pending_empties_file_when_all_filled(paths, cfg):
    add_pending(paths, [PendingFill(trade_date="2026-10-16", symbol="AAPL", side="sell", qty=2)])

    process_pending(paths, cfg, "2026-10-17", lambda symbol, day: 334.0)

    assert json.loads(paths.pending_fills_file.read_text()) == []
    assert _position(load_book(paths, cfg), "AAPL").quantity == -2


def test_save_book_round_trips(paths, cfg):
    book = _book(5000, Position(ticker="AAPL", quantity=2, average_price=10))

    save_book(paths, book)

    assert load_book(paths, cfg) == book
