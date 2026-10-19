"""Memory bridge: reflections on settled decisions, captured to files and replayed from sub-agent answers."""

import json

import pytest

from desk_runner.config import DEFAULT_CONFIG_PATH, DeskConfig, Paths, load_config, upstream_config
from desk_runner.memory_bridge import past_context, pending_reflections, settle_reflections
from tradingagents.memory import settlement
from tradingagents.memory.log import TradingMemoryLog

RUN_DATE = "2026-10-19"
ENTRY_DATE = "2026-10-05"
STEM = "AAPL_2026-10-05"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The entry's holding window has traded: 3% raw, 1% alpha, no price fetch."""
    monkeypatch.setattr(
        settlement, "fetch_returns", lambda *args, **kwargs: (0.03, 0.01, 5, "2026-10-12")
    )


@pytest.fixture
def paths(tmp_path) -> Paths:
    return Paths(tmp_path / "var")


@pytest.fixture
def cfg(paths) -> DeskConfig:
    return load_config(DEFAULT_CONFIG_PATH).model_copy(update={"var_dir": paths.var_dir})


def _log(paths, cfg) -> TradingMemoryLog:
    return TradingMemoryLog(upstream_config(cfg, paths))


@pytest.fixture
def pending_aapl(paths, cfg) -> None:
    _log(paths, cfg).store_decision("AAPL", ENTRY_DATE, "Buy AAPL on services growth.", rating="Buy")


def _memory_dir(paths):
    return paths.run_dir(RUN_DATE) / "memory"


def test_pending_writes_prompt_and_keeps_entry_pending(paths, cfg, pending_aapl):
    prompts = pending_reflections(paths, cfg, RUN_DATE)

    assert prompts == [_memory_dir(paths) / f"{STEM}.prompt.md"]
    assert "Raw return over 5 trading days: +3.0%" in prompts[0].read_text(encoding="utf-8")
    meta = json.loads((_memory_dir(paths) / f"{STEM}.meta.json").read_text(encoding="utf-8"))
    assert meta["answer_file"] == f"{STEM}.answer.md"
    assert len(meta["prompt_sha256"]) == 64
    assert [e["ticker"] for e in _log(paths, cfg).get_pending_entries()] == ["AAPL"]


def test_settle_uses_answer(paths, cfg, pending_aapl):
    pending_reflections(paths, cfg, RUN_DATE)
    (_memory_dir(paths) / f"{STEM}.answer.md").write_text("Lesson text.", encoding="utf-8")

    settled = settle_reflections(paths, cfg, RUN_DATE)

    assert settled == [("AAPL", ENTRY_DATE)]
    log = _log(paths, cfg)
    assert log.get_pending_entries() == []
    (entry,) = log.load_entries()
    assert entry["reflection"] == "Lesson text."


def test_settle_without_answer_keeps_pending(paths, cfg, pending_aapl):
    pending_reflections(paths, cfg, RUN_DATE)

    assert settle_reflections(paths, cfg, RUN_DATE) == []
    assert [e["ticker"] for e in _log(paths, cfg).get_pending_entries()] == ["AAPL"]


def test_settle_with_blank_answer_keeps_pending(paths, cfg, pending_aapl):
    pending_reflections(paths, cfg, RUN_DATE)
    (_memory_dir(paths) / f"{STEM}.answer.md").write_text("  \n", encoding="utf-8")

    assert settle_reflections(paths, cfg, RUN_DATE) == []
    assert [e["ticker"] for e in _log(paths, cfg).get_pending_entries()] == ["AAPL"]


def test_past_context_includes_lesson(paths, cfg, pending_aapl):
    pending_reflections(paths, cfg, RUN_DATE)
    (_memory_dir(paths) / f"{STEM}.answer.md").write_text("Lesson text.", encoding="utf-8")
    settle_reflections(paths, cfg, RUN_DATE)

    assert "Lesson text." in past_context(paths, cfg, "AAPL", "2026-10-19")
