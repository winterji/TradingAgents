# Desk runner: TradingAgents as a Claude Desktop scheduled task with sub-agents

Date: 2026-10-17. Status: draft for review.
Upstream base: TauricResearch/TradingAgents at `e8f5503` (v0.7.0), forked to `winterji/TradingAgents`.

## 1. Goal

Run the TradingAgents pipeline every US trading day for **AAPL, GOOGL and AMZN** as a Claude Desktop
scheduled task, where every agent turn is a Claude sub-agent instead of an API call. Instead of
trading, the run writes one JSON file per day that says what to buy, sell or short and how much.

Success criteria:

- The agent roles, the order of turns, the prompts and every agent-to-agent data structure are the
  upstream ones, unchanged. Upstream code builds the prompts, validates outputs and updates the state.
- Both debates run **3 rounds** (6 bull/bear turns, 9 risk turns).
- Analysts use the upstream tool functions and data sources.
- The decision file is deterministic given the agents' outputs, and every number in it can be traced
  to an agent output and a price.
- No broker access. Nothing is traded.

## 2. Decisions (agreed 2026-10-17)

| Topic | Decision |
|---|---|
| Fidelity | Replicate the current code at `e8f5503`, not the paper's prose. |
| Data structures | `AgentState`, `InvestDebateState`, `RiskDebateState`; `SentimentReport`, `ResearchPlan`, `TraderProposal`, `PortfolioDecision`; `PortfolioContext`; the memory log format. Used from the upstream package, never copied. |
| Models | Code tiers. Deep = Opus (`claude-opus-5-5`): Research Manager, Portfolio Manager, and the orchestrator. Quick = Sonnet (`claude-sonnet-5-5`): 4 analysts, bull, bear, trader, 3 risk debaters, reflection. |
| Rounds | `max_debate_rounds: 3`, `max_risk_discuss_rounds: 3`. |
| Execution model | Approach A: fresh sub-agent per agent turn (the code makes a fresh LLM call per turn with the history in the prompt). |
| Data sources | Upstream defaults: yfinance (prices, indicators via stockstats, news, profile, insiders), SEC EDGAR (statements), Reddit RSS, FRED (needs `FRED_API_KEY`). StockTwits (HTTP 403) and Polymarket (connection refused from this network) are unavailable and reported to agents with upstream's own unavailable text. |
| Portfolio | Virtual book, $100,000 starting cash, kept by the runner. Shorting allowed. No exposure limits. |
| Sizing | Trader's `position_sizing` text = target position size; Portfolio Manager rating gates direction (section 6). Ranges use the midpoint. |
| Fills | A decision made on day D is assumed filled at D's open (applied by the next run). |
| Schedule | Mon-Fri 14:00 Europe/Brussels (08:00 New York, 09:00 for ~3 weeks a year), NYSE holidays skipped. |
| Memory | Upstream memory log and reflection, as in the code. |
| Location | Fork at `/Users/winterji/Projects/TradingAgentsFork`; separate from the AITradingAgent repo. Upstream files stay untouched apart from `pyproject.toml` and `.gitignore`. |

## 3. Pipeline (per symbol, identical to `GraphSetup.setup_graph`)

```
Market, Sentiment ("social"), News, Fundamentals analysts  +  Memory Log step     (parallel)
  -> Bull -> Bear -> Bull -> Bear -> Bull -> Bear        (ConditionalLogic, count >= 2 * 3)
  -> Research Manager                                     (structured: ResearchPlan)
  -> Trader                                               (structured: TraderProposal)
  -> Aggressive -> Conservative -> Neutral  x3            (ConditionalLogic, count >= 3 * 3)
  -> Portfolio Manager                                    (structured: PortfolioDecision)
```

The three symbols run in parallel. Sub-agents per symbol: 4 analysts + 6 + 1 + 1 + 9 + 1 = 22,
plus reflections for settled memory entries.

## 4. Components

All new code lives in a separate package `desk_runner/` inside the fork, tests in `tests/desk_runner/`.

### 4.1 `desk_runner/config.yaml` + config model

```yaml
symbols: [AAPL, GOOGL, AMZN]
max_debate_rounds: 3
max_risk_discuss_rounds: 3
max_tool_rounds: 20
initial_cash: 100000
currency: USD
models: {deep: claude-opus-5-5, quick: claude-sonnet-5-5}
var_dir: desk_runner/var
```

Validated with Pydantic at startup; invalid or missing config means the CLI refuses to run.
`FRED_API_KEY` comes from the environment (`.env`, gitignored; `.env.example` documents it).
Upstream's config (`DEFAULT_CONFIG`) is used for everything else, with `data_cache_dir` and
`memory_log_path` pointed into `var_dir`.

### 4.2 CLI `ma`

The orchestrator and sub-agents run only these commands (`uv run ma ...`). Every command reads and
writes files under `var_dir`; none calls an LLM.

