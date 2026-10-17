"""Upstream agent nodes as desk runner turns.

``next_nodes`` says which upstream nodes run next. ``build_prompt`` captures the exact
prompt such a node would send. ``apply_answer`` replays a sub-agent's answer through the
same upstream node, so upstream code produces the state update.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel

from desk_runner.config import DeskConfig, Paths, write_json_atomic
from desk_runner.standin import StandInChatModel, extract_json_object, prompt_hash, render_messages
from desk_runner.state_store import load_state, update_state
from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.agents.analysts.fundamentals_analyst import TOOLS as FUNDAMENTALS_TOOLS
from tradingagents.agents.analysts.market_analyst import TOOLS as MARKET_TOOLS
from tradingagents.agents.analysts.news_analyst import TOOLS as NEWS_TOOLS
from tradingagents.agents.schemas import (
    PortfolioDecision,
    ResearchPlan,
    SentimentReport,
    TraderProposal,
)
from tradingagents.graph.conditional_logic import ConditionalLogic

SENTIMENT_INPUTS = "sentiment_inputs.json"
MODEL_ALIAS = {"deep": "opus", "quick": "sonnet"}
_DEBATE_NODES = {
    "Bull Researcher": "bull",
    "Bear Researcher": "bear",
    "Research Manager": "research_manager",
}
_RISK_NODES = {
    "Aggressive Analyst": "aggressive",
    "Conservative Analyst": "conservative",
    "Neutral Analyst": "neutral",
    "Portfolio Manager": "portfolio_manager",
}


class NodeError(Exception):
    """A node cannot run next, or its answer cannot be applied."""


@dataclass(frozen=True)
class NodeSpec:
    """One upstream node: its factory, model tier, output schema, report key and tools."""

    key: str
    factory: Callable[[Any], Callable[[dict[str, Any]], dict[str, Any]]]
    tier: Literal["deep", "quick"]
    schema: type[BaseModel] | None = None
    report_key: str | None = None
    tools: tuple[Any, ...] = ()


NODES: dict[str, NodeSpec] = {
    spec.key: spec
    for spec in (
        NodeSpec("market", create_market_analyst, "quick", report_key="market_report", tools=MARKET_TOOLS),
        NodeSpec(
            "social",
            create_sentiment_analyst,
            "quick",
            schema=SentimentReport,
            report_key="sentiment_report",
        ),
        NodeSpec("news", create_news_analyst, "quick", report_key="news_report", tools=NEWS_TOOLS),
        NodeSpec(
            "fundamentals",
            create_fundamentals_analyst,
            "quick",
            report_key="fundamentals_report",
            tools=FUNDAMENTALS_TOOLS,
        ),
        NodeSpec("bull", create_bull_researcher, "quick"),
        NodeSpec("bear", create_bear_researcher, "quick"),
        NodeSpec("research_manager", create_research_manager, "deep", schema=ResearchPlan),
        NodeSpec("trader", create_trader, "quick", schema=TraderProposal),
        NodeSpec("aggressive", create_aggressive_debator, "quick"),
        NodeSpec("conservative", create_conservative_debator, "quick"),
        NodeSpec("neutral", create_neutral_debator, "quick"),
        NodeSpec("portfolio_manager", create_portfolio_manager, "deep", schema=PortfolioDecision),
    )
}


class PromptFile(BaseModel):
    prompt_path: Path
    answer_path: Path
    model: str


class _TakeFreeText(Exception):
    """Raised by the stand-in's structured call so upstream takes its free-text path."""


def next_nodes(state: dict[str, Any], cfg: DeskConfig) -> list[str]:
    """The upstream nodes that run next for ``state``; empty once the desk is done."""
    analysts = [key for key, spec in NODES.items() if spec.report_key and not state.get(spec.report_key)]
    if analysts:
        return analysts
    logic = ConditionalLogic(cfg.max_debate_rounds, cfg.max_risk_discuss_rounds)
    if not state.get("investment_plan"):
        return [_DEBATE_NODES[logic.should_continue_debate(state)]]
    if not state.get("trader_investment_plan"):
        return ["trader"]
    if not state.get("final_trade_decision"):
        return [_RISK_NODES[logic.should_continue_risk_analysis(state)]]
    return []


