# Desk Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the upstream TradingAgents pipeline daily for AAPL, GOOGL, AMZN as a Claude Desktop scheduled task whose agent turns are sub-agents, and write a JSON file of orders sized against a virtual book.

**Architecture:** A package `desk_runner/` inside the fork exposes a CLI `ma`. `ma prompt` and `ma apply` call the real upstream node functions with a stand-in chat model, so upstream code builds every prompt and every state update; the Opus orchestrator (playbook) only spawns sub-agents and moves files. Deterministic Python handles the virtual book, sizing, memory settlement split, and the decision file.

**Tech Stack:** Python 3.11+, upstream `tradingagents` (same tree, `e8f5503`), langchain-core / langgraph (already upstream deps), Pydantic v2, PyYAML, typer, exchange-calendars, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-17-desk-runner-design.md`

## Global Constraints

- Upstream files are not edited, except `pyproject.toml` (package, deps, script) and `.gitignore`.
- Never copy upstream prompts, schemas or state shapes; import them from `tradingagents`.
- Symbols `[AAPL, GOOGL, AMZN]`; `max_debate_rounds: 3`; `max_risk_discuss_rounds: 3`; `max_tool_rounds: 20`; `initial_cash: 100000`; `currency: USD`; models `deep: claude-opus-5-5`, `quick: claude-sonnet-5-5`.
- Deep (Opus) nodes: `research_manager`, `portfolio_manager`. All other nodes and reflections: quick (Sonnet).
- Secrets only from the environment (`.env`, gitignored); `FRED_API_KEY` documented in `.env.example`.
- Invalid or missing config: the CLI refuses to run with a clear error.
- Runtime files under `desk_runner/var/` (gitignored). Writes of JSON files are atomic (temp file + `os.replace`).
- Trade date default = today in `America/New_York`; every command takes `--date YYYY-MM-DD`.
- No broker access and no orders sent anywhere.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Work on branch `feat/desk-runner`, PR to the fork's `main`.
- Lint: `.venv/bin/ruff check desk_runner tests/desk_runner` must be clean (repo's ruff config).

## Review Focus

1. Sub-agent answers a structured node with JSON wrapped in prose or a ```json fence → `ma apply` must extract the single JSON object (fenced or bare) before validating; prose with no object is a validation error. Test in Task 4.
2. Orchestrator calls `ma prompt`/`ma apply` for a node that is not next (or twice) → refuse with a clear error, state unchanged. Test in Task 4.
3. Bare size text like `"5%"` or `"5"` with no basis → `review` (spec only accepts % of portfolio/equity/capital/account). Test in Task 9.
4. Rerun on the same day before the open: pending fills dated today must stay pending (open not known yet), never filled at a guessed price. Test in Task 8.
5. Two analysts of one symbol applying at the same time → no lost update in `state.json` (file lock). Test in Task 3.

---

### Task 1: Scaffold, config, CLI skeleton

**Files:**
- Modify: `pyproject.toml` (add to `dependencies`: `pyyaml>=6.0`, `exchange-calendars>=4.5`; add `include` entry `desk_runner*`; `[project.scripts]` add `ma = "desk_runner.cli:app"`; package-data `desk_runner = ["config.yaml", "playbook.md"]`)
- Modify: `.gitignore` (add `desk_runner/var/`, `.env`)
- Create: `.env.example` (`FRED_API_KEY=`), `desk_runner/__init__.py`, `desk_runner/config.yaml` (values from Global Constraints plus `var_dir: desk_runner/var`), `desk_runner/config.py`, `desk_runner/cli.py`
- Test: `tests/desk_runner/__init__.py`, `tests/desk_runner/test_config.py`

**Interfaces:**
- Produces:
  - `class ModelsConfig(BaseModel)`: `deep: str`, `quick: str`
  - `class DeskConfig(BaseModel)` (`extra="forbid"`): `symbols: list[str]` (min 1, uppercase, unique), `max_debate_rounds: int >= 1`, `max_risk_discuss_rounds: int >= 1`, `max_tool_rounds: int >= 1`, `initial_cash: float > 0`, `currency: str`, `models: ModelsConfig`, `var_dir: Path`
  - `load_config(path: Path | None = None) -> DeskConfig` — path from arg, else env `DESK_RUNNER_CONFIG`, else `desk_runner/config.yaml`; raises `ConfigError(message)` on missing file, YAML error or validation error.
  - `class Paths` (frozen dataclass from `var_dir`): `run_dir(date) -> Path` (`var/runs/<date>`), `symbol_dir(date, symbol)`, `state_file(date, symbol)` (`.../state.json`), `book_file` (`var/book.json`), `pending_fills_file` (`var/pending_fills.json`), `decisions_file(date)` (`var/decisions/<date>.json`), `memory_log` (`var/memory/trading_memory.md`), `cache_dir` (`var/cache`)
  - `upstream_config(cfg: DeskConfig, paths: Paths) -> dict` — `copy.deepcopy(DEFAULT_CONFIG)` with `max_debate_rounds`, `max_risk_discuss_rounds`, `max_tool_rounds`, `data_cache_dir`, `memory_log_path` overridden.
  - `write_json_atomic(path: Path, data: Any) -> None`
  - `today_ny() -> str`
  - `cli.py`: `app = typer.Typer()`; a callback that runs `load_dotenv()`, loads config (exit code 2 with the `ConfigError` message on failure) and calls `tradingagents.dataflows.config.set_config(upstream_config(...))`; command `ma log <message> [--date]` appending `"<UTC ISO>Z <message>"` to `run_dir(date)/timing.log`.

