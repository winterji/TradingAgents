"""Stand-in chat model: records upstream LLM calls and replays scripted answers."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool

from desk_runner.standin import (
    StandInChatModel,
    extract_json_object,
    prompt_hash,
    render_messages,
)
from tradingagents.agents.schemas import TraderAction, TraderProposal


@tool
def get_thing(symbol: str) -> str:
    """Look up a thing for a symbol."""
    return symbol


def test_invoke_with_string_records_human_message():
    model = StandInChatModel(answer=lambda m: "hi")

    result = model.invoke("q")

    assert result.content == "hi"
    assert model.calls[0][0].content == "q"


def test_prompt_pipe_bind_tools():
    model = StandInChatModel(answer=lambda m: "analysis")
    prompt = ChatPromptTemplate.from_messages(
        [("system", "S {x}"), MessagesPlaceholder("messages")]
    ).partial(x="1")

    result = (prompt | model.bind_tools([get_thing])).invoke([("human", "AAPL")])

    assert result.content == "analysis"
    assert render_messages(model.calls[0]).startswith("### SYSTEM\nS 1")
    assert model.bound_tools == [get_thing]


def test_structured_returns_model():
    model = StandInChatModel(
        answer=lambda m: "unused",
        structured=lambda m, s: s.model_validate({"action": "Hold", "reasoning": "r"}),
    )

    result = model.with_structured_output(TraderProposal).invoke([{"role": "user", "content": "x"}])

    assert isinstance(result, TraderProposal)
    assert result.action == TraderAction.HOLD
    assert model.schemas == [TraderProposal]


def test_structured_without_callback_raises_not_implemented():
    model = StandInChatModel(answer=lambda m: "unused")

    with pytest.raises(NotImplementedError):
        model.with_structured_output(TraderProposal).invoke("x")


def test_generate_never_returns_tool_calls():
    model = StandInChatModel(answer=lambda m: "plain")

    result = model.bind_tools([get_thing]).invoke("q")

    assert isinstance(result, AIMessage)
    assert result.tool_calls == []


def test_render_messages_uses_role_headers():
    rendered = render_messages(
        [SystemMessage(content="sys"), HumanMessage(content="hum"), AIMessage(content="ai")]
    )

    assert rendered == "### SYSTEM\nsys\n\n### HUMAN\nhum\n\n### AI\nai"


def test_prompt_hash_is_sha256_hex():
    assert prompt_hash("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_extract_json_object_from_fence():
    text = 'Sure.\n```json\n{"a": 1}\n```\nDone.'

    assert extract_json_object(text) == '{"a": 1}'


def test_extract_json_object_bare_with_prose():
    assert extract_json_object('Here: {"a": 1} done') == '{"a": 1}'


def test_extract_json_object_without_object_raises():
    with pytest.raises(ValueError):
        extract_json_object("no json here")
