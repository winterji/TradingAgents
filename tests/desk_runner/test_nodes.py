"""Upstream nodes as desk runner turns: next nodes, captured prompts, and applied answers.

No test touches the network: the sentiment fetchers and the TypeSafe screen are replaced.
"""

import json
from types import SimpleNamespace

import pytest

from desk_runner.config import DEFAULT_CONFIG_PATH, DeskConfig, Paths, load_config
from desk_runner.nodes import NodeError, apply_answer, build_prompt, next_nodes
from desk_runner.state_store import init_state, load_state, update_state
from tradingagents.agents.analysts import sentiment_analyst

DATE = "2026-10-17"
SYMBOL = "AAPL"
REPORTS = {
    "market_report": "MARKET REPORT",
    "sentiment_report": "SENTIMENT REPORT",
    "news_report": "NEWS REPORT",
    "fundamentals_report": "FUNDAMENTALS REPORT",
}
RESEARCH_JSON = {"recommendation": "Hold", "rationale": "r", "strategic_actions": "s"}
TRADER_JSON = {"action": "Buy", "reasoning": "r", "position_sizing": "5% of portfolio"}
PM_JSON = {"rating": "Hold", "executive_summary": "e", "investment_thesis": "t"}
SENTIMENT_JSON = {"overall_band": "Neutral", "overall_score": 5, "confidence": "low", "narrative": "n"}


@pytest.fixture
def paths(tmp_path):
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


def _state(paths, *, reports=True, **changes):
    init_state(
        paths,
        DATE,
        SYMBOL,
        portfolio_context="cash 100000",
        past_context="",
        instrument_context="Apple Inc. (AAPL)",
    )
    base = dict(REPORTS) if reports else {}
    return update_state(paths, DATE, SYMBOL, lambda _state: {**base, **changes})


def _turn(paths, cfg, node, answer, freetext=False):
    prompt = build_prompt(paths, DATE, SYMBOL, node, cfg)
    prompt.answer_path.write_text(answer)
    return apply_answer(paths, DATE, SYMBOL, node, cfg, freetext=freetext)


def _canned_answer(node):
    if node in ("bull", "bear"):
        return "case"
    if node == "research_manager":
        return json.dumps(RESEARCH_JSON)
    if node == "trader":
        return json.dumps(TRADER_JSON)
    if node == "portfolio_manager":
        return json.dumps(PM_JSON)
    return "view"


def test_next_sequence_3_rounds(paths, cfg):
    state = _state(paths)
    order = []
    while nodes := next_nodes(state, cfg):
        assert len(nodes) == 1
        order.append(nodes[0])
        state = _turn(paths, cfg, nodes[0], _canned_answer(nodes[0]))

    assert order == (
        ["bull", "bear"] * 3
        + ["research_manager", "trader"]
        + ["aggressive", "conservative", "neutral"] * 3
        + ["portfolio_manager"]
    )
    assert next_nodes(state, cfg) == []


def test_next_returns_all_missing_analysts(paths, cfg):
    state = _state(paths, reports=False)
    assert next_nodes(state, cfg) == ["market", "social", "news", "fundamentals"]

    state = _turn(paths, cfg, "market", "market text")

    assert next_nodes(state, cfg) == ["social", "news", "fundamentals"]


def test_prompt_is_upstream_prompt(paths, cfg):
    _state(paths)

    prompt = build_prompt(paths, DATE, SYMBOL, "bull", cfg)

    text = prompt.prompt_path.read_text()
    assert "You are a Bull Analyst advocating for investing" in text
    assert "MARKET REPORT" in text
    assert prompt.model == "sonnet"


def test_structured_prompt_has_schema(paths, cfg):
    _state(paths, investment_plan="plan")

    prompt = build_prompt(paths, DATE, SYMBOL, "trader", cfg)

    text = prompt.prompt_path.read_text()
    assert "## Output format" in text
    assert "position_sizing" in text


def test_meta_records_deep_tier_model(paths, cfg):
    debate = {
        "bull_history": "",
        "bear_history": "",
        "history": "Bull Analyst: a\nBear Analyst: b",
        "current_response": "Bear Analyst: b",
        "count": 6,
    }
    _state(paths, investment_debate_state=debate)

    prompt = build_prompt(paths, DATE, SYMBOL, "research_manager", cfg)

    meta = json.loads(prompt.prompt_path.with_name(prompt.prompt_path.name.replace(".prompt.md", ".meta.json")).read_text())
    assert meta["model"] == "opus"
    assert meta["tier"] == "deep"
    assert meta["answer_file"] == prompt.answer_path.name