def build_prompt(paths: Paths, date: str, symbol: str, node: str, cfg: DeskConfig) -> PromptFile:
    """Capture the prompt ``node`` would send next and write it as a turn for a sub-agent."""
    state = _require_next(paths, date, symbol, node, cfg)
    turns = _turns_dir(paths, date, symbol)
    if _pending_meta(turns, node) is not None:
        raise NodeError(f"a prompt for {node} is already pending; apply its answer first")

    spec = NODES[node]
    model = StandInChatModel(answer=lambda _messages: "", structured=_take_free_text)
    with _sentiment_sources(paths, date, symbol, record=True) if spec.key == "social" else nullcontext():
        spec.factory(model)(state)
    rendered = _first_call_text(model, node)

    turns.mkdir(parents=True, exist_ok=True)
    stem = f"{_next_turn_number(turns):02d}_{node}"
    prompt_path = turns / f"{stem}.prompt.md"
    answer_path = turns / f"{stem}.answer.md"
    prompt_path.write_text(_prompt_text(spec, rendered))
    write_json_atomic(
        turns / f"{stem}.meta.json",
        {
            "node": node,
            "tier": spec.tier,
            "model": MODEL_ALIAS[spec.tier],
            "prompt_sha256": prompt_hash(rendered),
            "answer_file": answer_path.name,
        },
    )
    return PromptFile(prompt_path=prompt_path, answer_path=answer_path, model=MODEL_ALIAS[spec.tier])


def apply_answer(
    paths: Paths,
    date: str,
    symbol: str,
    node: str,
    cfg: DeskConfig,
    freetext: bool = False,
) -> dict[str, Any]:
    """Replay the pending turn's answer through ``node`` and merge the update into the state."""
    if node not in NODES:
        raise NodeError(f"unknown node {node!r}")
    spec = NODES[node]
    turns = _turns_dir(paths, date, symbol)
    meta_path = _pending_meta(turns, node)
    if meta_path is None:
        raise NodeError(f"no prompt is pending for {node}; run the prompt command first")
    meta = json.loads(meta_path.read_text())
    answer = _read_answer(turns / meta["answer_file"])
    parsed = None if freetext or spec.schema is None else _parse_structured(spec.schema, answer)

    def merge(state: dict[str, Any]) -> dict[str, Any]:
        if node not in next_nodes(state, cfg):
            raise NodeError(f"{node} is not the next node; next: {next_nodes(state, cfg) or 'none'}")
        model = StandInChatModel(answer=lambda _messages: answer, structured=_structured_answer(parsed))
        with _sentiment_sources(paths, date, symbol, record=False) if spec.key == "social" else nullcontext():
            update = spec.factory(model)(state)
        if prompt_hash(_first_call_text(model, node)) != meta["prompt_sha256"]:
            raise NodeError("state changed since prompt")
        return _state_update(spec, update)

    merged = update_state(paths, date, symbol, merge)
    if parsed is not None:
        write_json_atomic(
            paths.symbol_dir(date, symbol) / "structured" / f"{node}.json",
            parsed.model_dump(mode="json"),
        )
    meta_path.rename(meta_path.with_name(meta_path.name.replace(".meta.json", ".applied.json")))
    return merged


def _require_next(paths: Paths, date: str, symbol: str, node: str, cfg: DeskConfig) -> dict[str, Any]:
    if node not in NODES:
        raise NodeError(f"unknown node {node!r}; known: {', '.join(NODES)}")
    state = load_state(paths, date, symbol)
    nodes = next_nodes(state, cfg)
    if node not in nodes:
        raise NodeError(f"{node} is not the next node; next: {', '.join(nodes) or 'none (done)'}")
    return state


def _turns_dir(paths: Paths, date: str, symbol: str) -> Path:
    return paths.symbol_dir(date, symbol) / "turns"


def _pending_meta(turns: Path, node: str) -> Path | None:
    """The meta file of a turn whose answer has not been applied yet, if any."""
    pending = sorted(turns.glob(f"*_{node}.meta.json"))
    return pending[-1] if pending else None


def _next_turn_number(turns: Path) -> int:
    return len(list(turns.glob("*.prompt.md"))) + 1


def _first_call_text(model: StandInChatModel, node: str) -> str:
    """The rendered first model call; upstream's free-text fallback repeats it, so only this one counts."""
    if not model.calls:
        raise NodeError(f"{node} made no model call")
    return render_messages(model.calls[0])


