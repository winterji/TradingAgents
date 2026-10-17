"""Per-symbol AgentState store: creation, loading, and locked merge updates."""

import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from desk_runner.config import Paths
from desk_runner.state_store import StateError, init_state, load_state, update_state
from tradingagents.graph.propagation import Propagator

DATE = "2026-10-17"
SYMBOL = "AAPL"
CONTEXTS = {
    "portfolio_context": "cash 100000",
    "past_context": "past lesson",
    "instrument_context": "Apple Inc. (AAPL)",
}


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "var")


def _init(paths, symbol=SYMBOL):
    return init_state(
        paths,
        DATE,
        symbol,
        portfolio_context=CONTEXTS["portfolio_context"],
        past_context=CONTEXTS["past_context"],
        instrument_context=CONTEXTS["instrument_context"],
    )


def test_init_matches_propagator(paths):
    _init(paths)
    loaded = load_state(paths, DATE, SYMBOL)

    expected = Propagator().create_initial_state(
        SYMBOL,
        DATE,
        past_context=CONTEXTS["past_context"],
        instrument_context=CONTEXTS["instrument_context"],
        portfolio_context=CONTEXTS["portfolio_context"],
    )
    expected["messages"] = [HumanMessage(content=SYMBOL)]
    assert loaded == expected
    assert loaded["messages"][0].content == "AAPL"


def test_init_refuses_existing(paths):
    _init(paths)
    with pytest.raises(StateError):
        _init(paths)


def test_update_appends_messages_and_replaces_keys(paths):
    _init(paths)
    state = update_state(
        paths,
        DATE,
        SYMBOL,
        lambda _state: {"messages": [AIMessage("t")], "market_report": "R"},
    )

    assert len(state["messages"]) == 2
    assert state["messages"][1].content == "t"
    assert state["market_report"] == "R"
    assert load_state(paths, DATE, SYMBOL) == state


def test_concurrent_updates_not_lost(paths):
    _init(paths)
    keys = ["market_report", "sentiment_report", "news_report", "fundamentals_report"]
    errors: list[BaseException] = []

    def worker(key: str) -> None:
        def fn(_state: dict) -> dict:
            time.sleep(0.05)
            return {key: f"{key}-done"}

        try:
            update_state(paths, DATE, SYMBOL, fn)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(key,)) for key in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    final = load_state(paths, DATE, SYMBOL)
    for key in keys:
        assert final[key] == f"{key}-done"
