"""Stand-in chat model: records the upstream LLM calls and replays scripted answers.

The desk runner drives TradingAgents' graph without calling an LLM. Upstream nodes
call the model through ``bind_tools``, ``invoke`` or ``with_structured_output``;
this class answers each call through callbacks and keeps the exact prompts.
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    convert_to_messages,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, ConfigDict, Field

_ROLE_HEADERS: dict[type[BaseMessage], str] = {
    SystemMessage: "SYSTEM",
    HumanMessage: "HUMAN",
    AIMessage: "AI",
}
_FENCE_RE = re.compile(r"```json[ \t]*\n?(.*?)```", re.DOTALL)


class StandInChatModel(BaseChatModel):
    """A chat model whose answers come from callbacks instead of an LLM."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    answer: Callable[[list[BaseMessage]], str]
    structured: Callable[[list[BaseMessage], type[BaseModel]], BaseModel] | None = None
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    schemas: list[type[BaseModel]] = Field(default_factory=list)
    bound_tools: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "standin"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        message = AIMessage(content=self.answer(list(messages)))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "StandInChatModel":
        self.bound_tools = list(tools)
        return self

    def with_structured_output(
        self, schema: type[BaseModel], **kwargs: Any
    ) -> Runnable[Any, BaseModel]:
        def _invoke(input_: Any) -> BaseModel:
            messages = list(convert_to_messages(_as_message_input(input_)))
            self.calls.append(messages)
            self.schemas.append(schema)
            if self.structured is None:
                raise NotImplementedError("StandInChatModel has no structured answer")
            return self.structured(messages, schema)

        return RunnableLambda(_invoke)


def _as_message_input(input_: Any) -> Any:
    """Accept a plain string or a list of messages / role dicts / tuples."""
    if isinstance(input_, str):
        return [HumanMessage(content=input_)]
    return input_


def _content_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") if isinstance(block, dict) else str(block) for block in content
    )


def render_messages(messages: Sequence[AnyMessage]) -> str:
    """Render messages as ``### ROLE`` sections, the form used for prompt capture."""
    sections = []
    for message in messages:
        role = _ROLE_HEADERS.get(type(message), message.type.upper())
        sections.append(f"### {role}\n{_content_text(message)}")
    return "\n\n".join(sections)


def prompt_hash(text: str) -> str:
    """SHA-256 hex digest of a prompt text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_json_object(text: str) -> str:
    """Return the single JSON object in ``text``.

    A ```json fenced block is used when present; otherwise the outermost ``{...}``
    span is taken. Raises ``ValueError`` when no object can be found.
    """
    fenced = _FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object found in text")
    return candidate[start : end + 1]