| Command | Does |
|---|---|
| `ma init [--date D]` | Market-day check (NYSE calendar via `exchange_calendars`); applies pending fills to the book (section 5); lists pending memory entries to reflect on; creates `runs/<D>/`. Exits with "not a trading day" otherwise. |
| `ma memory pending` / `ma memory settle <ticker> <date> <file>` | Splits upstream `settle_pending` around its one LLM call: returns are computed by upstream `fetch_returns`; the reflection prompt comes from upstream `Reflector`; a Sonnet sub-agent writes the reflection; `settle` stores it with `TradingMemoryLog.update_with_outcome`. |
| `ma state init <symbol>` | `Propagator.create_initial_state` with `instrument_context` (`resolve_instrument_identity` + `build_instrument_context`), `portfolio_context` (book `render()` plus one line that short positions are allowed), and `past_context` (`TradingMemoryLog.get_past_context`). |
| `ma prompt <symbol> <node>` | Calls the real upstream node function on the current state with a stand-in LLM that records the exact prompt (and, for structured nodes, the JSON schema of the output model) and writes it to a file for the sub-agent. For analysts it renders the formatted system prompt and tool list. |
| `ma tool <symbol> <tool> <json-args>` | Invokes the upstream `@tool` function with `company_of_interest` and `trade_date` injected from the state, exactly like `InjectedState`. Counts tool rounds per analyst; after `max_tool_rounds` it returns upstream's `WRAP_UP` message instead of data, so the analyst writes its report. |
| `ma apply <symbol> <node> <file>` | Calls the same upstream node function again with a stand-in LLM that returns the sub-agent's answer, and merges the returned dict into the state. For structured nodes the answer is parsed into the upstream Pydantic model; on failure the command exits non-zero with the validation error. `--freetext` applies the answer through upstream's free-text fallback path. |
| `ma next <symbol>` | Next node: analysts not yet reported, then upstream `ConditionalLogic` with 3/3 rounds, then `done`. |
| `ma finalize` | Sizing (section 6), writes `decisions/<D>.json`, records pending fills, calls `TradingMemoryLog.store_decision` per symbol. Refuses if the decision file for `D` exists. |

The sentiment analyst pre-fetches its sources inside the upstream node, so `ma prompt` for it
performs the news, StockTwits and Reddit fetches; the sub-agent only writes the report.

### 4.3 `desk_runner/playbook.md`

The orchestrator's instructions (Opus session): run `ma init`; run reflections; for each symbol in
parallel loop `ma next` -> `ma prompt` -> spawn a fresh `general-purpose` sub-agent with model
override (`sonnet`, or `opus` for Research Manager and Portfolio Manager) -> `ma apply`; then
`ma finalize`; write a timing log. The playbook contains no prompt text of its own beyond the
wrapper telling a sub-agent to answer the given prompt, use only `ma tool` for data (analysts), and
write its answer to the given file. The orchestrator never edits state files.

### 4.4 Scheduled task

Claude Desktop scheduled task, cron `0 14 * * 1-5` local time, working folder the fork, session model
Opus, prompt: follow `desk_runner/playbook.md`.

## 5. Virtual book

- `var/book.json` is an upstream `PortfolioContext`: `cash`, `currency`, `positions[]` of
  `{ticker, quantity (signed, negative = short), average_price}`. Starts at $100,000, no positions.
- Pending fills: `ma finalize` writes the day's orders to `var/pending_fills.json`. The next
  `ma init` fills each at the open of its trade date (yfinance daily bar), then removes it.
- Buy: `cash -= qty * open`. Sell (including opening or adding to a short): `cash += qty * open`.
- `average_price`: weighted average when a position grows in its direction; unchanged when it shrinks;
  reset to the fill price when it crosses zero; removed at zero.
- Equity = cash + sum(quantity * reference price). Shorts count negative.
- If an open price is missing, the fill stays pending and the run says so (fail safe: never invent a
  price).

## 6. Sizing

Per symbol, after the Portfolio Manager:

1. Trader action **Hold** or final rating **Hold** -> `no_order`.
2. Trader action and rating point in opposite directions (Buy vs Underweight/Sell, Sell vs
   Buy/Overweight) -> `review`, no order.
3. Final rating `REVIEW` (no readable rating) -> `review`, no order.
4. Parse the Trader's `position_sizing` deterministically (regex, no LLM):
   - `N%` of portfolio, equity, capital or account -> percent of equity;
   - `$N` -> dollars; `N shares` -> shares;
   - a range (`3-5%`, `$2,000-3,000`, `10 to 20 shares`) -> midpoint;
   - missing, empty, or sized against something else (e.g. "half a standard allocation",
     "% of the position") -> `review`, no order.
5. Target shares = sign x size converted to whole shares at the reference price (rounded toward zero);
   sign = + for Buy/Overweight, - for Underweight/Sell.
6. Order qty = target - current. Zero -> `no_order`. Otherwise one market order, `time_in_force: opg`.

