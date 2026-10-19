"""Canned answers for upstream agent nodes, shared by offline tests.

``make_canned()`` returns a free-text answer callable with its own per-role turn
counters, so two runs that make the same calls in the same order see identical
answers. ``parse_with_canned`` answers structured calls with the fixed model for
the schema. A structured role's free-text answer is that same model as JSON.
"""

from collections import Counter
from collections.abc import Callable, Sequence

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from desk_runner.standin import render_messages
from tradingagents.agents.schemas import (
    PortfolioDecision,
    ResearchPlan,
    SentimentReport,
    TraderProposal,
)

MARKET_REPORT = "MARKET REPORT"
NEWS_REPORT = "NEWS REPORT"
FUNDAMENTALS_REPORT = "FUNDAMENTALS REPORT"

RESEARCH_JSON = {"recommendation": "Hold", "rationale": "r", "strategic_actions": "s"}
TRADER_JSON = {"action": "Buy", "reasoning": "r", "position_sizing": "5% of portfolio"}
PM_JSON = {"rating": "Hold", "executive_summary": "e", "investment_thesis": "t"}
SENTIMENT_JSON = {"overall_band": "Neutral", "overall_score": 5, "confidence": "low", "narrative": "n"}

FIXED_MODELS: dict[type[BaseModel], BaseModel] = {
    ResearchPlan: ResearchPlan.model_validate(RESEARCH_JSON),
    TraderProposal: TraderProposal.model_validate(TRADER_JSON),
    PortfolioDecision: PortfolioDecision.model_validate(PM_JSON),
    SentimentReport: SentimentReport.model_validate(SENTIMENT_JSON),
}

# Role markers from the upstream prompts. Checked in order; each marker appears only in its role's prompt.
_STRUCTURED_ROLES: tuple[tuple[str, type[BaseModel]], ...] = (
    ("As the Research Manager", ResearchPlan),
    ("You are a trading agent analyzing", TraderProposal),
    ("As the Portfolio Manager", PortfolioDecision),
    ("You are a financial market sentiment analyst", SentimentReport),
)
_FIXED_REPORTS: tuple[tuple[str, str], ...] = (
    ("You are a trading assistant tasked", MARKET_REPORT),
    ("You are a news researcher", NEWS_REPORT),
    ("You are a researcher tasked with analyzing fundamental", FUNDAMENTALS_REPORT),
)
_COUNTED_ROLES: tuple[tuple[str, str], ...] = (
    ("You are a Bull Analyst", "bull"),
    ("You are a Bear Analyst", "bear"),
    ("As the Aggressive Risk Analyst", "aggressive"),
    ("As the Conservative Risk Analyst", "conservative"),
    ("As the Neutral Risk Analyst", "neutral"),
)


class UnknownRoleError(Exception):
    """The prompt matches no known role marker."""


def make_canned() -> Callable[[Sequence[BaseMessage] | str], str]:
    """A fresh free-text answer callable; its turn counters start at zero."""
    counts: Counter[str] = Counter()

    def canned(messages: Sequence[BaseMessage] | str) -> str:
        text = messages if isinstance(messages, str) else render_messages(messages)
        for marker, schema in _STRUCTURED_ROLES:
            if marker in text:
                return FIXED_MODELS[schema].model_dump_json()
        for marker, report in _FIXED_REPORTS:
            if marker in text:
                return report
        for marker, role in _COUNTED_ROLES:
            if marker in text:
                counts[role] += 1
                return f"{role} turn {counts[role]}"
        raise UnknownRoleError(f"no canned answer for prompt starting: {text[:80]!r}")

    return canned


def parse_with_canned(_messages: list[BaseMessage], schema: type[BaseModel]) -> BaseModel:
    """The structured answer for ``schema``; matches the JSON ``make_canned`` gives for that role."""
    try:
        return FIXED_MODELS[schema]
    except KeyError as exc:
        raise UnknownRoleError(f"no canned model for {schema.__name__}") from exc

