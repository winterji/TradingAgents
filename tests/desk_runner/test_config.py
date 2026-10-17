"""Config loading, the Paths helper and the CLI's startup guard."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import tradingagents.dataflows.config as upstream_config_module
from desk_runner import config as config_module
from desk_runner.cli import app
from desk_runner.config import ConfigError, Paths, load_config, upstream_config

SHIPPED = Path(config_module.__file__).parent / "config.yaml"


@pytest.fixture(autouse=True)
def _restore_upstream_config():
    """The CLI pushes its settings into the process-wide upstream config; undo that."""
    saved = deepcopy(upstream_config_module._config)
    yield
    upstream_config_module._config = saved


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


def _shipped_data() -> dict:
    return yaml.safe_load(SHIPPED.read_text())


def test_shipped_config_loads():
    cfg = load_config(SHIPPED)

    assert cfg.symbols == ["AAPL", "GOOGL", "AMZN"]
    assert cfg.max_debate_rounds == 3
    assert cfg.max_risk_discuss_rounds == 3
    assert cfg.max_tool_rounds == 20
    assert cfg.initial_cash == 100000
    assert cfg.currency == "USD"
    assert cfg.models.deep == "claude-opus-5-5"
    assert cfg.models.quick == "claude-sonnet-5-5"


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="missing.yaml"):
        load_config(tmp_path / "missing.yaml")


def test_unknown_key_raises(tmp_path):
    data = _shipped_data()
    data["surprise"] = 1
    with pytest.raises(ConfigError, match="surprise"):
        load_config(_write_yaml(tmp_path / "c.yaml", data))


def test_invalid_yaml_raises(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("symbols: [AAPL\n")
    with pytest.raises(ConfigError, match="c.yaml"):
        load_config(bad)


def test_zero_rounds_raises(tmp_path):
    data = _shipped_data()
    data["max_debate_rounds"] = 0
    with pytest.raises(ConfigError, match="max_debate_rounds"):
        load_config(_write_yaml(tmp_path / "c.yaml", data))


def test_duplicate_symbols_raises(tmp_path):
    data = _shipped_data()
    data["symbols"] = ["AAPL", "AAPL"]
    with pytest.raises(ConfigError, match="unique"):
        load_config(_write_yaml(tmp_path / "c.yaml", data))


def test_lowercase_symbol_raises(tmp_path):
    data = _shipped_data()
    data["symbols"] = ["aapl"]
    with pytest.raises(ConfigError, match="uppercase"):
        load_config(_write_yaml(tmp_path / "c.yaml", data))


def test_env_var_selects_config_file(monkeypatch, tmp_path):
    data = _shipped_data()
    data["initial_cash"] = 5
    monkeypatch.setenv("DESK_RUNNER_CONFIG", str(_write_yaml(tmp_path / "c.yaml", data)))

    assert load_config().initial_cash == 5


def test_default_path_is_shipped_config(monkeypatch):
    monkeypatch.delenv("DESK_RUNNER_CONFIG", raising=False)

    assert load_config().symbols == ["AAPL", "GOOGL", "AMZN"]


def test_upstream_config_overrides(tmp_path):
    cfg = load_config(SHIPPED)
    paths = Paths(tmp_path)

    overrides = upstream_config(cfg, paths)

    assert overrides["max_risk_discuss_rounds"] == 3
    assert overrides["max_debate_rounds"] == 3
    assert overrides["max_tool_rounds"] == 20
    assert overrides["memory_log_path"].endswith("memory/trading_memory.md")
    assert overrides["data_cache_dir"] == str(paths.cache_dir)


def test_paths_layout(tmp_path):
    paths = Paths(tmp_path)

    assert paths.run_dir("2026-10-17") == tmp_path / "runs" / "2026-10-17"
    assert paths.symbol_dir("2026-10-17", "AAPL") == tmp_path / "runs" / "2026-10-17" / "AAPL"
    assert paths.state_file("2026-10-17", "AAPL") == (
        tmp_path / "runs" / "2026-10-17" / "AAPL" / "state.json"
    )
    assert paths.book_file == tmp_path / "book.json"
    assert paths.pending_fills_file == tmp_path / "pending_fills.json"
    assert paths.decisions_file("2026-10-17") == tmp_path / "decisions" / "2026-10-17.json"
    assert paths.memory_log == tmp_path / "memory" / "trading_memory.md"
    assert paths.cache_dir == tmp_path / "cache"


def test_cli_refuses_bad_config(monkeypatch, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("symbols: []\n")
    monkeypatch.setenv("DESK_RUNNER_CONFIG", str(bad))

    result = CliRunner().invoke(app, ["log", "hello"])

    assert result.exit_code == 2
    assert str(bad) in result.output


def test_log_appends_to_timing_log(monkeypatch, tmp_path):
    data = _shipped_data()
    data["var_dir"] = str(tmp_path / "var")
    monkeypatch.setenv("DESK_RUNNER_CONFIG", str(_write_yaml(tmp_path / "c.yaml", data)))

    result = CliRunner().invoke(app, ["log", "first", "--date", "2026-10-17"])
    assert result.exit_code == 0, result.output
    CliRunner().invoke(app, ["log", "second", "--date", "2026-10-17"])

    lines = (tmp_path / "var" / "runs" / "2026-10-17" / "timing.log").read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("Z first")
    assert lines[1].endswith("Z second")
    assert lines[0].startswith("2026-10-17T")


def test_write_json_atomic_replaces_file(tmp_path):
    target = tmp_path / "nested" / "state.json"

    config_module.write_json_atomic(target, {"a": 1})
    config_module.write_json_atomic(target, {"a": 2})

    assert json.loads(target.read_text()) == {"a": 2}
    assert sorted(p.name for p in target.parent.iterdir()) == ["state.json"]


def test_today_ny_is_iso_date():
    value = config_module.today_ny()

    assert len(value) == 10 and value[4] == "-" and value[7] == "-"
