"""`ma` command line: the desk runner's entry point for the scheduled task."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import typer
from dotenv import load_dotenv

from desk_runner.config import (
    ConfigError,
    DeskConfig,
    Paths,
    load_config,
    today_ny,
    upstream_config,
)
from desk_runner.nodes import NodeError, apply_answer, build_prompt, next_nodes
from desk_runner.state_store import StateError, load_state
from tradingagents.dataflows.config import set_config

app = typer.Typer(help="Desk runner: TradingAgents research and paper-trading desk.")


@dataclass(frozen=True)
class AppContext:
    config: DeskConfig
    paths: Paths


DateOption = Annotated[
    str | None, typer.Option("--date", help="Trade date YYYY-MM-DD (default: today, New York).")
]


@app.callback()
def main(ctx: typer.Context) -> None:
    load_dotenv()
    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    paths = Paths(config.var_dir)
    set_config(upstream_config(config, paths))
    ctx.obj = AppContext(config=config, paths=paths)


@app.command()
def log(
    ctx: typer.Context,
    message: Annotated[str, typer.Argument(help="Line to append to the run's timing log.")],
    date: DateOption = None,
) -> None:
    """Append a timestamped line to the run's timing log."""
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    run_dir = paths.run_dir(trade_date)
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (run_dir / "timing.log").open("a") as handle:
        handle.write(f"{stamp} {message}\n")


SymbolArgument = Annotated[str, typer.Argument(help="Symbol, e.g. AAPL.")]
NodeArgument = Annotated[str, typer.Argument(help="Upstream node key, e.g. bull or trader.")]


@app.command("next")
def next_command(ctx: typer.Context, symbol: SymbolArgument, date: DateOption = None) -> None:
    """List the upstream nodes that run next for the symbol's state."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    try:
        nodes = next_nodes(load_state(paths, trade_date, symbol), config)
    except StateError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"nodes": nodes, "done": not nodes}))


@app.command("prompt")
def prompt_command(
    ctx: typer.Context,
    symbol: SymbolArgument,
    node: NodeArgument,
    date: DateOption = None,
) -> None:
    """Write the prompt the upstream node would send next as a turn for a sub-agent."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    try:
        prompt_file = build_prompt(paths, trade_date, symbol, node, config)
    except (NodeError, StateError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(prompt_file.model_dump_json())


@app.command("apply")
def apply_command(
    ctx: typer.Context,
    symbol: SymbolArgument,
    node: NodeArgument,
    date: DateOption = None,
    freetext: Annotated[
        bool,
        typer.Option("--freetext", help="Take the node's free-text path with the answer as its text."),
    ] = False,
) -> None:
    """Replay the sub-agent's answer for the pending turn through the upstream node."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    try:
        state = apply_answer(paths, trade_date, symbol, node, config, freetext=freetext)
    except (NodeError, StateError) as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}))
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"ok": True, "next": next_nodes(state, config)}))
