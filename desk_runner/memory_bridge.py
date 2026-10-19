"""Memory bridge: settles past decisions with reflections written by a sub-agent.

Upstream settlement asks the quick model for each reflection. Here that model is a
stand-in, used in two passes around the sub-agents: ``pending_reflections`` records
every reflection prompt to a file (the entries stay pending), and
``settle_reflections`` replays settlement with the answer files the sub-agents wrote.
"""

import json
from pathlib import Path

from desk_runner.config import DeskConfig, Paths, upstream_config, write_json_atomic
from desk_runner.standin import StandInChatModel, prompt_hash, render_messages
from tradingagents.dataflows.date_window import is_historical
from tradingagents.memory import settlement
from tradingagents.memory.log import TradingMemoryLog
from tradingagents.memory.reflection import Reflector

DEFERRED_MESSAGE = "reflection deferred to a sub-agent"


class MemoryBridgeError(Exception):
    """A reflection prompt, answer or pending decision cannot be matched safely."""


class _CaptureReflector(Reflector):
    """Records each reflection prompt under the entry it settles, then defers the call."""

    def __init__(self, memory_dir: Path, entries: dict[str, tuple[str, str]]) -> None:
        self._memory_dir = memory_dir
        self._entries = entries
        self._entry: tuple[str, str] | None = None
        self.prompts: list[Path] = []
        super().__init__(StandInChatModel(answer=self._capture))

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        benchmark_name: str = "SPY",
        holding_days: int = 5,
    ) -> str:
        # Upstream passes only the decision text: that text names the entry.
        self._entry = self._entries[final_decision]
        return super().reflect_on_final_decision(
            final_decision=final_decision,
            raw_return=raw_return,
            alpha_return=alpha_return,
            benchmark_name=benchmark_name,
            holding_days=holding_days,
        )

    def _capture(self, messages: list) -> str:
        if self._entry is None:
            raise MemoryBridgeError("reflection called without a pending entry")
        ticker, entry_date = self._entry
        stem = f"{ticker}_{entry_date}"
        text = render_messages(messages)
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = self._memory_dir / f"{stem}.prompt.md"
        prompt_path.write_text(text, encoding="utf-8")
        write_json_atomic(
            self._memory_dir / f"{stem}.meta.json",
            {"prompt_sha256": prompt_hash(text), "answer_file": f"{stem}.answer.md"},
        )
        self.prompts.append(prompt_path)
        raise RuntimeError(DEFERRED_MESSAGE)


def _memory_dir(paths: Paths, date: str) -> Path:
    return paths.run_dir(date) / "memory"


def _log(cfg: DeskConfig, paths: Paths) -> TradingMemoryLog:
    return TradingMemoryLog(upstream_config(cfg, paths))


def _pending_by_decision(log: TradingMemoryLog) -> dict[str, tuple[str, str]]:
    """(ticker, entry date) of each pending decision, keyed by its decision text.

    Two pending decisions with the same text cannot be told apart, so that is refused.
    """
    entries: dict[str, tuple[str, str]] = {}
    for entry in log.get_pending_entries():
        key = entry["decision"]
        if key in entries:
            raise MemoryBridgeError(
                f"pending decisions {entries[key]} and {(entry['ticker'], entry['date'])} "
                "have the same text"
            )
        entries[key] = (entry["ticker"], entry["date"])
    return entries


def _answers_by_prompt_hash(memory_dir: Path) -> dict[str, Path]:
    """Answer file of each captured prompt, keyed by the prompt's SHA-256."""
    answers: dict[str, Path] = {}
    for meta_file in sorted(memory_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            answers[meta["prompt_sha256"]] = memory_dir / meta["answer_file"]
        except (OSError, ValueError, KeyError) as exc:
            raise MemoryBridgeError(f"unreadable memory metadata {meta_file.name}: {exc}") from exc
    return answers


def pending_reflections(paths: Paths, cfg: DeskConfig, date: str) -> list[Path]:
    """Write the reflection prompts for decisions whose holding window has traded.

    Returns the prompt files written. Every entry stays pending.
    """
    memory_dir = _memory_dir(paths, date)
    reflector = _CaptureReflector(memory_dir, _pending_by_decision(_log(cfg, paths)))
    settlement.settle_all_pending(_log(cfg, paths), reflector, upstream_config(cfg, paths), wait=True)
    return reflector.prompts


def settle_reflections(paths: Paths, cfg: DeskConfig, date: str) -> list[tuple[str, str]]:
    """Settle the pending decisions that have an answer for their captured prompt.

    Returns the (ticker, entry date) pairs settled. A decision without a non-empty
    answer stays pending.
    """
    answers = _answers_by_prompt_hash(_memory_dir(paths, date))

    def answer(messages: list) -> str:
        digest = prompt_hash(render_messages(messages))
        answer_file = answers.get(digest)
        if answer_file is None or not answer_file.exists():
            raise MemoryBridgeError(f"no answer file for reflection prompt {digest[:12]}")
        text = answer_file.read_text(encoding="utf-8").strip()
        if not text:
            raise MemoryBridgeError(f"empty answer file {answer_file.name}")
        return text

    reflector = Reflector(StandInChatModel(answer=answer))
    result = settlement.settle_all_pending(
        _log(cfg, paths), reflector, upstream_config(cfg, paths), wait=True
    )
    return result.settled


def past_context(paths: Paths, cfg: DeskConfig, symbol: str, date: str) -> str:
    """Past decisions and lessons for ``symbol`` as of ``date``, for prompt injection."""
    as_of = date if is_historical(date) else None
    return _log(cfg, paths).get_past_context(symbol, as_of=as_of)
