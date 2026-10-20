"""Runs the research agent's golden questions against real Bedrock and
real data, grades them, and writes a scorecard.

    make eval-agent                         # every case
    make eval-agent ARGS="--case vt-senators --case aoc-guns"
    make eval-agent ARGS="--no-judge"       # deterministic checks only

Needs:
- `BEDROCK_CHAT_MODEL_ID` set, with Bedrock access through `AWS_PROFILE`.
  The same profile covers the Titan embeddings behind `search_bills`.
- A synced `congressional_app` database (`make start-etl`, or
  `local_seed.sql`).

The agent calls cd.api in-process through httpx's ASGI transport, so no
cd-server needs to be running. Nothing is stored: each case runs the
graph directly with `AgentService.run_turn`.

Each run costs real Bedrock calls: about 25 cases × a few model calls,
plus one judge call per case. The token totals are in the scorecard.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from botocore.config import Config
from cd.lib import bedrock
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from cd.api import db as api_db
from cd.api.app import app as api_app
from cd.server import settings
from cd.server.services.agent_service import AgentService, get_agent_service, trace_ids
from cd.server.services.bill_search_service import BillSearchService
from cd.server.services.cd_api_service import ApiClient, ApiClientError, CdApiService
from evals.cases import CASES, Case
from evals.grading import CaseResult, Check, deterministic_checks, judge, judge_checks

RESULTS_DIR = Path(__file__).parent / "results"


class AsgiApiClient(ApiClient):
    """Calls cd.api in this process instead of over the network."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://cd-api"
        )

    async def get(self, path: str, query: dict[str, str]) -> dict:
        response = await self._client.get(path, params=query)
        if response.is_error:
            raise ApiClientError(response.status_code, response.text)
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


async def run_case(
    agent: AgentService, judge_model: ChatBedrockConverse | None, case: Case
) -> CaseResult:
    history: list[AnyMessage] = []
    known: set[str] = set()
    earlier: list[tuple[str, str]] = []
    started = time.monotonic()
    try:
        for i, question in enumerate(case.turns):
            result = await agent.run_turn(question, history, known)
            if i < len(case.turns) - 1:
                history += [HumanMessage(content=question), AIMessage(content=result.answer.content)]
                known |= trace_ids(result.answer.trace)
                earlier.append((question, result.answer.content))
        answer = result.answer
    except Exception as exc:
        return CaseResult(case, None, round((time.monotonic() - started) * 1000),
                          error=f"{type(exc).__name__}: {exc}")
    latency_ms = round((time.monotonic() - started) * 1000)

    checks = deterministic_checks(case, answer)
    if judge_model is not None:
        try:
            verdict = await judge(judge_model, case, answer, earlier)
            checks += judge_checks(case, verdict)
        except Exception as exc:
            checks.append(Check("judge", False, f"judge failed: {type(exc).__name__}: {exc}"))
    return CaseResult(case, answer, latency_ms, checks)


