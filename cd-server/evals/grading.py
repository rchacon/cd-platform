"""Grading for the agent evals: deterministic checks from the trace, plus
an LLM judge for what only reading the answer can tell.

Everything here except `judge()` is pure, so it's unit-tested
(tests/test_eval_grading.py) without a model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from cd.server.services.agent_service import STOP_ANSWERED, cited_ids
from cd.server.services.agent_store import AssistantAnswer
from evals.cases import Case

# Tool output shown to the judge per call; enough to check claims against.
_JUDGE_TOOL_OUTPUT_CHARS = 3000


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case: Case
    answer: AssistantAnswer | None
    latency_ms: int
    checks: list[Check] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)

    @property
    def tools_called(self) -> list[str]:
        if self.answer is None:
            return []
        return [s["name"] for s in self.answer.trace if s["type"] == "tool"]

    @property
    def model_calls(self) -> int:
        if self.answer is None:
            return 0
        return sum(1 for s in self.answer.trace if s["type"] == "model")


def deterministic_checks(case: Case, answer: AssistantAnswer) -> list[Check]:
    called = {s["name"] for s in answer.trace if s["type"] == "tool"}
    cited = cited_ids(answer.content)
    checks = [
        Check(
            "finished",
            answer.stop_reason == STOP_ANSWERED,
            "" if answer.stop_reason == STOP_ANSWERED else answer.stop_reason,
        )
    ]
    if case.tools_required:
        missing = sorted(case.tools_required - called)
        checks.append(Check("tools_required", not missing, f"never called {missing}" if missing else ""))
    if case.tools_forbidden:
        used = sorted(case.tools_forbidden & called)
        checks.append(Check("tools_forbidden", not used, f"called {used}" if used else ""))
    if case.must_cite:
        uncited = sorted(case.must_cite - cited)
        checks.append(Check("must_cite", not uncited, f"didn't cite {uncited}" if uncited else ""))
    return checks


class _Criterion(BaseModel):
    criterion: str
    passed: bool
    reason: str = Field(description="One sentence.")


class Verdict(BaseModel):
    """The judge's grading of one answer."""

    grounded: bool = Field(
        description=(
            "True if every factual claim in the answer about members, bills or "
            "votes is supported by the tool results. Claims the answer makes "
            "about its own limits (e.g. that Senate votes aren't available) "
            "don't need support."
        )
    )
    unsupported_claims: list[str] = Field(
        default_factory=list, description="Claims not supported by the tool results."
    )
    criteria: list[_Criterion] = Field(
        default_factory=list, description="One entry per criterion, in the order given."
    )


JUDGE_SYSTEM_PROMPT = """\
You grade answers from a research assistant about the U.S. Congress. The
assistant looked things up with tools; you see its tool calls and their
results, and its final answer. Judge only against those tool results, not
your own knowledge: the data may be newer or older than what you know.

Be strict about grounding: a vote, a bill title, a party or a district
that doesn't appear in the tool results is unsupported. Be fair about
criteria: grade what the answer does, not how it's phrased.\
"""


def tool_evidence(answer: AssistantAnswer) -> list[dict[str, Any]]:
    """The turn's tool calls as the judge sees them."""
    evidence = []
    for step in answer.trace:
        if step["type"] != "tool":
            continue
        output = step.get("output", "")
        if len(output) > _JUDGE_TOOL_OUTPUT_CHARS:
            output = output[:_JUDGE_TOOL_OUTPUT_CHARS] + "…"
        evidence.append({"tool": step["name"], "input": step.get("input"), "output": output})
    return evidence


def judge_prompt(case: Case, answer: AssistantAnswer, earlier: list[tuple[str, str]]) -> str:
    parts = []
    if earlier:
        history = "\n".join(f"User: {q}\nAssistant: {a}" for q, a in earlier)
        parts.append(f"Earlier in the conversation:\n{history}")
    parts.append(f"Question:\n{case.question}")
    parts.append(f"Tool calls and results:\n{json.dumps(tool_evidence(answer), indent=1)}")
    parts.append(f"Answer:\n{answer.content}")
    if case.expect:
        criteria = "\n".join(f"{i}. {c}" for i, c in enumerate(case.expect, 1))
        parts.append(f"Criteria:\n{criteria}")
    else:
        parts.append("Criteria: none beyond grounding.")
    return "\n\n".join(parts)


def judge_checks(case: Case, verdict: Verdict) -> list[Check]:
    checks = [
        Check(
            "grounded",
            verdict.grounded,
            "; ".join(verdict.unsupported_claims)[:300],
        )
    ]
    by_text = {c.criterion: c for c in verdict.criteria}
    for i, criterion in enumerate(case.expect):
        # Match by text, falling back to position: the judge sometimes
        # rephrases the criterion it's answering.
        graded = by_text.get(criterion) or (
            verdict.criteria[i] if i < len(verdict.criteria) else None
        )
        if graded is None:
            checks.append(Check(f"expect: {criterion}", False, "judge didn't grade it"))
        else:
            checks.append(Check(f"expect: {criterion}", graded.passed, graded.reason))
    return checks


async def judge(
    model: BaseChatModel, case: Case, answer: AssistantAnswer, earlier: list[tuple[str, str]]
) -> Verdict:
    structured = model.with_structured_output(Verdict)
    return await structured.ainvoke(
        [
            SystemMessage(content=JUDGE_SYSTEM_PROMPT),
            HumanMessage(content=judge_prompt(case, answer, earlier)),
        ]
    )
