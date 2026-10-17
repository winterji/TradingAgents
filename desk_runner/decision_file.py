"""The day's decision file (spec section 7): one record per symbol, validated before it is written."""

import secrets
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from desk_runner.config import ModelsConfig
from desk_runner.sizing import Order, ParsedSize
from tradingagents.agents.schemas import PortfolioDecision, ResearchPlan, TraderProposal
from tradingagents.portfolio import PortfolioContext

UPSTREAM_COMMIT = "e8f5503"
RUN_ID_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class Rounds(BaseModel):
    debate: int
    risk: int


class SymbolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    status: Literal["order", "no_order", "review"]
    reason: str
    final_rating: str | None = None
    trader_action: str | None = None
    trader_position_sizing: str | None = None
    parsed_size: ParsedSize | None = None
    reference_price: float | None = None
    current_qty: int
    target_qty: int | None = None
    order: Order | None = None
    research_plan: ResearchPlan | None = None
    trader_proposal: TraderProposal | None = None
    portfolio_decision: PortfolioDecision | None = None
    state_file: str

    @model_validator(mode="after")
    def _order_iff_status_order(self) -> "SymbolDecision":
        if (self.order is not None) != (self.status == "order"):
            raise ValueError("order is set exactly when status is 'order'")
        return self


class DecisionFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    trade_date: str
    generated_at: str
    upstream_commit: Literal["e8f5503"] = UPSTREAM_COMMIT
    models: ModelsConfig
    rounds: Rounds
    book_before: PortfolioContext
    # None when a held symbol has no reference price: equity is then unknown, never guessed.
    equity_before: float | None
    decisions: list[SymbolDecision] = Field(default_factory=list)


def new_run_id(now: datetime) -> str:
    """``<UTC time>-<6 hex>``, e.g. ``2026-10-19T12:00:00Z-ab12cd``."""
    return f"{now.astimezone(UTC).strftime(RUN_ID_TIME_FORMAT)}-{secrets.token_hex(3)}"
