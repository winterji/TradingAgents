"""Per-symbol AgentState files: created once per run, then updated by concurrent analysts under a file lock."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, messages_from_dict, messages_to_dict
from langgraph.graph.message import add_messages

from desk_runner.config import Paths, write_json_atomic
from tradingagents.dataflows.files import locked
from tradingagents.graph.propagation import Propagator


class StateError(Exception):
    """A per-symbol state file is missing, already exists, or is unreadable."""


def init_state(
    paths: Paths,
    date: str,
    symbol: str,
    portfolio_context: str,
    past_context: str,
    instrument_context: str,
) -> dict[str, Any]:
    """Create the state file for ``symbol`` on ``date``; refuses to overwrite an existing one."""
    path = paths.state_file(date, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        if path.exists():
            raise StateError(f"state already exists for {symbol} on {date}: {path}")
        state = Propagator().create_initial_state(
            symbol,
            date,
            past_context=past_context,
            instrument_context=instrument_context,
            portfolio_context=portfolio_context,
        )
        state["messages"] = [
            HumanMessage(content=message[1]) if isinstance(message, tuple) else message
            for message in state["messages"]
        ]
        _save(path, state)
    return state


def load_state(paths: Paths, date: str, symbol: str) -> dict[str, Any]:
    """Read the state file for ``symbol`` on ``date``."""
    return _read(paths.state_file(date, symbol))


def update_state(
    paths: Paths,
    date: str,
    symbol: str,
    fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Apply ``fn`` to the current state under the file lock and save the merged result.

    ``messages`` in the update are appended; every other key replaces the stored value.
    """
    path = paths.state_file(date, symbol)
    if not path.parent.is_dir():
        raise StateError(f"no state for {symbol} on {date}: {path}")
    with locked(path):
        state = _read(path)
        update = fn(state)
        merged = {**state, **{key: value for key, value in update.items() if key != "messages"}}
        if "messages" in update:
            merged["messages"] = add_messages(state["messages"], update["messages"])
        _save(path, merged)
    return merged


def _save(path: Path, state: dict[str, Any]) -> None:
    data = {**state, "messages": messages_to_dict(state["messages"])}
    write_json_atomic(path, data)


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise StateError(f"state file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read state file {path}: {exc}") from exc
    messages: list[BaseMessage] = messages_from_dict(data["messages"])
    return {**data, "messages": messages}
