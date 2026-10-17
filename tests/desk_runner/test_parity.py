"""Desk runner turns reach the same final state as the upstream LangGraph pipeline.

Both runs get the same canned answers in the same order, so any difference is in how
desk_runner drives the upstream node functions. No test touches the network.
"""

from types import SimpleNamespace

import pytest

from desk_runner.config import DEFAULT_CONFIG_PATH, DeskConfig, Paths, load_config
from desk_runner.nodes import apply_answer, build_prompt, next_nodes
from desk_runner.standin import StandInChatModel
from desk_runner.state_store import init_state, load_state
from tests.desk_runner.canned import (
    FUNDAMENTALS_REPORT,
    MARKET_REPORT,
    NEWS_REPORT,
    make_canned,
    parse_with_canned,
)
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup

DATE = "2026-10-19"
SYMBOL = "AAPL"
PARITY_KEYS = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_debate_state",
    "investment_plan",
    "trader_investment_plan",
    "risk_debate_state",
    "final_trade_decision",
    "final_rating",
)
CONTEXT = {"portfolio_context": "P", "instrument_context": "I", "past_context": "L"}


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path / "var")


@pytest.fixture
def cfg(tmp_path) -> DeskConfig:
    return load_config(DEFAULT_CONFIG_PATH).model_copy(update={"var_dir": tmp_path / "var"})


@pytest.fixture(autouse=True)
def offline_sentiment(monkeypatch):
    monkeypatch.setattr(sentiment_analyst, "get_news", SimpleNamespace(func=lambda *a, **k: "NEWS"))
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "STOCKTWITS")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "REDDIT")
    monkeypatch.setattr(sentiment_analyst, "jev_screen", lambda ticker: None)


def _run_upstream(cfg: DeskConfig) -> dict:
    canned = make_canned()
    model = StandInChatModel(answer=canned, structured=parse_with_canned)
    workflow = GraphSetup(
        model,
        model,
        ConditionalLogic(cfg.max_debate_rounds, cfg.max_risk_discuss_rounds),
        cfg.max_tool_rounds,
    ).setup_graph(("market", "social", "news", "fundamentals"))
    initial = Propagator().create_initial_state(SYMBOL, DATE, **CONTEXT)
    return workflow.compile().invoke(initial, {"recursion_limit": 200})


def _run_desk(paths: Paths, cfg: DeskConfig) -> dict:
    canned = make_canned()
    init_state(paths, DATE, SYMBOL, **CONTEXT)
    while nodes := next_nodes(load_state(paths, DATE, SYMBOL), cfg):
        node = nodes[0]
        prompt = build_prompt(paths, DATE, SYMBOL, node, cfg)
        prompt.answer_path.write_text(canned(prompt.prompt_path.read_text()))
        apply_answer(paths, DATE, SYMBOL, node, cfg)
    return load_state(paths, DATE, SYMBOL)


def test_cli_path_equals_upstream_graph(paths, cfg):
    upstream = _run_upstream(cfg)
    desk = _run_desk(paths, cfg)

    assert {key: desk[key] for key in PARITY_KEYS} == {key: upstream[key] for key in PARITY_KEYS}


def test_parity_run_uses_the_expected_answers(paths, cfg):
    desk = _run_desk(paths, cfg)

    assert desk["market_report"] == MARKET_REPORT
    assert desk["news_report"] == NEWS_REPORT
    assert desk["fundamentals_report"] == FUNDAMENTALS_REPORT
    assert desk["investment_debate_state"]["count"] == 2 * cfg.max_debate_rounds
    assert desk["risk_debate_state"]["count"] == 3 * cfg.max_risk_discuss_rounds
    assert desk["final_rating"] == "Hold"