- [ ] **Step 1: Create branch and venv**

Run: `git switch main && git switch -c feat/desk-runner && uv venv && uv pip install -e ".[dev]"`
Expected: `.venv/bin/python -c "import tradingagents"` exits 0.

- [ ] **Step 2: Write failing tests** in `tests/desk_runner/test_config.py`:
  - `test_shipped_config_loads`: `load_config(Path("desk_runner/config.yaml"))` → `symbols == ["AAPL","GOOGL","AMZN"]`, `max_debate_rounds == 3`, `max_risk_discuss_rounds == 3`, `initial_cash == 100000`, `models.deep == "claude-opus-5-5"`, `models.quick == "claude-sonnet-5-5"`.
  - `test_missing_file_raises`, `test_unknown_key_raises`, `test_zero_rounds_raises`, `test_duplicate_symbols_raises` → `pytest.raises(ConfigError)`.
  - `test_env_var_path(monkeypatch, tmp_path)`: `DESK_RUNNER_CONFIG` pointing at a copy with `initial_cash: 5` → loaded value 5.
  - `test_upstream_config_overrides`: `upstream_config(cfg, Paths(tmp_path))["max_risk_discuss_rounds"] == 3` and `memory_log_path` ends with `memory/trading_memory.md`.
  - `test_cli_refuses_bad_config(tmp_path)`: typer `CliRunner` with `DESK_RUNNER_CONFIG` → invalid file, `ma log x` exit code 2, message mentions the file.

- [ ] **Step 3: Run** `.venv/bin/pytest tests/desk_runner/test_config.py -v` → FAIL (module missing).
- [ ] **Step 4: Implement** the files above.
- [ ] **Step 5: Run** the tests → PASS; `.venv/bin/ruff check desk_runner tests/desk_runner` → clean.
- [ ] **Step 6: Commit** `feat(desk_runner): scaffold package, config and CLI skeleton`.

---

### Task 2: Stand-in chat model

**Files:**
- Create: `desk_runner/standin.py`
- Test: `tests/desk_runner/test_standin.py`

**Interfaces:**
- Produces:
  - `class StandInChatModel(BaseChatModel)`: constructed with `answer: Callable[[list[BaseMessage]], str]` and `structured: Callable[[list[BaseMessage], type[BaseModel]], BaseModel] | None = None`. Records every call in `self.calls: list[list[BaseMessage]]` and every structured request schema in `self.schemas: list[type[BaseModel]]`. `_generate` returns `AIMessage(content=answer(messages))` (never tool calls). `bind_tools(tools, **kw)` records `self.bound_tools = list(tools)` and returns `self`. `with_structured_output(schema, **kw)` returns a `RunnableLambda` that converts its input to messages (string → one `HumanMessage`, list of dicts/messages → messages), records the call and schema, and returns `structured(messages, schema)`; if `structured` is None it raises `NotImplementedError` at call time.
  - `render_messages(messages: list[BaseMessage]) -> str` — `"### SYSTEM\n...\n\n### HUMAN\n..."` (role headers `SYSTEM`/`HUMAN`/`AI`), content as text.
  - `prompt_hash(text: str) -> str` — sha256 hex.
  - `extract_json_object(text: str) -> str` — the single JSON object in `text`: a ```json fenced block if present, else the outermost `{...}` span; raises `ValueError` if none.

- [ ] **Step 1: Write failing tests:**
  - `test_invoke_with_string_records_human_message`: `StandInChatModel(answer=lambda m: "hi").invoke("q")` → `.content == "hi"`, `calls[0][0].content == "q"`.
  - `test_prompt_pipe_bind_tools`: `(ChatPromptTemplate.from_messages([("system","S {x}"), MessagesPlaceholder("messages")]).partial(x="1") | model.bind_tools([some_tool])).invoke([("human","AAPL")])` → content is the answer, `render_messages(model.calls[0])` starts with `"### SYSTEM\nS 1"`, `model.bound_tools == [some_tool]`.
  - `test_structured_returns_model`: with `structured=lambda m, s: s.model_validate({"action":"Hold","reasoning":"r"})`, `model.with_structured_output(TraderProposal).invoke([{"role":"user","content":"x"}])` is a `TraderProposal` with `action.value == "Hold"`; `model.schemas == [TraderProposal]`.
  - `test_extract_json_object`: fenced, bare-with-prose (`'Here: {"a": 1} done'` → `'{"a": 1}'`), and no-object (`pytest.raises(ValueError)`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `standin.py`.
- [ ] **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): stand-in chat model for capturing and replaying turns`.

---

### Task 3: State store

**Files:**
- Create: `desk_runner/state_store.py`
- Test: `tests/desk_runner/test_state_store.py`