def test_apply_debate_updates_state(paths, cfg):
    _state(paths)

    state = _turn(paths, cfg, "bull", "case")

    assert state["investment_debate_state"]["current_response"] == "Bull Analyst: case"
    assert state["investment_debate_state"]["count"] == 1


def test_apply_structured_fenced_json(paths, cfg):
    _state(paths, investment_plan="plan")
    answer = "Here is my proposal:\n```json\n" + json.dumps(TRADER_JSON) + "\n```\nThanks."

    state = _turn(paths, cfg, "trader", answer)

    assert "FINAL TRANSACTION PROPOSAL: **BUY**" in state["trader_investment_plan"]
    assert (paths.symbol_dir(DATE, SYMBOL) / "structured" / "trader.json").exists()


def test_apply_structured_invalid_raises_then_freetext(paths, cfg):
    _state(paths, investment_plan="plan")
    state_file = paths.state_file(DATE, SYMBOL)
    prompt = build_prompt(paths, DATE, SYMBOL, "trader", cfg)
    prompt.answer_path.write_text("no json here")
    before = state_file.read_bytes()

    with pytest.raises(NodeError):
        apply_answer(paths, DATE, SYMBOL, "trader", cfg)
    assert state_file.read_bytes() == before

    state = apply_answer(paths, DATE, SYMBOL, "trader", cfg, freetext=True)

    assert state["trader_investment_plan"] == "no json here"
    assert not (paths.symbol_dir(DATE, SYMBOL) / "structured" / "trader.json").exists()


def test_pm_freetext_rating(paths, cfg):
    _state(
        paths,
        investment_plan="plan",
        trader_investment_plan="trade",
        risk_debate_state={
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "history": "risk debate",
            "latest_speaker": "Neutral Analyst",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 9,
        },
    )

    state = _turn(paths, cfg, "portfolio_manager", "**Rating**: Sell\nExit the position.", freetext=True)

    assert state["final_rating"] == "Sell"


def test_wrong_node_refused(paths, cfg):
    _state(paths, reports=False)
    state_file = paths.state_file(DATE, SYMBOL)
    before = state_file.read_bytes()

    with pytest.raises(NodeError):
        build_prompt(paths, DATE, SYMBOL, "trader", cfg)

    assert state_file.read_bytes() == before
    assert not paths.symbol_dir(DATE, SYMBOL).joinpath("turns").exists()


def test_apply_without_prompt_refused(paths, cfg):
    _state(paths)

    with pytest.raises(NodeError):
        apply_answer(paths, DATE, SYMBOL, "bull", cfg)


def test_prompt_twice_refused(paths, cfg):
    _state(paths)
    build_prompt(paths, DATE, SYMBOL, "bull", cfg)

    with pytest.raises(NodeError):
        build_prompt(paths, DATE, SYMBOL, "bull", cfg)


def test_apply_twice_refused(paths, cfg):
    _state(paths)
    _turn(paths, cfg, "bull", "case")
    before = paths.state_file(DATE, SYMBOL).read_bytes()

    with pytest.raises(NodeError):
        apply_answer(paths, DATE, SYMBOL, "bull", cfg)

    assert paths.state_file(DATE, SYMBOL).read_bytes() == before


def test_sentiment_apply_reuses_cached_inputs(paths, cfg, monkeypatch):
    _state(paths, reports=False)
    prompt = build_prompt(paths, DATE, SYMBOL, "social", cfg)
    assert "STOCKTWITS" in prompt.prompt_path.read_text()

    def unavailable(*args, **kwargs):
        raise AssertionError("apply must not fetch sentiment sources")

    monkeypatch.setattr(sentiment_analyst, "get_news", SimpleNamespace(func=unavailable))
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", unavailable)
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", unavailable)
    prompt.answer_path.write_text(json.dumps(SENTIMENT_JSON))

    state = apply_answer(paths, DATE, SYMBOL, "social", cfg)

    assert state["sentiment_report"].startswith("**Overall Sentiment:**")


def test_analyst_merges_only_report(paths, cfg):
    _state(paths, reports=False)
    before = load_state(paths, DATE, SYMBOL)

    state = _turn(paths, cfg, "market", "market text")

    assert state["market_report"] == "market text"
    assert len(state["messages"]) == len(before["messages"])
