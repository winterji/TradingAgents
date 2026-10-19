"""``ma tool``: run an upstream ``@tool`` for an analyst, with the run's state injected.

The analyst sub-agent names the tool and passes only the model-visible arguments. The
arguments are validated the way upstream's ToolNode validates them (``tool.args_schema``,
lax, unknown keys dropped), and the parameters the ToolNode would fill from graph state
(``InjectedState``) are read from the symbol's state file here, so the model cannot
choose the symbol or the trade date.

Counting: each ``ma tool`` call counts as one tool round, including a call whose
arguments fail validation. Parallel calls are not grouped; each one is its own round.
"""

import inspect
import json
import typing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.prebuilt import InjectedState
from pydantic import ValidationError

from desk_runner.config import DeskConfig, Paths
from desk_runner.nodes import NODES
from desk_runner.state_store import StateError, load_state
from tradingagents.agents.analysts.turn import WRAP_UP
from tradingagents.dataflows.files import locked


class ToolBridgeError(Exception):
    """The tool is not offered to this analyst, its arguments are invalid, or the state is unreadable."""


def run_tool(
    paths: Paths,
    date: str,
    symbol: str,
    analyst: str,
    tool_name: str,
    args: dict[str, Any],
    cfg: DeskConfig,
) -> str:
    """Call ``tool_name`` for ``analyst`` on ``symbol``, returning the tool's text output.

    Once the analyst has used ``cfg.max_tool_rounds`` calls, returns ``WRAP_UP`` without
    calling the tool, so the analyst writes its report from what it already has.
    """
    tool = _allowed_tool(analyst, tool_name)
    try:
        state = load_state(paths, date, symbol)
    except StateError as exc:
        raise ToolBridgeError(str(exc)) from exc
    injected = _injected_arguments(tool, state)

    log_path = paths.symbol_dir(date, symbol) / "tool_calls.jsonl"
    with locked(log_path):
        if _rounds_used(log_path, analyst) >= cfg.max_tool_rounds:
            return WRAP_UP
        _append_call(log_path, analyst, tool_name, args)

    return str(tool.func(**_validated_arguments(tool, args, injected)))


def _allowed_tool(analyst: str, tool_name: str) -> BaseTool:
    if analyst not in NODES:
        raise ToolBridgeError(f"unknown analyst {analyst!r}; known: {', '.join(NODES)}")
    for tool in NODES[analyst].tools:
        if tool.name == tool_name:
            return tool
    offered = [tool.name for tool in NODES[analyst].tools]
    raise ToolBridgeError(
        f"tool {tool_name!r} is not offered to {analyst}; offered: {', '.join(offered) or 'none'}"
    )


def _injected_arguments(tool: BaseTool, state: dict[str, Any]) -> dict[str, Any]:
    """The state values for every parameter annotated with ``InjectedState``."""
    hints = typing.get_type_hints(tool.func, include_extras=True)
    injected: dict[str, Any] = {}
    for name, hint in hints.items():
        if typing.get_origin(hint) is not typing.Annotated:
            continue
        for marker in typing.get_args(hint)[1:]:
            if isinstance(marker, InjectedState):
                injected[name] = state if marker.field is None else state[marker.field]
    return injected


def _validated_arguments(
    tool: BaseTool, args: dict[str, Any], injected: dict[str, Any]
) -> dict[str, Any]:
    """The tool's keyword arguments, validated and coerced exactly as upstream's ToolNode does."""
    for name in args:
        if name in injected:
            raise ToolBridgeError(
                f"{name!r} is an injected argument and cannot be set by the model"
            )
    schema = tool.args_schema
    try:
        validated = schema.model_validate({**args, **injected})
    except ValidationError as exc:
        raise ToolBridgeError(f"invalid arguments for {tool.name}: {exc}") from exc
    parameters = inspect.signature(tool.func).parameters
    return {name: getattr(validated, name) for name in schema.model_fields if name in parameters}


def _rounds_used(log_path: Path, analyst: str) -> int:
    if not log_path.exists():
        return 0
    entries = (json.loads(line) for line in log_path.read_text().splitlines() if line.strip())
    return sum(1 for entry in entries if entry["analyst"] == analyst)


def _append_call(log_path: Path, analyst: str, tool_name: str, args: dict[str, Any]) -> None:
    entry = {
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "analyst": analyst,
        "tool": tool_name,
        "args": args,
    }
    with log_path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