**Interfaces:**
- Consumes: `Paths`, `write_json_atomic` (Task 1).
- Produces:
  - `init_state(paths: Paths, date: str, symbol: str, portfolio_context: str, past_context: str, instrument_context: str) -> dict` — `Propagator().create_initial_state(symbol, date, past_context=..., instrument_context=..., portfolio_context=...)`, saved to `state_file`; refuses (raises `StateError`) if the file exists.
  - `load_state(paths, date, symbol) -> dict` — messages rebuilt with `messages_from_dict`; the initial `("human", symbol)` tuple is stored as a `HumanMessage`.
  - `update_state(paths, date, symbol, fn: Callable[[dict], dict]) -> dict` — under `tradingagents.dataflows.files.locked(state_file)`: load, `update = fn(state)`, merge, save atomically, return the new state. Merge rule: `messages` combined with `langgraph.graph.message.add_messages`; every other key replaced.
  - `class StateError(Exception)`.
  - The CLI command `ma state init` is added in Task 12, once the book (Task 8) and memory (Task 11) exist.

- [ ] **Step 1: Write failing tests:**
  - `test_init_matches_propagator`: `init_state(...)` equals `Propagator().create_initial_state(...)` after a JSON round trip (`load_state`), with `messages[0].content == "AAPL"`.
  - `test_init_refuses_existing` → `StateError`.
  - `test_update_appends_messages_and_replaces_keys`: update returning `{"messages":[AIMessage("t")], "market_report":"R"}` → two messages, `market_report == "R"`.
  - `test_concurrent_updates_not_lost`: four threads each `update_state` setting a different report key (`market_report`, `sentiment_report`, `news_report`, `fundamentals_report`) after a `time.sleep(0.05)` inside `fn` → all four keys set in the final file.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `state_store.py`.
