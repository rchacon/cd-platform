import asyncio
import json
from datetime import datetime, timezone

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import Field

from cd.server.services.agent_service import (
    STEP_LIMIT_MESSAGE,
    AgentService,
    ConversationNotFoundError,
    build_graph,
    cited_ids,
    result_ids,
)
from cd.server.services.agent_store import AgentConversationRecord, AgentTurnRecord

pytestmark = pytest.mark.anyio

_NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


class ScriptedChatModel(BaseChatModel):
    """Returns its scripted messages in order and records what it was sent."""

    responses: list[AIMessage]
    calls: list[list] = Field(default_factory=list)
    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = [t.name for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai(text: str = "", *calls: dict, tokens: tuple[int, int] = (100, 10)) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=list(calls),
        usage_metadata={
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
            "total_tokens": sum(tokens),
        },
    )


async def _find_members(name: str) -> dict:
    """Find members by name."""
    return {"members": [{"id": "S000033", "name": "Bernie Sanders"}]}


async def _boom(name: str) -> dict:
    """Always fails."""
    raise RuntimeError("tool broke")


def _tool(fn, name: str):
    return StructuredTool.from_function(coroutine=fn, name=name, description=fn.__doc__)


_FIND = _tool(_find_members, "find_members")


async def _run(model, tools=(_FIND,), max_steps=8, history=None, known_ids=None):
    graph = build_graph(model, list(tools), max_steps)
    state = {
        "messages": [*(history or []), HumanMessage(content="question")],
        "trace": [],
        "known_ids": set(known_ids or ()),
        "steps": 0,
        "citation_retries": 0,
        "stop_reason": None,
    }
    return await graph.ainvoke(state, {"recursion_limit": 50})


def test_cited_ids_finds_member_and_bill_ids_including_several_per_bracket():
    text = (
        "Sanders [S000033] voted Yea [119-hr-2616, 119-s-5]. "
        "See [the site](https://x.gov) and [note]."
    )
    assert cited_ids(text) == {"S000033", "119-hr-2616", "119-s-5"}


def test_result_ids_collects_ids_at_any_depth():
    result = {"member_id": "P000197", "bills": [{"id": "119-hr-1", "votes": [{"id": 7}]}]}
    assert result_ids(result) == {"P000197", "119-hr-1"}


async def test_an_answer_without_tool_calls_ends_the_turn():
    model = ScriptedChatModel(responses=[_ai("I can only help with Congress.")])

    state = await _run(model)

    assert state["stop_reason"] == "answered"
    assert state["steps"] == 1
    assert [s["type"] for s in state["trace"]] == ["model", "citation_check"]
    assert model.bound_tools == ["find_members"]


async def test_tool_results_go_back_to_the_model_and_every_step_is_traced():
    model = ScriptedChatModel(
        responses=[
            _ai("", _call("find_members", {"name": "sanders"}, "c1"), tokens=(100, 20)),
            _ai("Bernie Sanders [S000033] is a senator.", tokens=(300, 15)),
        ]
    )

    state = await _run(model)

    assert state["stop_reason"] == "answered"
    tool_message = model.calls[1][-1]
    assert isinstance(tool_message, ToolMessage) and tool_message.tool_call_id == "c1"
    assert json.loads(tool_message.content)["members"][0]["id"] == "S000033"
    model_1, tool_step, model_2, check = state["trace"]
    assert model_1["tool_calls"] == ["find_members"]
    assert (model_1["input_tokens"], model_2["input_tokens"]) == (100, 300)
    assert tool_step["name"] == "find_members" and tool_step["input"] == {"name": "sanders"}
    assert tool_step["error"] is False
    assert check == {"type": "citation_check", "unknown_ids": []}


async def test_tool_calls_from_one_model_turn_run_concurrently():
    barrier = asyncio.Barrier(2)

    async def wait_for_the_other(name: str) -> dict:
        """Waits until both calls are running."""
        await asyncio.wait_for(barrier.wait(), timeout=2)  # sequential calls would time out
        return {"id": name}

    model = ScriptedChatModel(
        responses=[
            _ai("", _call("wait", {"name": "S000001"}, "a"), _call("wait", {"name": "S000002"}, "b")),
            _ai("Both [S000001] [S000002]."),
        ]
    )

    state = await _run(model, tools=[_tool(wait_for_the_other, "wait")])

    assert state["stop_reason"] == "answered"
    assert [s["error"] for s in state["trace"] if s["type"] == "tool"] == [False, False]


async def test_a_failing_or_unknown_tool_becomes_an_error_result_for_the_model():
    model = ScriptedChatModel(
        responses=[
            _ai("", _call("boom", {"name": "x"}, "a"), _call("nope", {}, "b")),
            _ai("Sorry, the lookup failed."),
        ]
    )

    state = await _run(model, tools=[_tool(_boom, "boom")])

    boom, nope = (json.loads(m.content) for m in model.calls[1][-2:])
    assert boom == {"error": "RuntimeError: tool broke"}
    assert nope == {"error": "Unknown tool 'nope'."}
    assert [s["error"] for s in state["trace"] if s["type"] == "tool"] == [True, True]
    assert state["stop_reason"] == "answered"


async def test_the_step_budget_ends_a_model_that_keeps_calling_tools():
    model = ScriptedChatModel(
        responses=[_ai("", _call("find_members", {"name": "x"}, f"c{i}")) for i in range(3)]
    )

    state = await _run(model, max_steps=3)

    assert state["stop_reason"] == "step_limit"
    assert state["steps"] == 3
    # The third call's tools never run: the budget is spent first.
    assert len([s for s in state["trace"] if s["type"] == "tool"]) == 2


