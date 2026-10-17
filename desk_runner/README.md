# Desk runner

The desk runner runs the TradingAgents pipeline for a small set of symbols as a Claude Desktop
scheduled task. The orchestrator (an Opus session following `desk_runner/playbook.md`) spawns
sub-agents for each agent turn. The `ma` command (`desk_runner/cli.py`) prepares each prompt, applies
each answer through the upstream node functions, and keeps a virtual paper book. The runner never
places orders with a broker: decisions are written to files and recorded as pending fills.

Design: `docs/superpowers/specs/2026-10-17-desk-runner-design.md`.

## Setup

From the repository root:

```bash
uv venv
uv pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and set `FRED_API_KEY` (free key from
https://fred.stlouisfed.org/docs/api/api_key.html). `.env` is gitignored. The other upstream data
sources need no key.

Symbols, models, rounds and `var_dir` are in `desk_runner/config.yaml`. An invalid or missing file
stops the CLI with an error.

## Commands

All commands are run as `.venv/bin/ma <command>` from the repository root. Each takes an optional
`--date YYYY-MM-DD` (default: today in America/New_York).

| Command | Purpose |
|---|---|
| `ma init` | Check for a trading day, fill pending orders at the open, create the day's run directory. |
| `ma memory pending` | Write reflection prompts for settled past decisions. |
| `ma memory settle` | Store the sub-agents' reflection answers. |
| `ma state init <SYMBOL>` | Create the day's state for a symbol. |
| `ma next <SYMBOL>` | List the nodes that run next, or `done`. |
| `ma prompt <SYMBOL> <NODE>` | Write the prompt for a node's sub-agent. |
| `ma apply <SYMBOL> <NODE> [--freetext]` | Apply a sub-agent's answer to the node. |
| `ma tool <SYMBOL> <ANALYST> <TOOL> '<JSON>'` | Data tool for an analyst sub-agent. |
| `ma finalize` | Write the decision file, queue pending fills, log decisions. |
| `ma log <MESSAGE>` | Append a line to the day's timing log. |

The playbook describes the order in which the orchestrator runs them.

## Files under `desk_runner/var/`

`var/` is gitignored and holds all runtime state. Do not edit these files by hand.

```
var/
  book.json              virtual book (cash, positions)
  pending_fills.json     decisions waiting for their fill at the next open
  decisions/<DATE>.json  the day's decision file (format: spec section 7)
  memory/trading_memory.md  upstream memory log
  cache/                 upstream data cache
  runs/<DATE>/
    timing.log           output of `ma log`
    memory/              reflection prompts and answers
    <SYMBOL>/state.json  agent state for the symbol
```

For the decision file format, see section 7 of the design spec.

## Running it

Scheduled task: a Claude Desktop task on weekdays at 14:00 Europe/Brussels, working folder this
repository, session model Opus, prompt:

> Follow desk_runner/playbook.md exactly.

To run it by hand, start a Claude Code or Desktop session in this repository and send the same
prompt.

## Known limitations

From section 12 of the design spec:

- Scheduled runs only happen while the Mac and the Claude app are running. A missed run is skipped.
- Sub-agents are a different runtime from upstream's API calls. Prompts are identical, but outputs
  can differ from an API run with the same models.
- Upstream's memory log measures outcomes from the first close after the trade date, while the
  virtual book fills at the open. Both are kept as upstream and the design spec define them.
- Run time and token use for the 22 sequential turns per symbol are not measured yet. The spike
  took about 7 minutes and 900k tokens for 11 turns.