- [ ] **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): per-symbol AgentState store with locking`.

---

### Task 4: Node registry, next, prompt, apply

**Files:**
- Create: `desk_runner/nodes.py`
- Modify: `desk_runner/cli.py` (commands `ma next`, `ma prompt`, `ma apply`)
- Test: `tests/desk_runner/test_nodes.py`

**Interfaces:**
- Consumes: `StandInChatModel`, `render_messages`, `prompt_hash`, `extract_json_object` (Task 2); `load_state`, `update_state` (Task 3).
- Produces:
  - `NODES: dict[str, NodeSpec]` with keys and upstream factories: `market` (`create_market_analyst`, report `market_report`), `social` (`create_sentiment_analyst`, `sentiment_report`, schema `SentimentReport`), `news` (`create_news_analyst`, `news_report`), `fundamentals` (`create_fundamentals_analyst`, `fundamentals_report`), `bull`, `bear`, `research_manager` (schema `ResearchPlan`), `trader` (schema `TraderProposal`), `aggressive`, `conservative`, `neutral`, `portfolio_manager` (schema `PortfolioDecision`). `NodeSpec` fields: `key`, `factory`, `tier: Literal["deep","quick"]`, `schema: type[BaseModel] | None`, `report_key: str | None` (analysts only), `tools: tuple` (analysts' upstream `TOOLS`; empty for `social`).
  - `next_nodes(state: dict, cfg: DeskConfig) -> list[str]`: analysts whose report is empty (all of them together, in `NODES` order); else, if `investment_plan` empty, map `ConditionalLogic(cfg.max_debate_rounds, cfg.max_risk_discuss_rounds).should_continue_debate(state)` (`"Bull Researcher"→"bull"`, `"Bear Researcher"→"bear"`, `"Research Manager"→"research_manager"`); else `["trader"]` if `trader_investment_plan` empty; else, if `final_trade_decision` empty, map `should_continue_risk_analysis` (`Aggressive/Conservative/Neutral Analyst`, `"Portfolio Manager"`); else `[]` (done).
  - `build_prompt(paths, date, symbol, node, cfg) -> PromptFile` — refuses (`NodeError`) if `node not in next_nodes(state)`. Runs the node on the loaded state with a capturing stand-in (answer `""`; structured callable raising so nothing is applied) and writes `symbol_dir/turns/<NN>_<node>.prompt.md` (NN = 2-digit turn counter, 01-based, from existing prompt files) with: the rendered prompt; for analysts with tools, a `## Tools` section listing each tool's `name`, `description` and `tool_call_schema.model_json_schema()` (injected args excluded) and the instruction to call them only via `ma tool`; for structured nodes, a `## Output format` section with `schema.model_json_schema()` and "Answer with one JSON object matching this schema." Also writes `<NN>_<node>.meta.json` `{node, tier, model, prompt_sha256, answer_file}` (`model` is the Agent-tool alias: `"opus"` for deep, `"sonnet"` for quick) where `answer_file = <NN>_<node>.answer.md`. Returns `PromptFile(prompt_path, answer_path, model)`.
  - Sentiment: during `build_prompt` for `social`, patch `sentiment_analyst.get_news` (object with `.func`), `fetch_stocktwits_messages`, `fetch_reddit_posts` with recording wrappers; save results to `symbol_dir/sentiment_inputs.json`. During `apply_answer` for `social`, patch the same names to return the saved values (fail with `NodeError` if the file is missing).
  - `apply_answer(paths, date, symbol, node, cfg, freetext: bool = False) -> dict` — refuses if `node not in next_nodes(state)` or there is no meta file for the latest turn of that node. Reads the answer file (empty → `NodeError`). Re-runs the node with a stand-in whose `answer` returns the answer text and whose `structured` parses `extract_json_object(answer)` with `schema.model_validate_json`; on `ValidationError`/`ValueError` raises `NodeError` with the error text, unless `freetext=True`, in which case `structured` raises so upstream's free-text fallback is taken. Verifies `prompt_hash(render_messages(<first captured call>))` (a structured miss makes upstream call again with the same prompt; the first call is the one compared) equals the meta's `prompt_sha256` (else `NodeError("state changed since prompt")`). Analysts: merges only `{report_key: update[report_key]}` (mirrors the analyst subgraph's `output_schema`); report empty → `NodeError`. Other nodes: merges the node's returned dict. On a successful structured parse, writes the model JSON to `symbol_dir/structured/<node>.json`. Returns the merged state.
  - CLI: `ma next <symbol>` prints `{"nodes": [...], "done": bool}`; `ma prompt <symbol> <node>` prints the `PromptFile` as JSON; `ma apply <symbol> <node> [--freetext]` prints `{"ok": true, "next": [...]}` or exits 1 with `{"ok": false, "error": "..."}`.
  - `class NodeError(Exception)`.

- [ ] **Step 1: Write failing tests** (fixtures: tmp `Paths`, a state from `init_state`, analysts' reports pre-filled when testing later nodes; network-free: patch sentiment fetchers):
  - `test_next_sequence_3_rounds`: starting with all reports filled, repeatedly apply canned answers and collect `next_nodes` → `["bull","bear"]*3 + ["research_manager","trader"] + ["aggressive","conservative","neutral"]*3 + ["portfolio_manager"]`, then `[]`.
  - `test_next_returns_all_missing_analysts`: fresh state → `["market","social","news","fundamentals"]`; after market applied → the other three.
  - `test_prompt_is_upstream_prompt`: `build_prompt(..., "bull")` file contains the upstream bull text `"You are a Bull Analyst advocating for investing"` and the state's `market_report`.
  - `test_structured_prompt_has_schema`: prompt for `trader` contains `"## Output format"` and `"position_sizing"`.
  - `test_apply_debate_updates_state`: after bull answer `"case"`, `investment_debate_state["current_response"] == "Bull Analyst: case"`, `count == 1`.
  - `test_apply_structured_fenced_json`: trader answer ```` ```json {"action":"Buy","reasoning":"r","position_sizing":"5% of portfolio"} ``` ```` → `trader_investment_plan` contains `"FINAL TRANSACTION PROPOSAL: **BUY**"` and `structured/trader.json` exists.
  - `test_apply_structured_invalid_raises_then_freetext`: answer `"no json here"` → `NodeError`; with `freetext=True` → `trader_investment_plan == "no json here"`, no `structured/trader.json`.
  - `test_pm_freetext_rating`: PM answer `"**Rating**: Sell\n..."` with `freetext=True` → `final_rating == "Sell"`.
  - `test_wrong_node_refused`: `build_prompt(..., "trader")` while debate pending → `NodeError`, state file unchanged.
  - `test_apply_without_prompt_refused` → `NodeError`.
  - `test_sentiment_apply_reuses_cached_inputs`: fetchers patched to return `"N1"`, `"S1"`, `"R1"` at prompt time and to raise at apply time → apply succeeds; `sentiment_report` starts with `"**Overall Sentiment:**"`.
  - `test_analyst_merges_only_report`: market apply → state has `market_report`, message count unchanged.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `nodes.py` and the three CLI commands. Upstream factories are called with the stand-in model, e.g. `NODES["bull"].factory(model)(state)`.
- [ ] **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): next/prompt/apply through upstream node functions`.

---

### Task 5: Parity test against the upstream graph

**Files:**
- Test: `tests/desk_runner/test_parity.py`

**Interfaces:**
- Consumes: Task 3 and Task 4 functions; upstream `GraphSetup`, `ConditionalLogic`, `Propagator`.

- [ ] **Step 1: Write the test** `test_cli_path_equals_upstream_graph`:
  - One `canned(messages) -> str` function picks an answer by role marker in the rendered prompt (e.g. `"You are a Bull Analyst"` → `f"bull turn {n}"` with a per-role counter; analysts' system prompts → a fixed report; structured roles → a fixed valid JSON per schema).
  - Upstream: `GraphSetup(quick, deep, ConditionalLogic(3, 3), 20).setup_graph(("market","social","news","fundamentals")).compile()` with `quick = deep = StandInChatModel(answer=canned, structured=parse_with_canned)`, invoked on `Propagator().create_initial_state("AAPL","2026-10-19", portfolio_context="P", instrument_context="I", past_context="L")` with sentiment fetchers patched.
  - Desk runner: same initial state via `init_state`, then loop `next_nodes` → `build_prompt` → write `canned(...)` answer to the answer file → `apply_answer` until done.
  - Assert equality of `market_report`, `sentiment_report`, `news_report`, `fundamentals_report`, `investment_debate_state`, `investment_plan`, `trader_investment_plan`, `risk_debate_state`, `final_trade_decision`, `final_rating`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/desk_runner/test_parity.py -v` → PASS (fix `nodes.py` if not; the upstream graph is the reference).
- [ ] **Step 3: Commit** `test(desk_runner): parity with the upstream LangGraph pipeline`.

---

### Task 6: Tools bridge

**Files:**
- Create: `desk_runner/tools_bridge.py`
- Modify: `desk_runner/cli.py` (command `ma tool <symbol> <analyst> <tool> <json-args>`)
- Test: `tests/desk_runner/test_tools_bridge.py`

**Interfaces:**
- Consumes: `NODES` (Task 4), `load_state` (Task 3).
- Produces:
  - `run_tool(paths, date, symbol, analyst: str, tool_name: str, args: dict, cfg) -> str` — `tool_name` must be in `NODES[analyst].tools` (else `ToolBridgeError`). Each call appends `{ts, analyst, tool, args}` to `symbol_dir/tool_calls.jsonl`; if that analyst already has `cfg.max_tool_rounds` calls, returns `tradingagents.agents.analysts.turn.WRAP_UP` without calling. Injected parameters: for every parameter of `tool.func` whose `Annotated` metadata contains an `InjectedState(field)`, pass `state[field]`; then call `tool.func(**args, **injected)` and return its string result.
  - `class ToolBridgeError(Exception)`.
  - CLI prints the tool's string output to stdout; errors exit 1 with the message.

- [ ] **Step 1: Write failing tests:**
  - `test_injects_symbol_and_trade_date`: monkeypatch `tradingagents.agents.tools.route_to_vendor` to capture args; `run_tool(..., "market", "get_stock_data", {"start_date":"2026-09-01","end_date":"2026-10-19"})` → captured symbol `"AAPL"`, end date clamped by upstream `as_of_window` to the trade date.
  - `test_tool_not_allowed_for_analyst`: `run_tool(..., "market", "get_news", ...)` → `ToolBridgeError`.
  - `test_wrap_up_after_max_rounds`: with `max_tool_rounds=2`, third call returns `WRAP_UP` and the vendor is not called.
  - `test_model_cannot_override_injected`: args containing `"symbol": "MSFT"` → `ToolBridgeError("injected argument")`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): analysts call upstream tools via ma tool`.

---

### Task 7: Market data helpers

**Files:**
- Create: `desk_runner/market.py`
- Test: `tests/desk_runner/test_market.py`

**Interfaces:**
- Produces:
  - `is_trading_day(date: str) -> bool` — `exchange_calendars.get_calendar("XNYS").is_session(date)`.
  - `last_close(symbol: str, trade_date: str) -> float` — parses the CSV returned by upstream `route_to_vendor("get_stock_data", symbol, <trade_date - 10 days>, trade_date)`; the last row with a date `< trade_date` and a positive `Close`; raises `MarketDataError` if none.
  - `open_on(symbol: str, date: str) -> float | None` — same source; the `Open` of the row dated `date`, or `None` if that row is absent or non-positive.
  - `class MarketDataError(Exception)`.

- [ ] **Step 1: Write failing tests** (monkeypatch `route_to_vendor` with a CSV string in upstream's format: `"# Stock data ...\n\nDate,Open,High,Low,Close,Volume,Dividends,Stock Splits\n2026-10-15,330,...,333.1,...\n2026-10-16,334,...,343.39,..."`):
  - `test_last_close_excludes_trade_date_row`: with rows for 10-16 and 10-19, `last_close("AAPL","2026-10-19") == 343.39` (the 10-16 close).
  - `test_open_on_present_and_missing`: `open_on("AAPL","2026-10-16") == 334.0`; `open_on("AAPL","2026-10-19") is None` when the row is absent.
  - `test_last_close_no_data_raises` → `MarketDataError`.
  - `test_trading_days`: `is_trading_day("2026-10-19") is True` (Monday), `"2026-10-17"` False (Saturday), `"2026-11-26"` False (Thanksgiving).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): NYSE calendar and price helpers`.