async def test_an_unknown_citation_gets_one_correction_then_passes():
    model = ScriptedChatModel(
        responses=[
            _ai("Sanders [S000033] voted on [119-hr-9]."),
            _ai("", _call("find_members", {"name": "sanders"}, "c1")),
            _ai("Sanders [S000033] is a senator."),
        ]
    )

    state = await _run(model)

    assert state["stop_reason"] == "answered"
    correction = model.calls[1][-1]
    assert isinstance(correction, HumanMessage)
    assert "119-hr-9, S000033" in correction.content
    checks = [s for s in state["trace"] if s["type"] == "citation_check"]
    assert checks == [
        {"type": "citation_check", "unknown_ids": ["119-hr-9", "S000033"]},
        {"type": "citation_check", "unknown_ids": []},
    ]


async def test_a_second_unknown_citation_flags_the_answer_instead_of_looping():
    model = ScriptedChatModel(responses=[_ai("Made up [S999999]."), _ai("Still made up [S999999].")])

    state = await _run(model)

    assert state["stop_reason"] == "citations_flagged"
    assert state["messages"][-1].content == "Still made up [S999999]."
    assert len(model.calls) == 2


async def test_ids_from_earlier_turns_can_be_cited_without_a_new_lookup():
    model = ScriptedChatModel(responses=[_ai("As before, [S000033].")])

    state = await _run(model, known_ids={"S000033"})

    assert state["stop_reason"] == "answered"


class _FakeStore:
    def __init__(self, conversation=None):
        self.conversation = conversation
        self.started = []
        self.added = []

    async def get_conversation(self, user_id, conversation_id):
        if self.conversation and self.conversation.id == conversation_id:
            return self.conversation
        return None

    async def start_conversation(self, user_id, question, answer):
        self.started.append((user_id, question, answer))
        self.conversation = AgentConversationRecord(id=1, title=question, created_at=_NOW, updated_at=_NOW)
        return 1

    async def add_exchange(self, user_id, conversation_id, question, answer):
        self.added.append((user_id, conversation_id, question, answer))
        return True


def _service(model, store) -> AgentService:
    return AgentService(store, model, "test-model", [_FIND])


def _turn(turn_id, role, content, trace=()):
    return AgentTurnRecord(
        id=turn_id, role=role, content=content, trace=list(trace), model_id=None,
        stop_reason=None, input_tokens=None, output_tokens=None, created_at=_NOW,
    )


async def test_ask_starts_a_conversation_with_the_answer_and_its_usage():
    model = ScriptedChatModel(
        responses=[
            _ai("", _call("find_members", {"name": "sanders"}, "c1"), tokens=(100, 20)),
            _ai("Sanders [S000033].", tokens=(300, 15)),
        ]
    )
    store = _FakeStore()

    await _service(model, store).ask("user-1", "  Who is Sanders?  ")

    ((user_id, question, answer),) = store.started
    assert (user_id, question) == ("user-1", "Who is Sanders?")
    assert answer.content == "Sanders [S000033]."
    assert (answer.stop_reason, answer.model_id) == ("answered", "test-model")
    assert (answer.input_tokens, answer.output_tokens) == (400, 35)
    system = model.calls[0][0]
    assert "nonpartisan" in system.content and "{today}" not in system.content


async def test_ask_replays_earlier_turns_and_their_tool_ids_in_a_follow_up():
    earlier = AgentConversationRecord(
        id=7, title="t", created_at=_NOW, updated_at=_NOW,
        turns=[
            _turn(1, "user", "Who is Sanders?"),
            _turn(2, "assistant", "Sanders [S000033].", trace=[
                {"type": "tool", "name": "find_members", "error": False,
                 "output": json.dumps({"members": [{"id": "S000033"}]})},
            ]),
        ],
    )
    model = ScriptedChatModel(responses=[_ai("He is [S000033].")])
    store = _FakeStore(earlier)

    await _service(model, store).ask("user-1", "Which party?", conversation_id=7)

    sent = model.calls[0]
    assert [type(m).__name__ for m in sent] == [
        "SystemMessage", "HumanMessage", "AIMessage", "HumanMessage"
    ]
    assert sent[-1].content == "Which party?"
    ((_, conversation_id, _, answer),) = store.added
    assert conversation_id == 7
    assert answer.stop_reason == "answered"  # S000033 still citable


async def test_ask_rejects_a_bad_question_or_conversation_before_calling_the_model():
    model = ScriptedChatModel(responses=[])
    service = _service(model, _FakeStore())

    with pytest.raises(ValueError):
        await service.ask("user-1", "   ")
    with pytest.raises(ValueError):
        await service.ask("user-1", "x" * 1001)
    with pytest.raises(ConversationNotFoundError):
        await service.ask("user-1", "hi", conversation_id=99)
    assert model.calls == []


async def test_ask_stores_the_step_limit_message_when_the_budget_runs_out():
    model = ScriptedChatModel(
        responses=[_ai("", _call("find_members", {"name": "x"}, f"c{i}")) for i in range(8)]
    )
    store = _FakeStore()

    await _service(model, store).ask("user-1", "Compare everyone")

    answer = store.started[0][2]
    assert (answer.stop_reason, answer.content) == ("step_limit", STEP_LIMIT_MESSAGE)
