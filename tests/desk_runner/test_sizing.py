"""Deterministic sizing: parse the Trader's position_sizing text and turn it into an order."""

import pytest

from desk_runner.sizing import ParsedSize, parse_size, size_decision


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("5% of portfolio", "pct", 5),
        ("3-5% of portfolio", "pct", 4),
        ("2.5 percent of total equity", "pct", 2.5),
        ("$2,000", "usd", 2000),
        ("$2k-3k", "usd", 2500),
        ("10 to 20 shares", "shares", 15),
        ("14 shares", "shares", 14),
    ],
)
def test_parse_variants(text, kind, value):
    assert parse_size(text) == ParsedSize(kind=kind, value=value)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "5%",  # Review Focus 3: bare percentage has no basis
        "5",  # Review Focus 3: bare number
        "half a standard allocation",
        "25% of the position",
        "5% of portfolio, or $3000",
    ],
)
def test_parse_rejects(text):
    assert parse_size(text) is None


def test_overweight_buy_opens_long():
    result = size_decision(
        final_rating="Overweight",
        trader_action="Buy",
        position_sizing="5% of portfolio",
        current_qty=0,
        price=343.39,
        equity=100000,
    )
    assert result.status == "order"
    assert result.target_qty == 14
    assert result.order is not None
    assert (result.order.side, result.order.qty) == ("buy", 14)
    assert result.order.type == "market"
    assert result.order.time_in_force == "opg"


def test_sell_crosses_to_short():
    result = size_decision(
        final_rating="Sell",
        trader_action="Sell",
        position_sizing="10% of portfolio",
        current_qty=14,
        price=343.39,
        equity=100000,
    )
    assert result.status == "order"
    assert result.target_qty == -29
    assert result.order is not None
    assert (result.order.side, result.order.qty) == ("sell", 43)


def test_hold_keeps_position():
    result = size_decision(
        final_rating="Hold",
        trader_action="Buy",
        position_sizing="5% of portfolio",
        current_qty=14,
        price=343.39,
        equity=100000,
    )
    assert result.status == "no_order"
    assert result.order is None


def test_trader_hold_no_order():
    result = size_decision(
        final_rating="Buy",
        trader_action="Hold",
        position_sizing="5% of portfolio",
        current_qty=0,
        price=343.39,
        equity=100000,
    )
    assert result.status == "no_order"
    assert result.order is None


def test_disagreement_review():
    result = size_decision(
        final_rating="Buy",
        trader_action="Sell",
        position_sizing="5% of portfolio",
        current_qty=0,
        price=343.39,
        equity=100000,
    )
    assert result.status == "review"
    assert result.order is None


def test_review_rating():
    result = size_decision(
        final_rating="REVIEW",
        trader_action="Buy",
        position_sizing="5% of portfolio",
        current_qty=0,
        price=343.39,
        equity=100000,
    )
    assert result.status == "review"
    assert result.order is None


def test_freetext_trader_review():
    result = size_decision(
        final_rating="Buy",
        trader_action=None,
        position_sizing="5% of portfolio",
        current_qty=0,
        price=343.39,
        equity=100000,
    )
    assert result.status == "review"
    assert result.order is None


def test_target_equals_current_no_order():
    result = size_decision(
        final_rating="Overweight",
        trader_action="Buy",
        position_sizing="5% of portfolio",
        current_qty=14,
        price=343.39,
        equity=100000,
    )
    assert result.status == "no_order"
    assert result.target_qty == 14
    assert result.order is None


def test_unreadable_size_review():
    result = size_decision(
        final_rating="Buy",
        trader_action="Buy",
        position_sizing="half a standard allocation",
        current_qty=0,
        price=343.39,
        equity=100000,
    )
    assert result.status == "review"
    assert result.parsed_size is None
    assert result.order is None


def test_unknown_rating_review():
    result = size_decision(
        final_rating="Strong Buy",
        trader_action="Buy",
        position_sizing="5% of portfolio",
        current_qty=0,
        price=343.39,
        equity=100000,
    )
    assert result.status == "review"
    assert result.order is None


@pytest.mark.parametrize(("price", "equity"), [(0, 100000), (-1.0, 100000), (343.39, 0)])
def test_non_positive_price_or_equity_review(price, equity):
    result = size_decision(
        final_rating="Buy",
        trader_action="Buy",
        position_sizing="5% of portfolio",
        current_qty=0,
        price=price,
        equity=equity,
    )
    assert result.status == "review"
    assert result.order is None


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("1,000 shares", "shares", 1000),
        ("2,500 shares", "shares", 2500),
        ("3% to 5% of portfolio", "pct", 4),
        ("3%-5% of portfolio", "pct", 4),
    ],
)
def test_parse_regressions(text, kind, value):
    assert parse_size(text) == ParsedSize(kind=kind, value=value)


@pytest.mark.parametrize(
    "text",
    [
        "Stop at $325",
        "Scale in near $343 entry",
        "Risk 1% of portfolio on this trade",
        "2% of portfolio at risk",
        ".5 of portfolio",
        ".5% of portfolio",
    ],
)
def test_parse_rejects_regressions(text):
    assert parse_size(text) is None


def test_parse_keeps_size_next_to_price_level():
    assert parse_size("Buy $2,000 of stock, stop at $325") == ParsedSize(kind="usd", value=2000)


def test_zero_share_size_is_review_not_sell():
    result = size_decision(
        final_rating="Buy",
        trader_action="Buy",
        position_sizing="$100",
        current_qty=10,
        price=343.39,
        equity=100000,
    )
    assert result.status == "review"
    assert result.reason == "size rounds to zero shares"
    assert result.order is None
