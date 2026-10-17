"""End-to-end dry run of the ``ma`` CLI over two trading days, offline.

Every external dependency is stubbed: the NYSE calendar, the vendor prices, the company
identity lookup and the sentiment sources. Sub-agent answers come from the canned
answers, except that the Portfolio Manager says Overweight so that the Trader's Buy
produces orders (the canned Hold would size to nothing).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from desk_runner import market
from desk_runner.book import FillReport
from desk_runner.cli import app
from desk_runner.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, Paths
from desk_runner.decision_file import DecisionFile
from tests.desk_runner.canned import make_canned
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.agents.schemas import PortfolioDecision
from tradingagents.portfolio import PortfolioContext

DAY1 = "2026-10-19"
DAY2 = "2026-10-20"
SYMBOLS = ["AAPL", "GOOGL", "AMZN"]
OPEN_PRICES = {"AAPL": 100.0, "GOOGL": 50.0, "AMZN": 25.0}
CLOSE_PRICES = {"AAPL": 99.0, "GOOGL": 49.0, "AMZN": 24.0}
PM_OVERWEIGHT = PortfolioDecision(
    rating="Overweight", executive_summary="e", investment_thesis="t"
).model_dump_json()

runner = CliRunner()


@pytest.fixture
def var_dir(tmp_path, monkeypatch) -> Path:
    """Point the CLI at a temporary config whose runtime files live under tmp_path."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["var_dir"] = str(tmp_path / "var")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config_path))
    return tmp_path / "var"


@pytest.fixture
def offline(monkeypatch):
    """Stub the calendar, prices, identity and sentiment sources; trading days are all days."""
    monkeypatch.setattr(market, "is_trading_day", lambda day: True)
    monkeypatch.setattr(market, "last_close", lambda symbol, day: CLOSE_PRICES[symbol])
    monkeypatch.setattr(market, "open_on", lambda symbol, day: OPEN_PRICES[symbol])
    monkeypatch.setattr(
        "desk_runner.cli.resolve_instrument_identity",
        lambda ticker: {"company_name": f"{ticker} Test Co"},
    )
    monkeypatch.setattr(sentiment_analyst, "get_news", SimpleNamespace(func=lambda *a, **k: "NEWS"))
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "STOCKTWITS")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "REDDIT")
    monkeypatch.setattr(sentiment_analyst, "jev_screen", lambda ticker: None)


def _cli(*args: str, date: str | None = None) -> str:
    argv = list(args) + (["--date", date] if date else [])
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    return result.stdout


def _json(*args: str, date: str | None = None) -> dict:
    return json.loads(_cli(*args, date=date))


def _answer(canned, prompt_text: str) -> str:
    if "As the Portfolio Manager" in prompt_text:
        return PM_OVERWEIGHT
    return canned(prompt_text)


def _run_symbol_pipeline(canned, symbol: str, date: str) -> None:
    """Drive one symbol's upstream nodes to completion through ma next, prompt, apply."""
    while True:
        nxt = _json("next", symbol, date=date)
        if nxt["done"]:
            return
        node = nxt["nodes"][0]
        prompt = _json("prompt", symbol, node, date=date)
        answer = _answer(canned, Path(prompt["prompt_path"]).read_text())
        Path(prompt["answer_path"]).write_text(answer)
        applied = _json("apply", symbol, node, date=date)
        assert applied["ok"] is True


def test_full_day_three_symbols(var_dir, offline):
    paths = Paths(var_dir)
    canned = make_canned()

    # Day 1: nothing pending yet, three states, full pipeline per symbol.
    day1_init = _json("init", date=DAY1)
    assert day1_init["trading_day"] is True
    assert day1_init["symbols"] == SYMBOLS
    assert day1_init["fills"]["filled"] == []
    assert paths.run_dir(DAY1).is_dir()

    assert _json("memory", "pending", date=DAY1)["prompts"] == []
    for symbol in SYMBOLS:
        _cli("state", "init", symbol, date=DAY1)
        _run_symbol_pipeline(canned, symbol, DAY1)

    finalized = _cli("finalize", date=DAY1)
    assert finalized.count("\n") == len(SYMBOLS)

    decision_file = DecisionFile.model_validate_json(paths.decisions_file(DAY1).read_text())
    assert decision_file.trade_date == DAY1
    assert [d.symbol for d in decision_file.decisions] == SYMBOLS
    orders = {d.symbol: d.order for d in decision_file.decisions if d.order is not None}
    assert set(orders) == set(SYMBOLS)
    assert all(order.side == "buy" for order in orders.values())
    pending = json.loads(paths.pending_fills_file.read_text())
    assert sorted(entry["symbol"] for entry in pending) == sorted(SYMBOLS)

    # Day 2: the day-1 orders fill at the day-1 open; the book matches them.
    day2_init = _json("init", date=DAY2)
    report = FillReport.model_validate(day2_init["fills"])
    assert sorted(fill.symbol for fill in report.filled) == sorted(SYMBOLS)
    assert report.still_pending == []
    assert report.missing_price == []
    assert paths.pending_fills_file.read_text().strip() == "[]"

    book = PortfolioContext.model_validate(json.loads(paths.book_file.read_text()))
    expected_cash = 100000 - sum(
        order.qty * OPEN_PRICES[symbol] for symbol, order in orders.items()
    )
    assert book.cash == pytest.approx(expected_cash)
    positions = {p.ticker: p.quantity for p in book.positions}
    assert positions == {symbol: order.qty for symbol, order in orders.items()}


def test_init_non_trading_day(var_dir, monkeypatch):
    monkeypatch.setattr(market, "is_trading_day", lambda day: False)

    assert _json("init", date="2026-10-17") == {"trading_day": False}
    assert not (var_dir / "runs" / "2026-10-17").exists()
    assert not (var_dir / "book.json").exists()
