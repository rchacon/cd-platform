"""The eval harness's own logic, without a real model: grading, the judge
prompt, the scorecard, and how the runner plays out a multi-turn case."""
import json

import pytest
from langchain_core.messages import AIMessage

from cd.server.services.agent_service import AgentService
from cd.server.services.agent_store import AssistantAnswer
from evals.cases import CASES, FIND, SEARCH, VOTES, _case
from evals.grading import (
    CaseResult,
    Verdict,
    _Criterion,
    deterministic_checks,
    judge_checks,
    judge_prompt,
    tool_evidence,
)
from evals.run import run_case, scorecard
from services.test_agent_service import _FIND, ScriptedChatModel, _ai, _call


def _answer(content="Sanders [S000033].", tools=(FIND,), stop="answered", output="{}"):
    trace = [{"type": "model", "input_tokens": 10, "output_tokens": 2}]
    trace += [{"type": "tool", "name": t, "input": {}, "output": output, "error": False} for t in tools]
    return AssistantAnswer(content, trace, "m", stop, 10, 2)


def test_case_ids_are_unique_and_every_case_checks_something():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    for case in CASES:
        assert case.tools_required or case.tools_forbidden or case.must_cite or case.expect, case.id


def test_deterministic_checks_pass_a_good_answer():
    case = _case("c", "q", "votes", tools=[FIND], forbid=[VOTES], cite=["S000033"])

    checks = deterministic_checks(case, _answer())

    assert [(c.name, c.passed) for c in checks] == [
        ("finished", True), ("tools_required", True), ("tools_forbidden", True), ("must_cite", True)
    ]


def test_deterministic_checks_report_what_went_wrong():
    case = _case("c", "q", "votes", tools=[FIND, SEARCH], forbid=[FIND], cite=["P000197"])

    checks = {c.name: c for c in deterministic_checks(case, _answer(stop="citations_flagged"))}

    assert checks["finished"].detail == "citations_flagged"
    assert checks["tools_required"].detail == "never called ['search_bills']"
    assert checks["tools_forbidden"].detail == "called ['find_members']"
    assert checks["must_cite"].detail == "didn't cite ['P000197']"
    assert not any(c.passed for c in checks.values())


def test_tool_evidence_shortens_long_outputs():
    (evidence,) = tool_evidence(_answer(output="x" * 5000))
    assert evidence["tool"] == FIND
    assert len(evidence["output"]) == 3001


def test_judge_prompt_has_the_history_evidence_answer_and_numbered_criteria():
    case = _case("c", ("first", "second"), "multi_turn", expect=["Says A.", "Says B."])

    prompt = judge_prompt(case, _answer(output='{"id": "S000033"}'), [("first", "earlier answer")])

    assert "User: first\nAssistant: earlier answer" in prompt
    assert "Question:\nsecond" in prompt
    assert '"tool": "find_members"' in prompt
    assert "Answer:\nSanders [S000033]." in prompt
    assert "1. Says A.\n2. Says B." in prompt


def test_judge_checks_match_criteria_by_text_or_position():
    case = _case("c", "q", "x", expect=["Says A.", "Says B.", "Says C."])
    verdict = Verdict(
        grounded=False,
        unsupported_claims=["a made-up vote"],
        criteria=[
            _Criterion(criterion="Says B.", passed=False, reason="no"),  # matched by text
            _Criterion(criterion="says a, rephrased", passed=True, reason="yes"),  # position 1
        ],
    )

    checks = judge_checks(case, verdict)

    assert [(c.name, c.passed) for c in checks] == [
        ("grounded", False),
        ("expect: Says A.", False),  # position 0 is the "Says B." entry
        ("expect: Says B.", False),
        ("expect: Says C.", False),
    ]
    assert checks[0].detail == "a made-up vote"
    assert checks[3].detail == "judge didn't grade it"


def test_scorecard_summarizes_and_details_failures():
    good = CaseResult(_case("good", "q", "lookup", tools=[FIND]), _answer(), 1200)
    good.checks = deterministic_checks(good.case, good.answer)
    bad = CaseResult(_case("bad", "Why?", "scope"), None, 300, error="RuntimeError: boom")

    report = scorecard([good, bad], "agent-model", None)

    assert "**Passed:** 1 of 2 cases (50%)" in report
    assert "| lookup | 1 / 1 |" in report and "| scope | 0 / 1 |" in report
    assert "| `good` | pass | find_members | 1 | 10 / 2 | 1.2s |" in report
    assert "### `bad`" in report and "RuntimeError: boom" in report


class _NoStore:
    pass


@pytest.mark.anyio
async def test_run_case_carries_history_and_ids_into_the_last_turn():
    model = ScriptedChatModel(
        responses=[
            _ai("", _call("find_members", {"name": "sanders"}, "c1")),
            _ai("Sanders [S000033]."),
            _ai("He's an Independent [S000033]."),  # cites an id from turn one
        ]
    )
    agent = AgentService(_NoStore(), model, "m", [_FIND])
    case = _case("mt", ("Who is Sanders?", "Which party?"), "multi_turn", cite=["S000033"])

    result = await run_case(agent, None, case)

    assert result.passed, result.checks
    last_call = model.calls[-1]
    assert [m.content for m in last_call[1:]] == ["Who is Sanders?", "Sanders [S000033].", "Which party?"]
    assert isinstance(last_call[2], AIMessage)
    assert json.loads(json.dumps(result.answer.trace))  # serializable for the JSON results