Reference price: latest daily close from upstream `get_stock_data` (the previous session's close at
08:00 New York).

## 7. Decision file `var/decisions/<D>.json`

```json
{
  "schema_version": "1.0",
  "run_id": "2026-10-19T12:00:00Z-ab12cd",
  "trade_date": "2026-10-19",
  "generated_at": "2026-10-19T12:41:07Z",
  "upstream_commit": "e8f5503",
  "models": {"deep": "claude-opus-5-5", "quick": "claude-sonnet-5-5"},
  "rounds": {"debate": 3, "risk": 3},
  "book_before": {"cash": 100000.0, "currency": "USD", "positions": []},
  "equity_before": 100000.0,
  "decisions": [
    {
      "symbol": "AAPL",
      "status": "order",
      "reason": "Overweight, trader Buy 5% of portfolio",
      "final_rating": "Overweight",
      "trader_action": "Buy",
      "trader_position_sizing": "5% of portfolio",
      "parsed_size": {"kind": "pct", "value": 5.0},
      "reference_price": 343.39,
      "current_qty": 0,
      "target_qty": 14,
      "order": {"side": "buy", "qty": 14, "type": "market", "time_in_force": "opg"},
      "research_plan": {"recommendation": "Overweight", "rationale": "...", "strategic_actions": "..."},
      "trader_proposal": {"action": "Buy", "reasoning": "...", "entry_price": null, "stop_loss": 325.0, "position_sizing": "5% of portfolio"},
      "portfolio_decision": {"rating": "Overweight", "executive_summary": "...", "investment_thesis": "...", "price_target": 360.0, "time_horizon": "1-3 months"},
      "state_file": "runs/2026-10-19/AAPL/state.json"
    }
  ]
}
```

- `status`: `order` | `no_order` | `review`. `order` is null unless `status` is `order`.
- `side` is the trade direction only; a sell that crosses from long to short is one order and
  `target_qty` shows the resulting position.
- `research_plan`, `trader_proposal`, `portfolio_decision` are the upstream models' JSON, or null when
  that node fell back to free text (the rendered text is then in the state file).
- The file is validated against its own Pydantic model before an atomic write.

## 8. Error handling (fail safe)

- Structured node output invalid: one re-spawn with the validation error; after that,
  `ma apply --freetext` (upstream behaviour of `invoke_structured_or_freetext`). For the Portfolio
  Manager that path reads the rating with upstream `parse_rating` (no rating -> `REVIEW`).
- Tool errors: upstream's `DATA_UNAVAILABLE` / `<... unavailable ...>` strings go to the agent.
- Crash mid-run: state files persist; rerunning the same day resumes from `ma next`. At finalize, a
  symbol without a Portfolio Manager decision gets `review`.
- `ma finalize` is idempotent: it refuses when the decision file exists.
- Missed run (Mac or app off): skipped; nothing to fill for that day.
- No broker credentials anywhere; the task cannot trade.

## 9. Files

```
desk_runner/
  __init__.py, cli.py, config.py, config.yaml, playbook.md
  state_store.py      # load/save AgentState JSON per symbol
  nodes.py            # node registry, stand-in LLMs, prompt/apply
  tools_bridge.py     # ma tool
  book.py             # virtual book and fills
  sizing.py           # section 6
  decision_file.py    # section 7 model + writer
  memory_bridge.py    # ma memory
  var/                # gitignored: runs/, decisions/, book.json, pending_fills.json, memory/, cache/
tests/desk_runner/
docs/superpowers/specs/2026-10-17-desk-runner-design.md
```

## 10. Testing

- Unit (pytest, no network, no LLM): size parser; sizing gate; book fills including long -> short
  crossings and average price; decision file model; idempotent finalize; `ma next` sequence for 3/3
  rounds; `ma prompt` / `ma apply` for every node against the upstream node functions with fake
  answers (the resulting state must equal what the upstream node produces).
- End-to-end dry run: a full day for 3 symbols with canned agent answers and stubbed data tools.
- Lint: `ruff` (the fork's tooling).
- Acceptance: one manual run of the scheduled task, reviewed together, before the schedule is enabled.

## 11. Out of scope

Trading or calling Alpaca; feeding the JSON into the AITradingAgent strategy engine; restoring
StockTwits or Polymarket; backtesting; changes to upstream agent code or prompts.

## 12. Known limitations

- Desktop scheduled runs only happen while the Mac and the Claude app run.
- Sub-agents are a different runtime than upstream's API calls (system prompt, tool context), so
  outputs will differ from an API run with the same models even though prompts are identical.
- Upstream's memory log measures outcomes from the first close after the trade date, while the
  virtual book fills at the open; the two are intentionally kept as upstream and section 5 define them.
- Run time and token use for 22 sequential turns per symbol are not measured yet (spike: ~7 min and
  ~900k tokens for 11 turns).
