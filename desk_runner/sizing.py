"""Deterministic sizing: the Trader's free-text position size and the PM rating become an order.

Pure logic, no I/O. Shorting is allowed and there are no exposure limits. Anything unclear
(unreadable rating or size, disagreeing Trader and PM, free-text Trader) is ``review``: no order.
"""

import math
import re
from typing import Literal

from pydantic import BaseModel, Field

SizeKind = Literal["pct", "usd", "shares"]

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_RANGE_SEP = r"\s*(?:-|–|to)\s*"
_BASIS = r"\s+of\s+(?:the\s+)?(?:total\s+)?(?:portfolio|equity|capital|account)\b"

# Each pattern captures (low, high); the high group is None for a single value.
_PCT_RE = re.compile(
    rf"(?P<lo>\d+(?:\.\d+)?)(?:{_RANGE_SEP}(?P<hi>\d+(?:\.\d+)?))?\s*(?:%|percent){_BASIS}",
    re.IGNORECASE,
)
_USD_RE = re.compile(
    rf"\$\s?(?P<lo>{_NUMBER})(?P<lo_k>k)?(?:{_RANGE_SEP}\$?\s?(?P<hi>{_NUMBER})(?P<hi_k>k)?)?",
    re.IGNORECASE,
)
_SHARES_RE = re.compile(
    rf"(?P<lo>\d+(?:\.\d+)?)(?:{_RANGE_SEP}(?P<hi>\d+(?:\.\d+)?))?\s+shares?\b",
    re.IGNORECASE,
)


class ParsedSize(BaseModel):
    kind: SizeKind
    value: float


class Order(BaseModel):
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0)
    type: Literal["market"] = "market"
    time_in_force: Literal["opg"] = "opg"


class SizingResult(BaseModel):
    status: Literal["order", "no_order", "review"]
    reason: str
    parsed_size: ParsedSize | None = None
    target_qty: int | None = None
    order: Order | None = None


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def _midpoint(lo: float, hi: float | None) -> float:
    return lo if hi is None else (lo + hi) / 2


def _usd_value(match: re.Match[str]) -> float:
    lo = _number(match.group("lo")) * (1000 if match.group("lo_k") else 1)
    if match.group("hi") is None:
        return lo
    hi = _number(match.group("hi")) * (1000 if match.group("hi_k") else 1)
    return _midpoint(lo, hi)


def _candidates(text: str) -> list[ParsedSize]:
    found: list[ParsedSize] = []
    for match in _PCT_RE.finditer(text):
        hi = match.group("hi")
        found.append(
            ParsedSize(
                kind="pct",
                value=_midpoint(float(match.group("lo")), None if hi is None else float(hi)),
            )
        )
    for match in _USD_RE.finditer(text):
        found.append(ParsedSize(kind="usd", value=_usd_value(match)))
    for match in _SHARES_RE.finditer(text):
        hi = match.group("hi")
        found.append(
            ParsedSize(
                kind="shares",
                value=_midpoint(float(match.group("lo")), None if hi is None else float(hi)),
            )
        )
    return found


def parse_size(text: str | None) -> ParsedSize | None:
    """Return the single size the text states, or None if it is missing, unclear or ambiguous."""
    if not text:
        return None
    distinct = {(c.kind, c.value) for c in _candidates(text)}
    if len(distinct) != 1:
        return None
    kind, value = distinct.pop()
    return ParsedSize(kind=kind, value=value)


_RATING_DIRECTION: dict[str, int] = {
    "buy": 1,
    "overweight": 1,
    "hold": 0,
    "underweight": -1,
    "sell": -1,
}
_ACTION_DIRECTION: dict[str, int] = {"buy": 1, "hold": 0, "sell": -1}


def _review(reason: str, parsed: ParsedSize | None = None) -> SizingResult:
    return SizingResult(status="review", reason=reason, parsed_size=parsed)


def _shares(size: ParsedSize, equity: float, price: float) -> int:
    if size.kind == "pct":
        return math.trunc(size.value / 100 * equity / price)
    if size.kind == "usd":
        return math.trunc(size.value / price)
    return math.trunc(size.value)


def size_decision(
    final_rating: str,
    trader_action: str | None,
    position_sizing: str | None,
    current_qty: int,
    price: float,
    equity: float,
) -> SizingResult:
    """Turn the PM rating, the Trader action and the Trader's sizing text into an order."""
    rating_key = final_rating.strip().lower()
    if rating_key == "review" or rating_key not in _RATING_DIRECTION:
        return _review(f"unreadable final rating {final_rating!r}")
    rating = _RATING_DIRECTION[rating_key]

    if trader_action is not None and trader_action.strip().lower() not in _ACTION_DIRECTION:
        return _review(f"unreadable trader action {trader_action!r}")
    action = None if trader_action is None else _ACTION_DIRECTION[trader_action.strip().lower()]

    if action == 0 or rating == 0:
        return SizingResult(status="no_order", reason="hold: current position kept")
    if action is not None and action != rating:
        return _review("trader action and final rating point in opposite directions")
    if action is None:
        return _review("trader wrote free text, no structured action")

    size = parse_size(position_sizing)
    if size is None:
        return _review("position size missing or not a readable % of portfolio, $ or shares")
    if price <= 0 or equity <= 0:
        return _review("price and equity must be positive to size the order", size)

    target = rating * _shares(size, equity, price)
    order_qty = target - current_qty
    if order_qty == 0:
        return SizingResult(
            status="no_order",
            reason="target equals current position",
            parsed_size=size,
            target_qty=target,
        )
    side: Literal["buy", "sell"] = "buy" if order_qty > 0 else "sell"
    return SizingResult(
        status="order",
        reason=f"move position from {current_qty} to {target} shares",
        parsed_size=size,
        target_qty=target,
        order=Order(side=side, qty=abs(order_qty)),
    )
