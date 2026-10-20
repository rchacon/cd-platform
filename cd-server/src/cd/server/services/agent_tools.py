"""The research agent's tools (cd-platform#164), as plain async methods.

Nothing here knows about LangChain or MCP: agent_service.py wraps these
methods as LangChain tools for the LangGraph agent, and the MCP server
exposes the same methods to other clients. Both read each tool's name,
docstring and `Annotated[..., Field(description=...)]` parameters, so the
docstrings are the model-facing descriptions -- written for the model
deciding which tool to call, not for a human reading the code.

Every result is JSON-serializable, and every member and bill in it
carries an `id` (a bioguide id or a bill key). Those ids are what the
agent cites, and what agent_service.py's citation check verifies its
answer against.

A failed lookup (an unknown bioguide id, a malformed bill key, cd-api
down) comes back as `{"error": "..."}` rather than raising, so the model
can see what went wrong and try something else.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from cd.lib.jsonapi import Resource
from cd.lib.models import MemberDetail
from pydantic import Field, ValidationError

from cd.server.services.bill_search_service import (
    MAX_VOTE_BILLS,
    BillSearchService,
)
from cd.server.services.cd_api_service import ApiClientError, CdApiService

# CRS summaries can run to thousands of characters; the agent needs the
# gist to judge relevance, not the whole text, and every character is
# input tokens on every later model call in the turn.
_SUMMARY_MAX_CHARS = 600
_MAX_SEARCH_RESULTS = 20


def _member(resource: Resource[MemberDetail]) -> dict[str, Any]:
    a = resource.attributes
    name = " ".join(
        part for part in (a.nickname or a.first_name, a.last_name, a.suffix) if part
    )
    return {
        "id": resource.id,
        "name": name or resource.id,
        "role": a.role,
        "party": a.party,
        "state": a.state,
        "district": a.district,
        "in_office": a.in_office,
    }


def _truncate(text: str | None) -> str | None:
    if text is None or len(text) <= _SUMMARY_MAX_CHARS:
        return text
    return text[: _SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def _error(exc: Exception) -> dict[str, str]:
    return {"error": str(exc)}


class AgentTools:
    """The tool methods, bound to the services they read through. The
    public coroutine methods listed in `TOOL_NAMES` are the tools."""

    def __init__(self, cd_api: CdApiService, bill_search: BillSearchService):
        self._cd_api = cd_api
        self._bill_search = bill_search

    async def find_members(
        self,
        name: Annotated[
            str | None,
            Field(description="Part or all of a member's name, e.g. 'pelosi' or 'bernie sanders'."),
        ] = None,
        state: Annotated[
            str | None,
            Field(description="Two-letter state or territory code, e.g. 'CA'."),
        ] = None,
        chamber: Annotated[
            Literal["senate", "house"] | None,
            Field(description="Limit to one chamber."),
        ] = None,
        district: Annotated[
            int | None,
            Field(description="House district number (0 for at-large). Requires state."),
        ] = None,
    ) -> dict[str, Any]:
        """Find current members of Congress by name, or list a state's
        delegation. Give a name, a state, or both. Use this to turn a name
        or "my senators" into member ids before calling any other tool
        that needs one. Returns each member's id, name, role, party,
        state, district and whether they're still in office."""
        if name is None and state is None:
            return {"error": "Give a name, a state, or both."}
        try:
            doc = await self._cd_api.find_members(name, state, chamber, district)
        except (ApiClientError, ValidationError) as exc:
            return _error(exc)
        return {"members": [_member(r) for r in doc.data]}

    async def get_member(
        self,
        member_id: Annotated[str, Field(description="A member's bioguide id, e.g. 'S000033'.")],
    ) -> dict[str, Any]:
        """Get one member of the current Congress by id, including members
        who have left office during this Congress."""
        try:
            doc = await self._cd_api.member_detail(member_id)
        except (ApiClientError, ValidationError) as exc:
            return _error(exc)
        member = _member(doc.data)
        a = doc.data.attributes
        member.update(phone=a.phone, website=a.website)
        return member

    async def search_bills(
        self,
        query: Annotated[
            str,
            Field(description="A topic in plain words, e.g. 'gun background checks'."),
        ],
        limit: Annotated[
            int,
            Field(ge=1, le=_MAX_SEARCH_RESULTS, description="How many bills to return."),
        ] = 10,
    ) -> dict[str, Any]:
        """Search bills in the current Congress by topic. Only bills that
        have had a recorded House vote are indexed. Returns each bill's
        id, title, policy area, a shortened CRS summary, and how it
        matched: 'policy_area' or 'subject' (an exact topic match) or
        'summary' (semantic similarity, so check that the title and
        summary are really on topic). Specific everyday phrasing can miss
        bills that broader or more formal wording finds."""
        try:
            bills = await self._bill_search.discover(query, min(limit, _MAX_SEARCH_RESULTS))
        except (ApiClientError, ValidationError) as exc:
            return _error(exc)
        return {
            "bills": [
                {
                    "id": b.bill_key,
                    "title": b.title,
                    "policy_area": b.policy_area,
                    "summary": _truncate(b.crs_summary),
                    "matched_via": [m.get("via") for m in b.matches],
                }
                for b in bills
            ]
        }

    async def get_member_votes(
        self,
        member_id: Annotated[str, Field(description="A member's bioguide id.")],
        bill_ids: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=MAX_VOTE_BILLS,
                description="Bill ids from search_bills, e.g. ['119-hr-2616'].",
            ),
        ],
    ) -> dict[str, Any]:
        """How one member voted on specific bills. Only House roll-call
        votes are recorded so far, so a senator has no votes here. Each
        bill lists every roll call the member voted in, with the question
        (procedural motions such as 'On Motion to Recommit' are not votes
        on the bill itself), their vote, the result and the date. A bill
        with an empty list means no recorded vote by this member."""
        try:
            grouped = await self._bill_search.votes_by_bill(member_id, bill_ids)
        except (ApiClientError, ValidationError) as exc:
            return _error(exc)
        return {
            "member_id": member_id,
            "bills": [
                {
                    "id": bill_id,
                    "votes": [
                        {
                            "question": v.vote_question,
                            "vote": v.vote_cast,
                            "result": v.result,
                            "date": v.vote_date.isoformat(),
                        }
                        for v in grouped.get(bill_id, [])
                    ],
                }
                for bill_id in bill_ids
            ],
        }


TOOL_NAMES = ("find_members", "get_member", "search_bills", "get_member_votes")