---

### Task 8: Virtual book and fills

**Files:**
- Create: `desk_runner/book.py`
- Test: `tests/desk_runner/test_book.py`

**Interfaces:**
- Consumes: `Paths`, `write_json_atomic` (Task 1); `open_on` (Task 7, injected as a callable for tests).
- Produces:
  - `load_book(paths, cfg) -> PortfolioContext` — upstream `tradingagents.portfolio.PortfolioContext`; creates `{cash: initial_cash, currency, positions: []}` if the file is missing.
  - `save_book(paths, book) -> None`.
  - `apply_fill(book, symbol, side: Literal["buy","sell"], qty: int, price: float) -> PortfolioContext` — returns a new book. Buy: `cash -= qty*price`; sell: `cash += qty*price`. Signed quantity `q' = q ± qty`. `average_price`: same-direction growth → weighted average of `|q|` and `qty`; shrink toward zero → unchanged; crossing zero → fill price; `q' == 0` → position removed.
  - `equity(book, prices: dict[str, float]) -> float` — `cash + Σ quantity * prices[ticker]`.
  - `render_for_agents(book, symbol) -> str` — `book.render(symbol)` + `"\n- Short positions are allowed (negative quantity = short)."`.
  - `PendingFill` (Pydantic): `trade_date: str`, `symbol: str`, `side`, `qty: int`.
  - `add_pending(paths, fills: list[PendingFill]) -> None`.
  - `process_pending(paths, cfg, today: str, open_price: Callable[[str, str], float | None]) -> FillReport` — fills only entries with `trade_date < today` whose open is known; others stay pending. `FillReport`: `filled: list[PendingFill]`, `still_pending: list[PendingFill]`, `missing_price: list[PendingFill]`.

