"""The research agent (cd-platform#164): a LangGraph graph over Bedrock.

The model decides which tools to call and how many times; this module
owns the loop around it. The graph has four nodes:

    agent ──(tool calls)──▶ tools ──▶ agent
      │  └─(step budget spent)──▶ step_limit ──▶ END
      └─(final answer)──▶ check_citations ──▶ END
                                │
                                └─(unknown citation, first time)──▶ agent

- `agent` calls the chat model with the tools bound.
- `tools` runs every tool call from that model turn concurrently.
- `check_citations` verifies that every id the answer cites ([S000033],
  [119-hr-2616]) came back from a tool. The first time one didn't, the
  model gets one chance to fix it. After that the answer is kept but
  marked 'citations_flagged'.
- `step_limit` ends the turn when the model is still calling tools after
  `max_steps` model calls.

Every model call and tool call is appended to the state's `trace`, with
its latency and token usage, and stored alongside the answer.

See docs/agent-design.md for why this is a hand-built StateGraph rather
than LangGraph's prebuilt agent, and why conversations are stored in
cd-server's own tables rather than a LangGraph checkpointer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import operator
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, TypedDict

from botocore.config import Config
from cd.lib import bedrock
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from cd.server import settings
from cd.server.services.agent_store import (
    AgentConversationRecord,
    AgentStoreClient,
    AgentTurnRecord,
    AssistantAnswer,
)
from cd.server.services.agent_tools import TOOL_NAMES, AgentTools
from cd.server.services.bill_search_service import BillSearchService
from cd.server.services.cd_api_service import CdApiService

logger = logging.getLogger(__name__)

# Model calls per turn. Comparing two members on one topic takes about
# five (find both, search, get each one's votes, answer), so this leaves
# room for a retry or two without letting a confused model loop.
DEFAULT_MAX_STEPS = 8
# A question longer than this is almost certainly not a question.
MAX_QUESTION_LEN = 1000
# Earlier turns replayed to the model, newest kept. Bounds input tokens
# on a long conversation.
_HISTORY_TURNS = 20
# A tool result is stored in the trace in full up to this size, so a
# History view can show it; beyond that it's cut.
_TRACE_OUTPUT_MAX_CHARS = 4000

STOP_ANSWERED = "answered"
STOP_STEP_LIMIT = "step_limit"
STOP_CITATIONS_FLAGGED = "citations_flagged"

STEP_LIMIT_MESSAGE = (
    "I couldn't finish researching that within my step limit. Try asking "
    "about one member or one topic at a time."
)

SYSTEM_PROMPT = """\
You are a neutral, nonpartisan research assistant for questions about the
current U.S. Congress: its members, the bills it has voted on, and how
members voted. Today is {today}.

Use the tools to look things up; never answer from memory. Work out which
members and bills the question is about, then fetch what you need. If a
name could match more than one member, say so and ask which one.

Cite every fact you state with the id of the member or bill it came from,
in square brackets: [S000033] for a member, [119-hr-2616] for a bill.
Only cite ids that a tool returned. If the tools don't have what's
needed, say so plainly instead of guessing.

Things the data can't tell you, which you should say when they matter:
- Only House roll-call votes are recorded so far, not Senate votes.
- Only bills that have had a House vote are searchable.

Describe what members did, not why: don't speculate about motives, and
don't label anyone's overall stance (e.g. "anti-X"). A procedural vote,
such as on a motion to recommit, is not a vote on the bill itself.

If the question isn't about Congress, its members, bills or votes, say
briefly that you can only help with those.\
"""

# [S000033], [119-hr-2616], or several in one bracket: [119-hr-1, 119-s-5]
_BRACKET = re.compile(r"\[([^\[\]]+)\]")
_CITATION_ID = re.compile(r"^(?:[A-Z]\d{6}|\d{1,3}-[a-z]+-\d+)$")


def cited_ids(text: str) -> set[str]:
    """Every member or bill id the text cites in square brackets."""
    ids: set[str] = set()
    for bracket in _BRACKET.findall(text):
        for part in bracket.split(","):
            part = part.strip()
            if _CITATION_ID.match(part):
                ids.add(part)
    return ids


def result_ids(value: Any) -> set[str]:
    """Every `id` in a tool result, at any depth."""
    ids: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("id", "member_id") and isinstance(item, str):
                ids.add(item)
            else:
                ids |= result_ids(item)
    elif isinstance(value, list):
        for item in value:
            ids |= result_ids(item)
    return ids


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    trace: Annotated[list[dict[str, Any]], operator.add]
    # ids citable this turn: from earlier turns' tool results, plus every
    # tool result so far in this one.
    known_ids: Annotated[set[str], operator.or_]
    steps: int
    citation_retries: int
    stop_reason: str | None


def _text(message: AIMessage) -> str:
    return message.text if isinstance(message.text, str) else message.text()


def _usage(message: AIMessage) -> tuple[int, int]:
    usage = message.usage_metadata or {}
    return usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def _trace_output(content: str) -> str:
    if len(content) <= _TRACE_OUTPUT_MAX_CHARS:
        return content
    return content[:_TRACE_OUTPUT_MAX_CHARS] + "…"


def build_tools(tools: AgentTools) -> list[BaseTool]:
    """LangChain tools from AgentTools' methods. The schema comes from
    each method's signature and the description from its docstring."""
    return [
        StructuredTool.from_function(
            coroutine=getattr(tools, name),
            name=name,
            description=" ".join(getattr(tools, name).__doc__.split()),
        )
        for name in TOOL_NAMES
    ]


