"""``ma finalize``: turn each symbol's finished pipeline into a decision, pending orders and memory entries.

Inside the decisions lock the writes happen in a fixed order: this trade date's pending fills
(replaced, never appended, so a rerun cannot double them), then the decision file (atomic), then
the memory log. A crash before the decision file leaves no decision file, so a rerun completes the
day. A crash after it makes a rerun refuse, so no order is queued twice. A symbol with an earlier
order still pending is ``review``: its position is not known yet, so nothing is sized for it.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from desk_runner.book import PendingFill, equity, load_book, read_pending, replace_pending_for_date
from desk_runner.config import DeskConfig, Paths, upstream_config, write_json_atomic
from desk_runner.decision_file import DecisionFile, Rounds, SymbolDecision, new_run_id
from desk_runner.market import MarketDataError, last_close
from desk_runner.sizing import size_decision
from desk_runner.state_store import StateError, load_state
from tradingagents.agents.schemas import PortfolioDecision, ResearchPlan, TraderProposal
from tradingagents.dataflows.files import locked
from tradingagents.memory.log import TradingMemoryLog
from tradingagents.portfolio import PortfolioContext

M = TypeVar("M", bound=BaseModel)
PriceFunction = Callable[[str, str], float]
Clock = Callable[[], datetime]
RUN_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PIPELINE_INCOMPLETE = "pipeline incomplete"
NO_REFERENCE_PRICE = "no reference price"
EARLIER_ORDER_UNFILLED = "earlier order not yet filled"


class FinalizeError(Exception):
    """The day cannot be finalized: the decision file exists or a structured output is unreadable."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def finalize(
    paths: Paths,
    cfg: DeskConfig,
    date: str,
    price: PriceFunction = last_close,
    now: Clock = _utcnow,
) -> DecisionFile:
    """Write the decision file for ``date``, queue its orders and log its decisions."""
    decisions_path = paths.decisions_file(date)
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    with locked(decisions_path):
        if decisions_path.exists():
            raise FinalizeError(f"decision file already exists: {decisions_path}")
        generated = now()
        book = load_book(paths, cfg)
        states = {symbol: _load_state_or_none(paths, date, symbol) for symbol in cfg.symbols}
        prices = _reference_prices(date, price, [*cfg.symbols, *(p.ticker for p in book.positions)])
        equity_before = _equity_or_none(book, prices)
        unfilled = {fill.symbol for fill in read_pending(paths) if fill.trade_date < date}
        decisions = [
            _decide(
                paths, date, symbol, states[symbol], book, prices, equity_before, symbol in unfilled
            )
            for symbol in cfg.symbols
        ]
        decision_file = DecisionFile(
            run_id=new_run_id(generated),
            trade_date=date,
            generated_at=generated.astimezone(UTC).strftime(RUN_TIME_FORMAT),
            models=cfg.models,
            rounds=Rounds(debate=cfg.max_debate_rounds, risk=cfg.max_risk_discuss_rounds),
            book_before=book,
            equity_before=equity_before,
            decisions=decisions,
        )
        replace_pending_for_date(
            paths,
            date,
            [
                PendingFill(trade_date=date, symbol=d.symbol, side=d.order.side, qty=d.order.qty)
                for d in decisions
                if d.order is not None
            ],
        )
        write_json_atomic(decisions_path, decision_file.model_dump(mode="json"))
        _log_decisions(paths, cfg, date, states)
    return decision_file


def _load_state_or_none(paths: Paths, date: str, symbol: str) -> dict[str, Any] | None:
    try:
        return load_state(paths, date, symbol)
    except StateError:
        return None


def _final_text(state: dict[str, Any] | None) -> str:
    return (state or {}).get("final_trade_decision") or ""


def _reference_prices(date: str, price: PriceFunction, symbols: list[str]) -> dict[str, float]:
    """Reference price for each symbol that has one; a symbol without one is left out, never guessed."""
    prices: dict[str, float] = {}
    for symbol in dict.fromkeys(symbols):
        try:
            prices[symbol] = price(symbol, date)
        except MarketDataError:
            continue
    return prices


def _equity_or_none(book: PortfolioContext, prices: dict[str, float]) -> float | None:
    """Equity at the reference prices, or None when a held symbol has no price."""
    if any(p.ticker not in prices for p in book.positions):
        return None
    return equity(book, prices)


def _decide(
    paths: Paths,
    date: str,
    symbol: str,
    state: dict[str, Any] | None,
    book: PortfolioContext,
    prices: dict[str, float],
    equity_before: float | None,
    earlier_unfilled: bool,
) -> SymbolDecision:
    held = book.position_in(symbol)
    current_qty = int(held.quantity) if held is not None else 0
    state_file = paths.state_file(date, symbol).relative_to(paths.var_dir).as_posix()
    base: dict[str, Any] = {"symbol": symbol, "current_qty": current_qty, "state_file": state_file}

    if earlier_unfilled:
        return SymbolDecision(status="review", reason=EARLIER_ORDER_UNFILLED, **base)
    if not _final_text(state):
        return SymbolDecision(status="review", reason=PIPELINE_INCOMPLETE, **base)

    structured_dir = paths.symbol_dir(date, symbol) / "structured"
    research = _read_structured(structured_dir / "research_manager.json", ResearchPlan)
    trader = _read_structured(structured_dir / "trader.json", TraderProposal)
    portfolio = _read_structured(structured_dir / "portfolio_manager.json", PortfolioDecision)
    final_rating = state.get("final_rating") or "REVIEW"
    trader_action = trader.action.value if trader is not None else None
    position_sizing = trader.position_sizing if trader is not None else None
    reference = prices.get(symbol)

    fields: dict[str, Any] = {
        **base,
        "final_rating": final_rating,
        "trader_action": trader_action,
        "trader_position_sizing": position_sizing,
        "reference_price": reference,
        "research_plan": research,
        "trader_proposal": trader,
        "portfolio_decision": portfolio,
    }
    if reference is None or equity_before is None:
        return SymbolDecision(status="review", reason=NO_REFERENCE_PRICE, **fields)

    sizing = size_decision(
        final_rating, trader_action, position_sizing, current_qty, reference, equity_before
    )
    return SymbolDecision(
        status=sizing.status,
        reason=sizing.reason,
        parsed_size=sizing.parsed_size,
        target_qty=sizing.target_qty,
        order=sizing.order,
        **fields,
    )


def _read_structured(path: Path, schema: type[M]) -> M | None:
    """A structured node output, or None when the node fell back to free text."""
    if not path.exists():
        return None
    try:
        return schema.model_validate_json(path.read_text())
    except ValueError as exc:
        raise FinalizeError(f"structured output is unreadable: {path}: {exc}") from exc


def _log_decisions(
    paths: Paths, cfg: DeskConfig, date: str, states: dict[str, dict[str, Any] | None]
) -> None:
    memory = TradingMemoryLog(upstream_config(cfg, paths))
    for symbol in cfg.symbols:
        state = states[symbol]
        text = _final_text(state)
        if text:
            memory.store_decision(symbol, date, text, rating=state.get("final_rating") or "REVIEW")