- [ ] **Step 1: Write failing tests:**
  - `test_new_book_starts_with_initial_cash`: `load_book` → `cash == 100000`, no positions.
  - `test_buy_then_add_weighted_average`: buy 10 @100, buy 10 @110 → qty 20, avg 105, cash 97900.
  - `test_long_to_short_crossing`: from +10 @100, sell 15 @120 → qty -5, avg 120, cash +1800.
  - `test_short_cover_to_zero_removes`: from -5 @120, buy 5 @110 → no position.
  - `test_equity_counts_shorts_negative`: cash 101800, AAPL -5, price 130 → 101150.
  - `test_render_mentions_shorts`: `"Short positions are allowed"` in `render_for_agents(book, "AAPL")`.
  - `test_same_day_fill_stays_pending` (Review Focus 4): pending dated `today` → `still_pending`, book unchanged, `open_price` not called.
  - `test_missing_open_stays_pending`: `open_price` returns None for a past date → in `missing_price`, still in the pending file.
  - `test_process_pending_fills_past_dates`: past fill with open 334 → book updated and pending file emptied.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): virtual book with signed positions and next-open fills`.

---

### Task 9: Sizing

**Files:**
- Create: `desk_runner/sizing.py`
- Test: `tests/desk_runner/test_sizing.py`

**Interfaces:**
- Produces:
  - `ParsedSize` (Pydantic): `kind: Literal["pct","usd","shares"]`, `value: float`.
  - `parse_size(text: str | None) -> ParsedSize | None` — regex only. Accepts `N%` / `N percent` followed within the same clause by `of (the )?(total )?(portfolio|equity|capital|account)`; `$N` (commas allowed, `k` suffix = ×1000); `N shares`. Ranges `A-B`, `A–B`, `A to B` → midpoint. Returns `None` for missing text, bare numbers or bare percentages, other bases ("of the position", "standard allocation"), or more than one distinct size.
  - `SizingResult` (Pydantic): `status: Literal["order","no_order","review"]`, `reason: str`, `parsed_size: ParsedSize | None`, `target_qty: int | None`, `order: Order | None`. `Order`: `side: Literal["buy","sell"]`, `qty: int > 0`, `type: Literal["market"] = "market"`, `time_in_force: Literal["opg"] = "opg"`.
  - `size_decision(final_rating: str, trader_action: str | None, position_sizing: str | None, current_qty: int, price: float, equity: float) -> SizingResult` — rules in spec §6, in order: rating `REVIEW` → review; trader action `Hold` or rating `Hold` → no_order (current position kept); opposite directions (Buy vs Underweight/Sell, Sell vs Buy/Overweight) → review; `trader_action is None` (free-text trader) or `parse_size` None → review; target = sign × shares (`pct`: `trunc(value/100*equity/price)`, `usd`: `trunc(value/price)`, `shares`: `trunc(value)`), sign + for Buy/Overweight, − for Underweight/Sell; `order_qty = target - current_qty`; 0 → no_order; else order side `buy` if positive else `sell`, `qty = abs`.

- [ ] **Step 1: Write failing tests:**
  - `test_parse_variants` (parametrized): `"5% of portfolio"`→pct 5; `"3-5% of portfolio"`→pct 4; `"2.5 percent of total equity"`→pct 2.5; `"$2,000"`→usd 2000; `"$2k-3k"`→usd 2500; `"10 to 20 shares"`→shares 15; `"14 shares"`→shares 14.
  - `test_parse_rejects` (parametrized, all → None, Review Focus 3): `None`, `""`, `"5%"`, `"5"`, `"half a standard allocation"`, `"25% of the position"`, `"5% of portfolio, or $3000"`.
  - `test_overweight_buy_opens_long`: rating Overweight, action Buy, `"5% of portfolio"`, current 0, price 343.39, equity 100000 → target 14, order buy 14.
  - `test_sell_crosses_to_short`: rating Sell, action Sell, `"10% of portfolio"`, current 14, price 343.39, equity 100000 → target -29, order sell 43.
  - `test_hold_keeps_position`: rating Hold, action Buy → no_order, `order is None`.
  - `test_trader_hold_no_order`: rating Buy, action Hold → no_order.
  - `test_disagreement_review`: rating Buy, action Sell → review.
  - `test_review_rating`: rating `REVIEW` → review.
  - `test_freetext_trader_review`: action None → review.
  - `test_target_equals_current_no_order`: target 14, current 14 → no_order.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): deterministic sizing from trader text and PM rating`.

---

### Task 10: Decision file and finalize

**Files:**
- Create: `desk_runner/decision_file.py`, `desk_runner/finalize.py`
- Modify: `desk_runner/cli.py` (command `ma finalize [--date]`)
- Test: `tests/desk_runner/test_finalize.py`

