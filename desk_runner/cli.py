"""`ma` command line: the desk runner's entry point for the scheduled task."""

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
