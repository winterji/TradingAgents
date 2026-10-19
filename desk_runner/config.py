"""Desk runner configuration: the YAML file, the runtime file layout and atomic JSON writes."""

import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tradingagents.default_config import DEFAULT_CONFIG

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"
CONFIG_ENV_VAR = "DESK_RUNNER_CONFIG"
MARKET_TZ = ZoneInfo("America/New_York")


class ConfigError(Exception):
    """The desk runner config file is missing, unreadable or invalid."""


class ModelsConfig(BaseModel):
    deep: str
    quick: str


class DeskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(min_length=1)
    max_debate_rounds: int = Field(ge=1)
    max_risk_discuss_rounds: int = Field(ge=1)
    max_tool_rounds: int = Field(ge=1)
    initial_cash: float = Field(gt=0)
    currency: str
    models: ModelsConfig
    var_dir: Path

    @field_validator("symbols")
    @classmethod
    def _symbols_uppercase_and_unique(cls, symbols: list[str]) -> list[str]:
        for symbol in symbols:
            if symbol != symbol.upper():
                raise ValueError(f"symbol {symbol!r} must be uppercase")
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be unique")
        return symbols


def load_config(path: Path | None = None) -> DeskConfig:
    """Load the config from ``path``, else ``$DESK_RUNNER_CONFIG``, else the shipped file."""
    if path is None:
        env_path = os.environ.get(CONFIG_ENV_VAR)
        path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        return DeskConfig.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"invalid config file {path}: {details}") from exc


@dataclass(frozen=True)
class Paths:
    """Layout of the runtime files under ``var_dir``."""

    var_dir: Path

    def run_dir(self, date: str) -> Path:
        return self.var_dir / "runs" / date

    def symbol_dir(self, date: str, symbol: str) -> Path:
        return self.run_dir(date) / symbol

    def state_file(self, date: str, symbol: str) -> Path:
        return self.symbol_dir(date, symbol) / "state.json"

    @property
    def book_file(self) -> Path:
        return self.var_dir / "book.json"

    @property
    def pending_fills_file(self) -> Path:
        return self.var_dir / "pending_fills.json"

    def decisions_file(self, date: str) -> Path:
        return self.var_dir / "decisions" / f"{date}.json"

    @property
    def memory_log(self) -> Path:
        return self.var_dir / "memory" / "trading_memory.md"

    @property
    def cache_dir(self) -> Path:
        return self.var_dir / "cache"


def upstream_config(cfg: DeskConfig, paths: Paths) -> dict[str, Any]:
    """Upstream DEFAULT_CONFIG with the desk runner's round limits and file locations."""
    overrides = deepcopy(DEFAULT_CONFIG)
    overrides["max_debate_rounds"] = cfg.max_debate_rounds
    overrides["max_risk_discuss_rounds"] = cfg.max_risk_discuss_rounds
    overrides["max_tool_rounds"] = cfg.max_tool_rounds
    overrides["data_cache_dir"] = str(paths.cache_dir)
    overrides["memory_log_path"] = str(paths.memory_log)
    return overrides


def write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` through a temp file, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def today_ny() -> str:
    """Today's date in America/New_York, the trading-day boundary."""
    return datetime.now(MARKET_TZ).date().isoformat()
