from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from cd.lib.jsonapi import Resource
from cd.lib.models import Bill, RollCallVote

from cd.server.services.cd_api_service import CdApiService

# cd-api's GET /members/{id}/votes rejects a filter[bill] naming more than
# this many ids (its own MAX_VOTE_BILLS). GET /bills' page[size] cap is a
# separate constant over there that happens to match today; `search()`
# clamps to this so the votes hop can't 422 if the two ever diverge.
_MAX_VOTE_BILLS = 50


@dataclass(frozen=True)
class VoteResult:
    """One cast position by the queried member on one roll call. Flattened
    from a cd-api `roll_call_vote` resource -- the bill/member/roll_call
    linkage lives on the resource's `relationships`, not here; by the time
    a `VoteResult` exists it has already been grouped onto its bill."""

    vote_cast: str
    vote_question: str
    result: str
    vote_date: date


@dataclass(frozen=True)
class BillResult:
    """A bill from `GET /bills`, merged with the queried member's votes on
    it. `discoverBills` (no member) produces these with `votes` always
    `[]`; `searchBills` fills `votes` from `GET /members/{id}/votes` --
    still `[]`, never absent, for a bill the member didn't vote on (the
    "matched, no vote on record" state is first-class -- cd-platform#104).
    """

    # the cd-api resource id -- bills.bill_key, e.g. "119-hr-2616"
    bill_key: str
    congress: int
    bill_type: str
    bill_number: int
    title: str | None
    policy_area: str | None
    crs_summary: str | None
    # why this bill surfaced for this search -- passed through from the
    # resource's `meta.matches` verbatim ([{"via": "policy_area"}, ...]);
    # its shape is cd-api's to evolve (cd-platform#131/#132), so it stays
    # opaque here.
    matches: list[dict[str, Any]]
    votes: list[VoteResult] = field(default_factory=list)


def _bill_result(resource: Resource[Bill], votes: list[VoteResult]) -> BillResult:
    attrs = resource.attributes
    return BillResult(
        bill_key=resource.id,
        congress=attrs.congress,
        bill_type=attrs.bill_type,
        bill_number=attrs.bill_number,
        title=attrs.title,
        policy_area=attrs.policy_area,
        crs_summary=attrs.crs_summary,
        matches=list((resource.meta or {}).get("matches", [])),
        votes=votes,
    )


def _vote_result(attrs: RollCallVote) -> VoteResult:
    return VoteResult(
        vote_cast=attrs.vote_cast,
        vote_question=attrs.vote_question,
        result=attrs.result,
        vote_date=attrs.vote_date,
    )


def _bill_id_of(vote: Resource[RollCallVote]) -> str | None:
    """The bill a `roll_call_vote` resource points at, via its derived
    `bill` relationship -- cd-api carries this edge directly so votes can
    be grouped by bill without traversing the roll call. `None` if the
    linkage is somehow absent (a cd-api contract break; the vote is then
    dropped rather than crashing the query)."""
    rel = (vote.relationships or {}).get("bill")
    if rel is None or isinstance(rel.data, list):
        return None
    return rel.data.id


class BillSearchService:
    """Composes cd-api's `GET /bills` (topic search) and
    `GET /members/{id}/votes` (per-member voting record) into one list of
    `BillResult`s, zipped by bill id -- the merge cd-webapp's
    representative-topic view needs, kept out of the GraphQL resolver.

    The two cd-api hops are sequential by necessity: the vote lookup is
    filtered by the ids the search returns.
    """

    def __init__(self, cd_api: CdApiService):
        self._cd_api = cd_api

    async def discover(
        self, query: str, page_size: int | None = None
    ) -> list[BillResult]:
        """Topic search with no member -- bills and why they matched, no
        votes. Backs `discoverBills`."""
        bills = await self._cd_api.search_bills(query, page_size)
        return [_bill_result(r, votes=[]) for r in bills.data]

    async def search(
        self, bioguide_id: str, query: str, page_size: int | None = None
    ) -> list[BillResult]:
        """Topic search plus how `bioguide_id` voted on each matched bill.
        Backs `searchBills`."""
        # Never ask for more bills than the votes endpoint will accept in
        # one filter[bill] -- keeps the second hop self-consistent (every
        # returned bill gets a real vote lookup) rather than 422ing.
        # `None` stays `None` (cd-api's own default is well under the cap).
        if page_size is not None:
            page_size = min(page_size, _MAX_VOTE_BILLS)
        bills = await self._cd_api.search_bills(query, page_size)
        if not bills.data:
            # No bills -> nothing to filter votes by; skip the second hop
            # (an empty filter[bill] is a 422 anyway).
            return []

        bill_keys = [r.id for r in bills.data]
        votes = await self._cd_api.member_votes(bioguide_id, bill_keys)

        votes_by_bill: dict[str, list[VoteResult]] = {}
        for vote in votes.data:
            bill_key = _bill_id_of(vote)
            if bill_key is not None:
                votes_by_bill.setdefault(bill_key, []).append(_vote_result(vote.attributes))

        # Search-relevance order preserved; a matched bill with no vote
        # keeps an explicit empty list.
        return [_bill_result(r, votes=votes_by_bill.get(r.id, [])) for r in bills.data]
