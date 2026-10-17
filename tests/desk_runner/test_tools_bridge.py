"""The ``ma tool`` bridge: upstream tools called with injected state, no network."""

import json

import pytest

from desk_runner.config import DEFAULT_CONFIG_PATH, DeskConfig, Paths, load_config
from desk_runner.state_store import init_state
from desk_runner.tools_bridge import ToolBridgeError, run_tool
from tradingagents.agents import tools as upstream_tools
from tradingagents.agents.analysts.turn import WRAP_UP

DATE = "2026-10-17"
SYMBOL = "AAPL"


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "var")


@pytest.fixture
def cfg(tmp_path) -> DeskConfig:
    return load_config(DEFAULT_CONFIG_PATH).model_copy(update={"var_dir": tmp_path / "var"})


@pytest.fixture
def vendor(monkeypatch):
    calls = []

    def fake_route(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return "VENDOR OUTPUT"

    monkeypatch.setattr(upstream_tools, "route_to_vendor", fake_route)
    return calls


@pytest.fixture
def state(paths):
    init_state(
        paths, DATE, SYMBOL, portfolio_context="cash", past_context="", instrument_context="Apple"
    )


def test_injects_symbol_and_trade_date(paths, cfg, vendor, state):
    result = run_tool(
        paths,
        DATE,
        SYMBOL,
        "market",
        "get_stock_data",
        {"start_date": "2026-09-01", "end_date": "2026-10-19"},
        cfg,
    )

    assert result == "VENDOR OUTPUT"
    method, args, _kwargs = vendor[0]
    assert method == "get_stock_data"
    assert args == (SYMBOL, "2026-09-01", DATE)


def test_tool_not_allowed_for_analyst(paths, cfg, vendor, state):
    with pytest.raises(ToolBridgeError):
        run_tool(
            paths,
            DATE,
            SYMBOL,
            "market",
            "get_news",
            {"start_date": "2026-09-01", "end_date": "2026-10-01"},
            cfg,
        )
    assert vendor == []


def test_wrap_up_after_max_rounds(paths, cfg, vendor, state):
    cfg = cfg.model_copy(update={"max_tool_rounds": 2})
    args = {"start_date": "2026-09-01", "end_date": "2026-10-01"}

    run_tool(paths, DATE, SYMBOL, "market", "get_stock_data", args, cfg)
    run_tool(paths, DATE, SYMBOL, "market", "get_stock_data", args, cfg)
    result = run_tool(paths, DATE, SYMBOL, "market", "get_stock_data", args, cfg)

    assert result == WRAP_UP
    assert len(vendor) == 2


def test_model_cannot_override_injected(paths, cfg, vendor, state):
    with pytest.raises(ToolBridgeError, match="injected argument"):
        run_tool(
            paths,
            DATE,
            SYMBOL,
            "market",
            "get_stock_data",
            {"symbol": "MSFT", "start_date": "2026-09-01", "end_date": "2026-10-01"},
            cfg,
        )
    assert vendor == []


def test_calls_are_logged(paths, cfg, vendor, state):
    args = {"start_date": "2026-09-01", "end_date": "2026-10-01"}

    run_tool(paths, DATE, SYMBOL, "market", "get_stock_data", args, cfg)

    log = paths.symbol_dir(DATE, SYMBOL) / "tool_calls.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["analyst"] == "market"
    assert entries[0]["tool"] == "get_stock_data"
    assert entries[0]["args"] == args
    assert "ts" in entries[0]


def test_missing_required_argument_is_bridge_error(paths, cfg, vendor, state):
    with pytest.raises(ToolBridgeError):
        run_tool(paths, DATE, SYMBOL, "market", "get_stock_data", {"start_date": "2026-09-01"}, cfg)
    assert vendor == []


def test_missing_state_is_bridge_error(paths, cfg, vendor):
    with pytest.raises(ToolBridgeError):
        run_tool(
            paths,
            DATE,
            SYMBOL,
            "market",
            "get_stock_data",
            {"start_date": "2026-09-01", "end_date": "2026-10-01"},
            cfg,
        )
    assert vendor == []