**Interfaces:**
- Consumes: `load_state` (Task 3), `last_close` (Task 7), `load_book`, `equity`, `add_pending`, `PendingFill` (Task 8), `size_decision`, `ParsedSize`, `Order` (Task 9), upstream `ResearchPlan`, `TraderProposal`, `PortfolioDecision`, `PortfolioContext`, `TradingMemoryLog`.
- Produces:
  - `SymbolDecision` and `DecisionFile` Pydantic models with exactly the fields in spec §7 (`schema_version: Literal["1.0"]`, `run_id`, `trade_date`, `generated_at`, `upstream_commit: Literal["e8f5503"]`, `models`, `rounds`, `book_before: PortfolioContext`, `equity_before: float`, `decisions: list[SymbolDecision]`; per symbol: `symbol`, `status`, `reason`, `final_rating`, `trader_action: str | None`, `trader_position_sizing: str | None`, `parsed_size`, `reference_price: float | None`, `current_qty: int`, `target_qty: int | None`, `order: Order | None`, `research_plan: ResearchPlan | None`, `trader_proposal: TraderProposal | None`, `portfolio_decision: PortfolioDecision | None`, `state_file: str`). Validator: `order` is set iff `status == "order"`.
  - `new_run_id(now: datetime) -> str` — `"<YYYY-MM-DDTHH:MM:SSZ>-<6 hex>"`.
  - `finalize(paths, cfg, date, price: Callable[[str, str], float] = last_close, now: Callable[[], datetime] = utcnow) -> DecisionFile` — refuses (`FinalizeError`) if `decisions_file(date)` exists. Per symbol: state missing or `final_trade_decision` empty → `review` "pipeline incomplete"; else read `structured/{research_manager,trader,portfolio_manager}.json` when present (None otherwise), `final_rating` from state, trader action/sizing from `structured/trader.json` (None if absent), price via `price(symbol, date)` (`MarketDataError` → `review` "no reference price"), equity from the book at those prices, then `size_decision`. Writes the decision file atomically, adds pending fills for every `order`, and calls `TradingMemoryLog(upstream_config(...)).store_decision(symbol, date, final_trade_decision, rating=final_rating)` for symbols with a final decision.
  - `class FinalizeError(Exception)`.
  - CLI prints a one-line summary per symbol (`AAPL order buy 14`, `GOOGL review: ...`).

- [ ] **Step 1: Write failing tests** (states built by writing `state.json` and `structured/*.json` directly; `price` stub returns 343.39/250/220):
  - `test_finalize_writes_valid_file`: three complete symbols → file exists, `DecisionFile.model_validate_json` passes, AAPL order buy 14, `book_before.cash == 100000`.
  - `test_finalize_adds_pending_and_memory_entries`: pending file has one entry per order; memory log contains `"[<date> | AAPL | Overweight | pending]"`.
  - `test_incomplete_symbol_is_review`: GOOGL without `final_trade_decision` → review "pipeline incomplete", no memory entry for GOOGL.
  - `test_missing_price_is_review`: price stub raises `MarketDataError` for AMZN → review.
  - `test_finalize_is_idempotent`: second call → `FinalizeError`, file unchanged.
  - `test_order_iff_status_order`: constructing `SymbolDecision(status="review", order=Order(...))` raises.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): decision file, finalize and pending fills`.

---

### Task 11: Memory bridge

**Files:**
- Create: `desk_runner/memory_bridge.py`
- Modify: `desk_runner/cli.py` (commands `ma memory pending`, `ma memory settle`)
- Test: `tests/desk_runner/test_memory_bridge.py`

**Interfaces:**
- Consumes: `StandInChatModel`, `render_messages`, `prompt_hash` (Task 2); upstream `settlement.settle_all_pending`, `Reflector`, `TradingMemoryLog`.
- Produces:
  - `pending_reflections(paths, cfg, date) -> list[Path]` — runs `settle_all_pending(log, Reflector(capture), upstream_config, wait=True)` with a stand-in whose `answer` records the messages and raises `RuntimeError("deferred")`, so upstream leaves every entry pending. Writes one `run_dir/memory/<ticker>_<entry-date>.prompt.md` per captured call (rendered messages) plus `.meta.json` `{prompt_sha256, answer_file}`; returns the prompt paths. Entries whose window has not traded are skipped by upstream.
  - `settle_reflections(paths, cfg, date) -> list[tuple[str, str]]` — runs `settle_all_pending` again with a stand-in whose `answer` looks up the answer file by `prompt_hash(render_messages(messages))` among this run's meta files; no answer for a hash → raise (upstream keeps that entry pending). Returns upstream's `Settlement.settled`.
  - `past_context(paths, cfg, symbol, date) -> str` — `TradingMemoryLog(...).get_past_context(symbol, as_of=date if is_historical(date) else None)`.

- [ ] **Step 1: Write failing tests** (memory log seeded with a pending entry `[2026-10-05 | AAPL | Buy | pending]`; monkeypatch `settlement.fetch_returns` to return `(0.03, 0.01, 5, "2026-10-12")`):
  - `test_pending_writes_prompt_and_keeps_entry_pending`: one prompt file containing `"Raw return over 5 trading days: +3.0%"`; log entry still pending.
  - `test_settle_uses_answer`: write `"Lesson text."` to the answer file → `settle_reflections` returns `[("AAPL","2026-10-05")]`; log contains `"Lesson text."` and no longer `"| pending]"` for that entry.
  - `test_settle_without_answer_keeps_pending` → returns `[]`, entry still pending.
  - `test_past_context_includes_lesson`: after settlement, `past_context(..., "AAPL", "2026-10-19")` contains `"Lesson text."`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): memory settlement with sub-agent reflections`.

---

### Task 12: `ma init`, `ma state init`, end-to-end dry run