def scorecard(results: list[CaseResult], model_id: str, judge_id: str | None) -> str:
    passed = sum(r.passed for r in results)
    answered = [r for r in results if r.answer is not None]
    lines = [
        "# Research agent eval scorecard",
        "",
        f"- **Run:** {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        f"- **Agent model:** `{model_id}`",
        f"- **Judge model:** `{judge_id}`" if judge_id else "- **Judge:** off (deterministic checks only)",
        f"- **Passed:** {passed} of {len(results)} cases ({passed / len(results):.0%})",
    ]
    if answered:
        tokens_in = sum(r.answer.input_tokens for r in answered)
        tokens_out = sum(r.answer.output_tokens for r in answered)
        latencies = [r.latency_ms for r in answered]
        flagged = sum(r.answer.stop_reason == "citations_flagged" for r in answered)
        step_limited = sum(r.answer.stop_reason == "step_limit" for r in answered)
        lines += [
            f"- **Agent tokens:** {tokens_in:,} in, {tokens_out:,} out (judge not included)",
            f"- **Latency per case:** median {statistics.median(latencies) / 1000:.1f}s, "
            f"max {max(latencies) / 1000:.1f}s",
            f"- **Citations flagged:** {flagged}. **Step limit hit:** {step_limited}.",
        ]

    lines += ["", "## By category", "", "| Category | Passed |", "|---|---|"]
    categories: dict[str, list[CaseResult]] = {}
    for r in results:
        categories.setdefault(r.case.category, []).append(r)
    for category, rs in categories.items():
        lines.append(f"| {category} | {sum(r.passed for r in rs)} / {len(rs)} |")

    lines += [
        "",
        "## Cases",
        "",
        "| Case | Result | Tools called | Model calls | Tokens in / out | Latency |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        tokens = f"{r.answer.input_tokens:,} / {r.answer.output_tokens:,}" if r.answer else "–"
        tools = ", ".join(r.tools_called) or "–"
        lines.append(
            f"| `{r.case.id}` | {'pass' if r.passed else '**fail**'} | {tools} | "
            f"{r.model_calls} | {tokens} | {r.latency_ms / 1000:.1f}s |"
        )

    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["", "## Failures"]
        for r in failures:
            lines += ["", f"### `{r.case.id}`", "", f"> {r.case.question}", ""]
            if r.error:
                lines.append(f"- **error:** {r.error}")
            for c in r.checks:
                if not c.passed:
                    lines.append(f"- **{c.name}**" + (f": {c.detail}" if c.detail else ""))
            if r.answer:
                lines += ["", "<details><summary>Answer</summary>", "", r.answer.content, "", "</details>"]
    return "\n".join(lines) + "\n"


def _json_result(r: CaseResult) -> dict:
    return {
        "case": r.case.id,
        "passed": r.passed,
        "error": r.error,
        "latency_ms": r.latency_ms,
        "checks": [asdict(c) for c in r.checks],
        "answer": asdict(r.answer) if r.answer else None,
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument(
        "--judge-model", default=None,
        help="Bedrock model id for the judge (default: the agent's own model)",
    )
    args = parser.parse_args(argv)

    if not settings.BEDROCK_CHAT_MODEL_ID:
        print("BEDROCK_CHAT_MODEL_ID isn't set; the evals need a real model.", file=sys.stderr)
        return 2
    cases = CASES
    if args.case:
        unknown = set(args.case) - {c.id for c in CASES}
        if unknown:
            print(f"unknown case ids: {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in CASES if c.id in args.case]

    judge_id = None if args.no_judge else (args.judge_model or settings.BEDROCK_CHAT_MODEL_ID)
    judge_model = None
    if judge_id:
        judge_model = ChatBedrockConverse(
            client=bedrock.build_bedrock_client(Config(read_timeout=60)), model_id=judge_id, temperature=0, max_tokens=2048,
            disable_streaming=True,
        )

    await api_db.connect()
    cd_api = CdApiService(AsgiApiClient())
    agent = get_agent_service(cd_api, BillSearchService(cd_api))
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(case: Case) -> CaseResult:
        async with semaphore:
            result = await run_case(agent, judge_model, case)
            print(f"{'pass' if result.passed else 'FAIL'}  {case.id}", file=sys.stderr)
            return result

    try:
        results = await asyncio.gather(*(bounded(c) for c in cases))
    finally:
        await cd_api.aclose()
        await api_db.close()

    report = scorecard(results, settings.BEDROCK_CHAT_MODEL_ID, judge_id)
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RESULTS_DIR / f"{stamp}.md").write_text(report)
    (RESULTS_DIR / f"{stamp}.json").write_text(
        json.dumps([_json_result(r) for r in results], indent=1, default=str)
    )
    print(report)
    print(f"Wrote {RESULTS_DIR}/{stamp}.md and .json", file=sys.stderr)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
