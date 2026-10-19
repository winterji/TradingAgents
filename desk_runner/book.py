"""The virtual book and its fills.

The book is the upstream ``PortfolioContext``: cash plus signed positions (negative
quantity is a short). Orders are queued as pending fills dated by trade date and
filled at the next session's open, once that open is known. A fill whose open is
not known stays pending; a price is never guessed.
"""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from desk_runner.config import DeskConfig, Paths, write_json_atomic
from tradingagents.dataflows.files import locked
from tradingagents.portfolio import PortfolioContext, Position

SHORT_NOTE = "- Short positions are allowed (negative quantity = short)."


class PendingFill(BaseModel):
    trade_date: str = Field(description="Trading day the order was decided (YYYY-MM-DD)")
    symbol: str
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0)


class FillReport(BaseModel):
    filled: list[PendingFill] = Field(default_factory=list)
    still_pending: list[PendingFill] = Field(default_factory=list)
    missing_price: list[PendingFill] = Field(default_factory=list)


OpenPrice = Callable[[str, str], float | None]


def save_book(paths: Paths, book: PortfolioContext) -> None:
    write_json_atomic(paths.book_file, book.model_dump())


def load_book(paths: Paths, cfg: DeskConfig) -> PortfolioContext:
    """The saved book, or a new one with the initial cash when the file is missing."""
    if not paths.book_file.exists():
        book = PortfolioContext(cash=cfg.initial_cash, currency=cfg.currency, positions=[])
        save_book(paths, book)
        return book
    return PortfolioContext.model_validate(json.loads(paths.book_file.read_text()))


def apply_fill(
    book: PortfolioContext, symbol: str, side: Literal["buy", "sell"], qty: int, price: float
) -> PortfolioContext:
    """A new book with one fill applied: cash and the signed position updated."""
    if qty <= 0 or price <= 0:
        raise ValueError(f"fill needs a positive quantity and price, got {qty} at {price}")
    if book.cash is None:
        raise ValueError("cannot fill against a book without cash")

    delta = qty if side == "buy" else -qty
    cash = book.cash - qty * price if side == "buy" else book.cash + qty * price
    current = book.position_in(symbol)
    old_qty = current.quantity if current else 0.0
    old_avg = (current.average_price or 0.0) if current else 0.0
    new_qty = old_qty + delta

    positions = [p for p in book.positions if p is not current]
    if new_qty != 0:
        if old_qty == 0 or (old_qty > 0) == (delta > 0):
            # Opening or growing in the same direction: weighted average of |quantity|.
            avg = (abs(old_qty) * old_avg + qty * price) / (abs(old_qty) + qty)
        elif (new_qty > 0) == (old_qty > 0):
            # Shrinking toward zero without crossing: the average stays.
            avg = old_avg
        else:
            # Crossing zero: the remainder is a new position at the fill price.
            avg = price
        positions.append(Position(ticker=symbol, quantity=new_qty, average_price=avg))

    return PortfolioContext(cash=cash, currency=book.currency, positions=positions)


def equity(book: PortfolioContext, prices: dict[str, float]) -> float:
    """Cash plus the market value of every position (shorts count negative)."""
    if book.cash is None:
        raise ValueError("equity needs the book's cash")
    return book.cash + sum(p.quantity * prices[p.ticker] for p in book.positions)


def render_for_agents(book: PortfolioContext, symbol: str) -> str:
    return f"{book.render(symbol)}\n{SHORT_NOTE}"


def _locked(path: Path) -> AbstractContextManager[bool]:
    """``locked`` for a runtime file whose directory may not exist yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return locked(path)


def read_pending(paths: Paths) -> list[PendingFill]:
    if not paths.pending_fills_file.exists():
        return []
    return [
        PendingFill.model_validate(entry)
        for entry in json.loads(paths.pending_fills_file.read_text())
    ]


def _write_pending(paths: Paths, fills: list[PendingFill]) -> None:
    write_json_atomic(paths.pending_fills_file, [fill.model_dump() for fill in fills])


def add_pending(paths: Paths, fills: list[PendingFill]) -> None:
    with _locked(paths.pending_fills_file):
        _write_pending(paths, read_pending(paths) + fills)


def replace_pending_for_date(paths: Paths, trade_date: str, fills: list[PendingFill]) -> None:
    """Replace the pending fills dated ``trade_date`` with ``fills``; other dates are kept. Idempotent."""
    with _locked(paths.pending_fills_file):
        kept = [fill for fill in read_pending(paths) if fill.trade_date != trade_date]
        _write_pending(paths, kept + fills)


def process_pending(paths: Paths, cfg: DeskConfig, today: str, open_price: OpenPrice) -> FillReport:
    """Fill pending orders dated before ``today`` at their open; keep the rest pending.

    Orders dated ``today`` or later are not priced at all. An order whose open is
    unknown stays pending and is reported in ``missing_price``.
    """
    report = FillReport()
    with _locked(paths.pending_fills_file), _locked(paths.book_file):
        book = load_book(paths, cfg)
        remaining: list[PendingFill] = []
        for fill in read_pending(paths):
            if fill.trade_date >= today:
                report.still_pending.append(fill)
                remaining.append(fill)
                continue
            price = open_price(fill.symbol, fill.trade_date)
            if price is None:
                report.missing_price.append(fill)
                remaining.append(fill)
                continue
            book = apply_fill(book, fill.symbol, fill.side, fill.qty, price)
            report.filled.append(fill)
        save_book(paths, book)
        _write_pending(paths, remaining)
    return report