**Files:**
- Modify: `desk_runner/cli.py` (commands `ma init`, `ma state init <symbol>`)
- Test: `tests/desk_runner/test_e2e_dry_run.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `ma init [--date]`: not a trading day → prints `{"trading_day": false}` and exits 0 without creating anything; else `process_pending(...)` with `open_on`, creates `run_dir`, prints `{"trading_day": true, "fills": FillReport, "symbols": cfg.symbols}`.
  - `ma state init <symbol> [--date]`: `init_state(..., portfolio_context=render_for_agents(load_book(...), symbol), past_context=past_context(...), instrument_context=build_instrument_context(symbol, "stock", resolve_instrument_identity(symbol), date))`.

- [ ] **Step 1: Write the test** `test_full_day_three_symbols` (CliRunner; `DESK_RUNNER_CONFIG` → tmp copy with `var_dir` in `tmp_path`; patched: `is_trading_day` True, `resolve_instrument_identity` → `{"company_name": "X"}`, sentiment fetchers, `last_close`, `open_on`; `canned` answers from Task 5):
  - Day 1 `2026-10-19`: `ma init`, `ma memory pending`, `ma state init` ×3, loop `ma next`/`ma prompt`/write answer/`ma apply` per symbol until done, `ma finalize` → decision file valid, 3 entries; pending fills exist.
  - Day 2 `2026-10-20`: `ma init` → fills applied at the patched open; `book.json` positions match day 1 orders.
  - `test_init_non_trading_day`: `is_trading_day` False → `{"trading_day": false}`, no run dir.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** the two commands. **Step 4: Run** the whole suite `.venv/bin/pytest tests/desk_runner -v` → PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(desk_runner): init and state commands, end-to-end dry run`.

---

### Task 13: Playbook and README

**Files:**
- Create: `desk_runner/playbook.md`, `desk_runner/README.md`
- Test: `tests/desk_runner/test_playbook.py`

**Interfaces:**
- Consumes: CLI commands from Tasks 1-12.

- [ ] **Step 1: Write the failing test** `test_playbook_mentions_every_command`: playbook text contains `ma init`, `ma memory pending`, `ma memory settle`, `ma state init`, `ma next`, `ma prompt`, `ma apply`, `--freetext`, `ma tool`, `ma finalize`, `ma log`, `model: "sonnet"`, `model: "opus"`, and the sub-agent wrapper heading `## Sub-agent wrapper`.
- [ ] **Step 2: Write `playbook.md`** for the Opus orchestrator, in this order:
  1. `cd` to the fork; run `.venv/bin/ma init`; stop if `trading_day` is false. `ma log start`.
  2. `ma memory pending`; for each prompt file spawn a `general-purpose` sub-agent with `model: "sonnet"` using the wrapper; then `ma memory settle`.
  3. `ma state init <symbol>` for each symbol.
  4. For all symbols in parallel: loop `ma next <symbol>`; for each returned node (analysts together in one message) run `ma prompt <symbol> <node>`, spawn a fresh `general-purpose` sub-agent with `model` from the prompt result (`"opus"` for deep, `"sonnet"` for quick), then `ma apply <symbol> <node>`. On `ok: false`: spawn once more with the error appended; if it fails again, `ma apply --freetext`. Log each turn with `ma log`.
  5. `ma finalize`; `ma log end`; reply with the finalize summary.
  6. Rules: never edit files under `desk_runner/var/` by hand; never use Alpaca or any broker tool; never run anything except `.venv/bin/ma` and the sub-agents.

  `## Sub-agent wrapper` (exact text the orchestrator sends, with placeholders):
  ```
  You are one agent in a trading research pipeline. Read the prompt in {prompt_path} and answer it exactly as that prompt asks.
  {only for analysts with tools:} Get data only by running `.venv/bin/ma tool {symbol} {node} <tool_name> '<json args>'` with the tools listed in the prompt. Do not use web search or any other data source. Stop calling tools when a call returns the wrap-up message.
  {for all other nodes:} Use only the information in the prompt. Do not call tools or search the web.
  Write only your final answer to {answer_path} and nothing else to any other file. Reply "done" when written.
  ```
- [ ] **Step 3: Write `README.md`**: purpose, setup (`uv venv && uv pip install -e ".[dev]"`, `.env` with `FRED_API_KEY`), commands, file layout, decision file format pointer to the spec, known limitations from spec §12.
- [ ] **Step 4: Run** the test → PASS; full suite → PASS; ruff clean.
- [ ] **Step 5: Commit** `docs(desk_runner): orchestrator playbook and README`.

---

### Task 14: PR, scheduled task, acceptance run

- [ ] **Step 1:** `git push -u origin feat/desk-runner`; open a PR to `winterji/TradingAgents` `main` (`gh pr create --repo winterji/TradingAgents --base main`), body ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Run a review subagent and post its findings as PR comments. Do not merge.
- [ ] **Step 2:** Ask Jiří to put `FRED_API_KEY` in `.env` and to merge or approve running from the branch.
- [ ] **Step 3:** Create the Claude Desktop scheduled task: cron `0 14 * * 1-5`, working folder `/Users/winterji/Projects/TradingAgentsFork`, prompt `Follow desk_runner/playbook.md exactly.`, session model Opus; disabled if the tool allows, otherwise created and immediately run manually.
- [ ] **Step 4:** Run it once manually on a trading day (`run_scheduled_task`). Expected: `desk_runner/var/decisions/<date>.json` exists and validates; `timing.log` shows 22 turns per symbol; no files outside `desk_runner/var/` changed (`git status` clean apart from ignored files).
- [ ] **Step 5:** Review the run with Jiří (decision file, a sample of turn files, timing, token use). Enable the schedule only after his OK.
