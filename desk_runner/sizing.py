"""Deterministic sizing: the Trader's free-text position size and the PM rating become an order.

Pure logic, no I/O. Shorting is allowed and there are no exposure limits. Anything unclear
(unreadable rating or size, disagreeing Trader and PM, free-text Trader) is ``review``: no order.
"""

import math
import re
from typing import Literal

from pydantic import BaseModel, Field

SizeKind = Literal["pct", "usd", "shares"]

# Numbers: "1,000", "2500", "2.5". The left guard stops matches inside ".5" or "1,5".
_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_LEFT = r"(?<![\d.,])"
_SEP = r"\s*(?:-|–|to)\s*"
_BASIS = r"\s+of\s+(?:the\s+)?(?:total\s+)?(?:portfolio|equity|capital|account)\b"

# Each pattern captures (lo, hi); hi is None for a single value.
_PCT_RE = re.compile(
    rf"{_LEFT}(?P<lo>{_NUM})(?:(?:\s*(?:%|percent))?{_SEP}(?P<hi>{_NUM}))?"
    rf"\s*(?:%|percent){_BASIS}",
    re.IGNORECASE,
)
# Optional magnitude after a $ amount: "k", "m", "b"/"bn" (a billion, always review), or the words.
_USD_UNIT = r"(?:\s*(?P<{}>thousand|million|billion|bn|b|m|k)(?![a-z]))?"
_USD_RE = re.compile(
    rf"\$\s?(?P<lo>{_NUM})"
    + _USD_UNIT.format("lo_u")
    + rf"(?:{_SEP}\$?\s?(?P<hi>{_NUM})"
    + _USD_UNIT.format("hi_u")
    + r")?",
    re.IGNORECASE,
)
_SHARES_RE = re.compile(
    rf"{_LEFT}(?P<lo>{_NUM})(?:{_SEP}(?P<hi>{_NUM}))?\s+shares?\b",
    re.IGNORECASE,
)

# A clause ends at ";", ":", parentheses, a newline, or a "." or "," not followed by a digit.
_CLAUSE_BREAK = re.compile(r"[.,](?!\d)|[;:()\n]")
_RISK_RE = re.compile(r"\b(?:risks?|loss(?:es)?|drawdown)\b", re.IGNORECASE)
# Words that describe a change to an existing position, not a size of a new one. Any of them is review.
_CHANGE_RE = re.compile(
    r"\b(?:add|additional|increase|trim|reduce|cut|exit|close|entire|full|all|remaining"
    r"|half|quarter|third)\b",
    re.IGNORECASE,
)
_MULTIPLIERS: dict[str, float] = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
}
_BILLION_UNITS = frozenset({"b", "bn", "billion"})
_PRICE_WORDS = frozenset(
    {"stop", "stop-loss", "target", "entry", "at", "near", "below", "above", "price"}
)
_PRICE_TAIL_RE = re.compile(r"\s*(?:entry|stop|target)\b", re.IGNORECASE)


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


def _usd_value(match: re.Match[str]) -> float | None:
    """The dollar amount a ``$`` match states, or None when it is a billion or an ambiguous range."""
    lo_unit = (match.group("lo_u") or "").lower() or None
    hi_unit = (match.group("hi_u") or "").lower() or None
    if lo_unit in _BILLION_UNITS or hi_unit in _BILLION_UNITS:
        return None
    lo_raw = _number(match.group("lo"))
    if match.group("hi") is None:
        return lo_raw * _MULTIPLIERS.get(lo_unit or "", 1)

    hi_raw = _number(match.group("hi"))
    lo_mult = _MULTIPLIERS.get(lo_unit or "")
    hi_mult = _MULTIPLIERS.get(hi_unit or "")
    if lo_mult and hi_mult and lo_mult != hi_mult:
        return None
    # A unit on one end applies to the other end too, unless that end is already written in
    # full (1,000 or more, e.g. "$2,000-3k"). Two ends with no unit are plain dollars.
    shared = lo_mult or hi_mult or 1.0
    lo = lo_raw * (lo_mult or (shared if lo_raw < 1000 else 1.0))
    hi = hi_raw * (hi_mult or (shared if hi_raw < 1000 else 1.0))
    if lo > hi:
        return None
    return _midpoint(lo, hi)


def _clause_bounds(text: str, pos: int) -> tuple[int, int]:
    """Return the (start, end) of the clause that contains position ``pos``."""
    start = 0
    for brk in _CLAUSE_BREAK.finditer(text, 0, pos):
        start = brk.end()
    end = len(text)
    brk = _CLAUSE_BREAK.search(text, pos)
    if brk is not None:
        end = brk.start()
    return start, end


def _is_price_level(text: str, match: re.Match[str]) -> bool:
    """True if a ``$N`` reads as a price (stop, entry, target), not as a position size."""
    start, _ = _clause_bounds(text, match.start())
    words = re.findall(r"[A-Za-z][A-Za-z-]*", text[start : match.start()])[-3:]
    if any(word.lower() in _PRICE_WORDS for word in words):
        return True
    return _PRICE_TAIL_RE.match(text, match.end()) is not None


def _has_risk_budget(text: str) -> bool:
    """True if any size in the text shares its clause with risk or loss wording (a risk budget)."""
    for match in (*_PCT_RE.finditer(text), *_USD_RE.finditer(text), *_SHARES_RE.finditer(text)):
        start, end = _clause_bounds(text, match.start())
        if _RISK_RE.search(text[start:end]):
            return True
    return False


def _candidates(text: str) -> list[ParsedSize] | None:
    """Every size the text states, or None when one of them is a billion or an ambiguous range."""
    found: list[ParsedSize] = []
    for match in _PCT_RE.finditer(text):
        hi = match.group("hi")
        found.append(
            ParsedSize(
                kind="pct",
                value=_midpoint(_number(match.group("lo")), None if hi is None else _number(hi)),
            )
        )
    for match in _USD_RE.finditer(text):
        if _is_price_level(text, match):
            continue
        value = _usd_value(match)
        if value is None:
            return None
        found.append(ParsedSize(kind="usd", value=value))
    for match in _SHARES_RE.finditer(text):
        hi = match.group("hi")
        found.append(
            ParsedSize(
                kind="shares",
                value=_midpoint(_number(match.group("lo")), None if hi is None else _number(hi)),
            )
        )
    return found


def parse_size(text: str | None) -> ParsedSize | None:
    """Return the single size the text states, or None if it is missing, unclear or ambiguous."""
    if not text or _has_risk_budget(text) or _CHANGE_RE.search(text):
        return None
    candidates = _candidates(text)
    if candidates is None:
        return None
    distinct = {(c.kind, c.value) for c in candidates}
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

    shares = _shares(size, equity, price)
    if shares == 0:
        return _review("size rounds to zero shares", size)

    target = rating * shares
    order_qty = target - current_qty
    if order_qty == 0:
        return SizingResult(
            status="no_order",
            reason="target equals current position",
            parsed_size=size,
            target_qty=target,
        )
    side: Literal["buy", "sell"] = "buy" if order_qty > 0 else "sell"
    if (1 if side == "buy" else -1) != rating:
        return _review("order side opposes rating", size)
    return SizingResult(
        status="order",
        reason=f"move position from {current_qty} to {target} shares",
        parsed_size=size,
        target_qty=target,
        order=Order(side=side, qty=abs(order_qty)),
    )
