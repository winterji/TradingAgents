"""`ma init --ignore-calendar`: the NYSE check is skipped for test runs only."""

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from desk_runner import market
from desk_runner.cli import app
from desk_runner.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, Paths

DAY = "2026-10-17"

runner = CliRunner()


@pytest.fixture
def var_dir(tmp_path, monkeypatch) -> Path:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["var_dir"] = str(tmp_path / "var")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config_path))
    monkeypatch.setattr(market, "open_on", lambda symbol, day: 1.0)
    return tmp_path / "var"


def _invoke(*args: str) -> dict:
    result = runner.invoke(app, ["init", *args, "--date", DAY])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_ignore_calendar_runs_on_a_non_trading_day(var_dir, monkeypatch):
    calls: list[str] = []

    def fake_is_trading_day(day: str) -> bool:
        calls.append(day)
        return False

    monkeypatch.setattr(market, "is_trading_day", fake_is_trading_day)

    output = _invoke("--ignore-calendar")

    assert calls == []
    assert output["trading_day"] is True
    assert output["calendar_ignored"] is True
    assert "fills" in output
    assert "symbols" in output
    assert Paths(var_dir).run_dir(DAY).is_dir()


def test_non_trading_day_without_flag_is_unchanged(var_dir, monkeypatch):
    monkeypatch.setattr(market, "is_trading_day", lambda day: False)

    assert _invoke() == {"trading_day": False}
    assert not Paths(var_dir).run_dir(DAY).exists()


def test_trading_day_without_flag_reports_calendar_not_ignored(var_dir, monkeypatch):
    monkeypatch.setattr(market, "is_trading_day", lambda day: True)

    output = _invoke()

    assert output["trading_day"] is True
    assert output["calendar_ignored"] is False
    assert Paths(var_dir).run_dir(DAY).is_dir()
