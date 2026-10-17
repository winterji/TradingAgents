# Desk runner playbook

You are the orchestrator of the desk runner. Run it from the repository root
`/Users/winterji/Projects/TradingAgentsFork`. The day's analysis is a fixed sequence of
`.venv/bin/ma` commands. Sub-agents answer each prompt the commands write. Do not decide anything
about trades yourself: the commands and the upstream code decide.

## Rules

- Run only `.venv/bin/ma ...` through Bash, and the sub-agents through the Agent tool.
- Use `subagent_type: "general-purpose"` for every sub-agent. Set `model` from the prompt result
  (`model: "opus"` or `model: "sonnet"`). Memory reflections always use `model: "sonnet"`.
- Each sub-agent gets a fresh spawn. Never reuse a sub-agent for a second turn.
- Never edit any file under `desk_runner/var/` by hand. Never edit state files.
- Never use Alpaca, any broker tool, web search, or any other data source. Only `.venv/bin/ma`
  and the sub-agents.
- Never call `ma tool`. Only analyst sub-agents do.
- If a command fails and this playbook does not say to stop, log the failure with `ma log` and
  continue as described below.

## 1. Start the day

1. Run `.venv/bin/ma init`.
   - If it exits non-zero, stop and report the error.
   - If the output is `{"trading_day": false}`, stop and report "not a trading day". Do nothing else.
   - Otherwise keep the `symbols` list from the output for the rest of the run.
2. Run `.venv/bin/ma log "start"`.

## 2. Memory reflections

1. Run `.venv/bin/ma memory pending`. Its output is `{"prompts": [...]}`.
   - If it exits non-zero, run `.venv/bin/ma log "memory pending failed: <error>"`, skip to step 3.
     This is not fatal.
2. For each prompt path in `prompts`, spawn a sub-agent with `model: "sonnet"`, using the
   wrapper from section "Sub-agent wrapper" with the "all other nodes" line. The answer path is
   the prompt path with `.prompt.md` replaced by `.answer.md`. Spawn all reflections in one
   message so they run in parallel.
3. Run `.venv/bin/ma memory settle`.
   - If it exits non-zero, or a reflection answer is missing, run
     `.venv/bin/ma log "memory settle: <error or missing answer>"` and continue. This is not fatal.

## 3. Start each symbol

For each symbol from step 1, run `.venv/bin/ma state init <SYMBOL>`.
- If it exits non-zero, run `.venv/bin/ma log "state init failed for <SYMBOL>: <error>"` and drop
  that symbol from the rest of the run. Finalize marks it for review.

## 4. Run the analysts and debates

Process all symbols in parallel. Each symbol runs its own loop, and each loop is independent.

For one symbol, repeat until `ma next` returns `"done": true`:

1. Run `.venv/bin/ma next <SYMBOL>`. Its output is `{"nodes": [...], "done": bool}`.
   - If it exits non-zero, log it and stop processing this symbol.
   - If `done` is true, the symbol is finished. Leave the loop.
2. For each node in `nodes`, run `.venv/bin/ma prompt <SYMBOL> <NODE>`. Its output is
   `{"prompt_path": ..., "answer_path": ..., "model": ...}`.
   - If it exits non-zero, log it and stop processing this symbol.
3. Spawn one sub-agent per node with `model` set to the `model` from its prompt result. Use the
   wrapper with the right line for the node (see "Which wrapper line"). When `nodes` has several
   analysts (market, social, news, fundamentals), spawn them all in one message so they run in
   parallel. Wait until every sub-agent of this step has finished.
4. For each node, run `.venv/bin/ma apply <SYMBOL> <NODE>`.
   - If it prints `{"ok": true, ...}`, log it with `.venv/bin/ma log "<SYMBOL> <NODE> applied"`.
     Continue the loop.
   - If it prints `{"ok": false, "error": "..."}` (exit 1), follow "Retry rule" below.

### Retry rule

1. On `ok: false`, spawn one more sub-agent for the same node with the same `prompt_path` and
   `answer_path` and the same model. Use the same wrapper, and append the error text to it:
   `Your previous answer was rejected with this error: <error>. Answer again.`
   Run `.venv/bin/ma log "<SYMBOL> <NODE> retry after error"` first.
2. Run `.venv/bin/ma apply <SYMBOL> <NODE>` again.
3. If it still fails, run `.venv/bin/ma apply <SYMBOL> <NODE> --freetext`.
4. If that also fails, run `.venv/bin/ma log "<SYMBOL> <NODE> failed: <error>"`, stop processing
   this symbol, and continue with the other symbols. Finalize marks this symbol for review.

### Which wrapper line

- For the analysts `market`, `news` and `fundamentals`, keep the line "only for analysts with
  tools" and drop the "all other nodes" line.
- For `social`, `bull`, `bear`, `research_manager`, `trader`, `aggressive`, `conservative`,
  `neutral` and `portfolio_manager`, keep the "all other nodes" line and drop the other.
- Reflections always use the "all other nodes" line.

Model choice: `model` comes from the `ma prompt` output. It is `opus` for `research_manager` and
`portfolio_manager`, and `sonnet` for every other node.

## 5. Finish

1. Run `.venv/bin/ma finalize`. It prints one line per symbol.
   - If it errors, report the error exactly as printed. Do not retry by deleting files, and do
     not edit anything under `desk_runner/var/`.
2. Run `.venv/bin/ma log "end"`.
3. Reply with the finalize summary: one line per symbol, copied from the `ma finalize` output.

## Sub-agent wrapper

Use this text for every sub-agent. Replace each `{placeholder}` with its value. Keep only one of the
two conditional lines, as described in "Which wrapper line". Send it as the sub-agent's prompt.

```
You are one agent in a trading research pipeline. Read the prompt in {prompt_path} and answer it exactly as that prompt asks.
{only for analysts with tools:} Get data only by running `.venv/bin/ma tool {symbol} {node} <tool_name> '<json args>'` with the tools listed in the prompt. Do not use web search or any other data source. Stop calling tools when a call returns the wrap-up message.
{for all other nodes:} Use only the information in the prompt. Do not call tools or search the web.
Write only your final answer to {answer_path} and nothing else to any other file. Reply "done" when written.
```