def _prompt_text(spec: NodeSpec, rendered: str) -> str:
    sections = [rendered]
    if spec.tools:
        sections.append(_tools_section(spec.tools))
    if spec.schema is not None:
        sections.append(_output_section(spec.schema))
    return "\n\n".join(sections) + "\n"


def _tools_section(tools: tuple[Any, ...]) -> str:
    lines = ["## Tools", "", "Call these tools only through `ma tool`, never directly.", ""]
    for tool in tools:
        schema = json.dumps(tool.tool_call_schema.model_json_schema(), indent=2)
        lines += [f"### {tool.name}", "", tool.description, "", "```json", schema, "```", ""]
    return "\n".join(lines).rstrip()


def _output_section(schema: type[BaseModel]) -> str:
    body = json.dumps(schema.model_json_schema(), indent=2)
    return (
        "## Output format\n\n"
        "Answer with one JSON object matching this schema.\n\n"
        f"```json\n{body}\n```"
    )


def _take_free_text(_messages: list[Any], _schema: type[BaseModel]) -> BaseModel:
    raise _TakeFreeText("structured answer not requested; upstream takes its free-text path")


def _structured_answer(parsed: BaseModel | None) -> Callable[[list[Any], type[BaseModel]], BaseModel]:
    def structured(_messages: list[Any], _schema: type[BaseModel]) -> BaseModel:
        if parsed is None:
            raise _TakeFreeText("free-text answer; upstream takes its free-text path")
        return parsed

    return structured


def _parse_structured(schema: type[BaseModel], answer: str) -> BaseModel:
    try:
        return schema.model_validate_json(extract_json_object(answer))
    except ValueError as exc:
        raise NodeError(f"answer is not a valid {schema.__name__} JSON object: {exc}") from exc


def _read_answer(path: Path) -> str:
    try:
        answer = path.read_text()
    except FileNotFoundError as exc:
        raise NodeError(f"answer file not found: {path}") from exc
    if not answer.strip():
        raise NodeError(f"answer file is empty: {path}")
    return answer


def _state_update(spec: NodeSpec, update: dict[str, Any]) -> dict[str, Any]:
    """Analysts contribute only their report; every other node contributes its whole update."""
    if spec.report_key is None:
        return update
    report = update.get(spec.report_key, "")
    if not report.strip():
        raise NodeError(f"{spec.key} produced an empty report")
    return {spec.report_key: report}


@contextmanager
def _sentiment_sources(paths: Paths, date: str, symbol: str, *, record: bool) -> Iterator[None]:
    """Record the sentiment sources at prompt time, or replay the recorded ones at apply time."""
    path = paths.symbol_dir(date, symbol) / SENTIMENT_INPUTS
    recorded: dict[str, str] = {}
    if record:
        replacements: dict[str, Any] = {
            "get_news": SimpleNamespace(func=_recording(recorded, "news", sentiment_analyst.get_news.func)),
            "fetch_stocktwits_messages": _recording(
                recorded, "stocktwits", sentiment_analyst.fetch_stocktwits_messages
            ),
            "fetch_reddit_posts": _recording(recorded, "reddit", sentiment_analyst.fetch_reddit_posts),
        }
    else:
        saved = _read_sentiment_inputs(path)
        replacements = {
            "get_news": SimpleNamespace(func=lambda *_args, **_kwargs: saved["news"]),
            "fetch_stocktwits_messages": lambda *_args, **_kwargs: saved["stocktwits"],
            "fetch_reddit_posts": lambda *_args, **_kwargs: saved["reddit"],
            "jev_screen": lambda _ticker: None,
        }
    originals = {name: getattr(sentiment_analyst, name) for name in replacements}
    for name, value in replacements.items():
        setattr(sentiment_analyst, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(sentiment_analyst, name, value)
    if record:
        write_json_atomic(path, recorded)


def _recording(store: dict[str, str], key: str, fetch: Callable[..., str]) -> Callable[..., str]:
    def record(*args: Any, **kwargs: Any) -> str:
        result = fetch(*args, **kwargs)
        store[key] = result
        return result

    return record


def _read_sentiment_inputs(path: Path) -> dict[str, str]:
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise NodeError(f"sentiment inputs were not recorded: {path}") from exc
    missing = {"news", "stocktwits", "reddit"} - saved.keys()
    if missing:
        raise NodeError(f"sentiment inputs incomplete, missing {sorted(missing)}: {path}")
    return saved