def build_graph(model: BaseChatModel, tools: list[BaseTool], max_steps: int = DEFAULT_MAX_STEPS):
    """The compiled agent graph. `model` is any LangChain chat model that
    supports tool calling -- ChatBedrockConverse in production, a scripted
    fake in tests."""
    bound = model.bind_tools(tools)
    tools_by_name = {t.name: t for t in tools}

    async def agent(state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        message = await bound.ainvoke(state["messages"])
        input_tokens, output_tokens = _usage(message)
        return {
            "messages": [message],
            "steps": state["steps"] + 1,
            "trace": [
                {
                    "type": "model",
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "tool_calls": [c["name"] for c in message.tool_calls],
                }
            ],
        }

    async def run_tool(call: dict[str, Any]) -> tuple[ToolMessage, dict[str, Any], set[str]]:
        started = time.monotonic()
        tool = tools_by_name.get(call["name"])
        if tool is None:
            result: Any = {"error": f"Unknown tool {call['name']!r}."}
        else:
            try:
                result = await tool.ainvoke(call["args"])
            except Exception as exc:  # bad arguments, or a bug in a tool
                logger.warning("agent tool %s failed", call["name"], exc_info=True)
                result = {"error": f"{type(exc).__name__}: {exc}"}
        content = json.dumps(result, default=str)
        step = {
            "type": "tool",
            "name": call["name"],
            "input": call["args"],
            "output": _trace_output(content),
            "error": isinstance(result, dict) and "error" in result,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
        message = ToolMessage(content=content, tool_call_id=call["id"], name=call["name"])
        return message, step, result_ids(result)

    async def run_tools(state: AgentState) -> dict[str, Any]:
        calls = state["messages"][-1].tool_calls
        results = await asyncio.gather(*(run_tool(call) for call in calls))
        known: set[str] = set()
        for _, _, ids in results:
            known |= ids
        return {
            "messages": [message for message, _, _ in results],
            "trace": [step for _, step, _ in results],
            "known_ids": known,
        }

    def step_limit(state: AgentState) -> dict[str, Any]:
        return {"stop_reason": STOP_STEP_LIMIT}

    def check_citations(state: AgentState) -> dict[str, Any]:
        answer = _text(state["messages"][-1])
        unknown = sorted(cited_ids(answer) - state["known_ids"])
        step = {"type": "citation_check", "unknown_ids": unknown}
        if not unknown:
            return {"stop_reason": STOP_ANSWERED, "trace": [step]}
        if state["citation_retries"] == 0 and state["steps"] < max_steps:
            correction = HumanMessage(
                content=(
                    f"Your answer cites {', '.join(unknown)}, which no tool "
                    "returned. Look them up with a tool, or remove those "
                    "claims, then give your full answer again."
                )
            )
            return {"messages": [correction], "citation_retries": 1, "trace": [step]}
        return {"stop_reason": STOP_CITATIONS_FLAGGED, "trace": [step]}

    def after_agent(state: AgentState) -> str:
        if state["messages"][-1].tool_calls:
            return "step_limit" if state["steps"] >= max_steps else "tools"
        return "check_citations"

    def after_check(state: AgentState) -> str:
        return END if state["stop_reason"] else "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_node("step_limit", step_limit)
    graph.add_node("check_citations", check_citations)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent, ["tools", "step_limit", "check_citations"])
    graph.add_edge("tools", "agent")
    graph.add_edge("step_limit", END)
    graph.add_conditional_edges("check_citations", after_check, ["agent", END])
    return graph.compile()


@dataclass(frozen=True)
class TurnResult:
    answer: AssistantAnswer
    messages: list[AnyMessage]


def _history(turns: list[AgentTurnRecord]) -> tuple[list[AnyMessage], set[str]]:
    """Earlier turns as plain question/answer messages, plus every id
    their tool calls returned (still citable in a follow-up). Tool calls
    themselves aren't replayed: the answers already carry what they
    found, and replaying them would multiply input tokens."""
    messages: list[AnyMessage] = []
    known: set[str] = set()
    for turn in turns[-_HISTORY_TURNS:]:
        if turn.role == "user":
            messages.append(HumanMessage(content=turn.content))
        else:
            messages.append(AIMessage(content=turn.content))
    for turn in turns:
        for step in turn.trace:
            if step.get("type") == "tool" and not step.get("error"):
                try:
                    known |= result_ids(json.loads(step["output"]))
                except (KeyError, ValueError):
                    pass  # an output cut short in the trace
    return messages, known


class ConversationNotFoundError(Exception):
    """The conversation doesn't exist, or isn't the caller's."""


class AgentService:
    """What schema.py depends on: runs a turn and stores it."""

    def __init__(
        self,
        store: AgentStoreClient,
        model: BaseChatModel,
        model_id: str,
        tools: list[BaseTool],
        max_steps: int = DEFAULT_MAX_STEPS,
    ):
        self._store = store
        self._model_id = model_id
        self._max_steps = max_steps
        self._graph = build_graph(model, tools, max_steps)

    async def connect(self) -> None:
        await self._store.connect()

    async def aclose(self) -> None:
        await self._store.close()

    async def run_turn(
        self,
        question: str,
        history: list[AnyMessage] | None = None,
        known_ids: set[str] | None = None,
    ) -> TurnResult:
        """Runs the graph for one question, without storing anything."""
        system = SystemMessage(content=SYSTEM_PROMPT.format(today=date.today().isoformat()))
        initial: AgentState = {
            "messages": [system, *(history or []), HumanMessage(content=question)],
            "trace": [],
            "known_ids": set(known_ids or ()),
            "steps": 0,
            "citation_retries": 0,
            "stop_reason": None,
        }
        # A backstop only: the step budget ends the turn well before this.
        config = {"recursion_limit": self._max_steps * 3 + 5}
        try:
            state = await self._graph.ainvoke(initial, config)
        except GraphRecursionError:
            logger.warning("agent hit the recursion limit despite its step budget")
            state = {**initial, "stop_reason": STOP_STEP_LIMIT}

        stop_reason = state["stop_reason"]
        if stop_reason == STOP_STEP_LIMIT:
            content = STEP_LIMIT_MESSAGE
        else:
            content = _text(state["messages"][-1])
        model_steps = [s for s in state["trace"] if s["type"] == "model"]
        answer = AssistantAnswer(
            content=content,
            trace=state["trace"],
            model_id=self._model_id,
            stop_reason=stop_reason,
            input_tokens=sum(s["input_tokens"] for s in model_steps),
            output_tokens=sum(s["output_tokens"] for s in model_steps),
        )
        return TurnResult(answer=answer, messages=state["messages"])

    async def ask(
        self, user_id: str, question: str, conversation_id: int | None = None
    ) -> AgentConversationRecord:
        """Answers `question` -- in a new conversation, or as a follow-up
        in `conversation_id` -- stores the exchange, and returns the whole
        conversation. Raises ValueError for an empty or over-long question
        and ConversationNotFoundError for a conversation that isn't the
        caller's; both before any model call."""
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")
        if len(question) > MAX_QUESTION_LEN:
            raise ValueError(f"question must be at most {MAX_QUESTION_LEN} characters")

        history: list[AnyMessage] = []
        known: set[str] = set()
        if conversation_id is not None:
            conversation = await self._store.get_conversation(user_id, conversation_id)
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            history, known = _history(conversation.turns)

        result = await self.run_turn(question, history, known)

        if conversation_id is None:
            conversation_id = await self._store.start_conversation(user_id, question, result.answer)
        elif not await self._store.add_exchange(user_id, conversation_id, question, result.answer):
            raise ConversationNotFoundError(conversation_id)
        return await self._store.get_conversation(user_id, conversation_id)

    async def conversation(self, user_id: str, conversation_id: int) -> AgentConversationRecord | None:
        return await self._store.get_conversation(user_id, conversation_id)

    async def conversations(self, user_id: str, limit: int = 20) -> list[AgentConversationRecord]:
        return await self._store.list_conversations(user_id, limit)

    async def count_by_user_since(self, user_id: str, since: datetime) -> int:
        """This caller's answers since `since` -- the per-user daily cap."""
        return await self._store.count_answers_since(since, user_id)

    async def count_global_since(self, since: datetime) -> int:
        """Every caller's answers since `since` -- the global daily cap."""
        return await self._store.count_answers_since(since)


# Longer than bedrock_chat_service's 30s: a tool-calling turn's final
# answer can be long, and it's generated after several tool results have
# grown the input.
_BEDROCK_CLIENT = bedrock.build_bedrock_client(
    Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 2, "mode": "standard"})
)


def get_agent_service(cd_api: CdApiService, bill_search: BillSearchService) -> AgentService:
    # Same model setting as summarizeVotingRecord. get_bedrock_chat_client()
    # already fails startup outside local dev when it's unset, so this
    # only needs to construct the model.
    model = ChatBedrockConverse(
        client=_BEDROCK_CLIENT,
        model_id=settings.BEDROCK_CHAT_MODEL_ID,
        temperature=0.2,
        max_tokens=2048,
        disable_streaming=True,
    )
    return AgentService(
        AgentStoreClient(settings.PG_DSN),
        model,
        settings.BEDROCK_CHAT_MODEL_ID,
        build_tools(AgentTools(cd_api, bill_search)),
    )
