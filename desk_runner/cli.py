"""`ma` command line: the desk runner's entry point for the scheduled task."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import typer
from dotenv import load_dotenv

from desk_runner import market
from desk_runner.book import load_book, process_pending, render_for_agents
from desk_runner.config import (
    ConfigError,
    DeskConfig,
    Paths,
    load_config,
    today_ny,
    upstream_config,
)
from desk_runner.finalize import FinalizeError, finalize
from desk_runner.memory_bridge import (
    MemoryBridgeError,
    past_context,
    pending_reflections,
    settle_reflections,
)
from desk_runner.nodes import NodeError, apply_answer, build_prompt, next_nodes
from desk_runner.state_store import StateError, init_state, load_state
from desk_runner.tools_bridge import ToolBridgeError, run_tool
from tradingagents.agents.context import build_instrument_context, resolve_instrument_identity
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


@app.command("init")
def init_command(ctx: typer.Context, date: DateOption = None) -> None:
    """Fill the pending orders of earlier days at their open and start the day's run directory."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    if not market.is_trading_day(trade_date):
        typer.echo(json.dumps({"trading_day": False}))
        return
    report = process_pending(paths, config, trade_date, market.open_on)
    paths.run_dir(trade_date).mkdir(parents=True, exist_ok=True)
    typer.echo(
        json.dumps(
            {
                "trading_day": True,
                "fills": report.model_dump(mode="json"),
                "symbols": config.symbols,
            }
        )
    )


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


ToolArgument = Annotated[str, typer.Argument(help="Upstream tool name, e.g. get_stock_data.")]
ArgsArgument = Annotated[str, typer.Argument(help="Model-visible tool arguments as a JSON object.")]


@app.command("tool")
def tool_command(
    ctx: typer.Context,
    symbol: SymbolArgument,
    analyst: NodeArgument,
    tool: ToolArgument,
    args: ArgsArgument,
    date: DateOption = None,
) -> None:
    """Run an upstream data tool for an analyst; prints the tool's output."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    try:
        arguments = json.loads(args)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: arguments are not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not isinstance(arguments, dict):
        typer.echo("error: arguments must be a JSON object", err=True)
        raise typer.Exit(code=1)
    try:
        output = run_tool(paths, trade_date, symbol, analyst, tool, arguments, config)
    except ToolBridgeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(output)


@app.command("finalize")
def finalize_command(ctx: typer.Context, date: DateOption = None) -> None:
    """Write the day's decision file, queue its orders as pending fills and log the decisions."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    try:
        decision_file = finalize(paths, config, trade_date, price=market.last_close)
    except FinalizeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for decision in decision_file.decisions:
        if decision.order is not None:
            typer.echo(f"{decision.symbol} order {decision.order.side} {decision.order.qty}")
        else:
            typer.echo(f"{decision.symbol} {decision.status}: {decision.reason}")


memory_app = typer.Typer(help="Memory: reflections on settled decisions, written by sub-agents.")
app.add_typer(memory_app, name="memory")


@memory_app.command("pending")
def memory_pending_command(ctx: typer.Context, date: DateOption = None) -> None:
    """Write the reflection prompts for decisions whose window has traded; entries stay pending."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    try:
        prompts = pending_reflections(paths, config, trade_date)
    except MemoryBridgeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"prompts": [str(prompt) for prompt in prompts]}))


@memory_app.command("settle")
def memory_settle_command(ctx: typer.Context, date: DateOption = None) -> None:
    """Settle pending decisions that have a sub-agent's answer for their reflection prompt."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    try:
        settled = settle_reflections(paths, config, trade_date)
    except MemoryBridgeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"settled": [{"ticker": t, "date": d} for t, d in settled]}))


state_app = typer.Typer(help="Per-symbol agent state: created once per symbol and day.")
app.add_typer(state_app, name="state")


@state_app.command("init")
def state_init_command(ctx: typer.Context, symbol: SymbolArgument, date: DateOption = None) -> None:
    """Create the day's state file for a symbol, seeded with the book, past lessons and identity."""
    config: DeskConfig = ctx.obj.config
    paths: Paths = ctx.obj.paths
    trade_date = date or today_ny()
    try:
        init_state(
            paths,
            trade_date,
            symbol,
            portfolio_context=render_for_agents(load_book(paths, config), symbol),
            past_context=past_context(paths, config, symbol, trade_date),
            instrument_context=build_instrument_context(
                symbol, "stock", resolve_instrument_identity(symbol), trade_date
            ),
        )
    except (StateError, MemoryBridgeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps({"ok": True, "symbol": symbol, "date": trade_date}))
