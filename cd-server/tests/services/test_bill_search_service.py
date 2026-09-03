import asyncio

from cd.lib.jsonapi import CollectionDocument
from cd.lib.models import Bill, RollCallVote

from cd.server.services.bill_search_service import BillSearchService


def _bill(bill_key: str, *, via: str = "policy_area", **attr_overrides) -> dict:
    congress, bill_type, number = bill_key.split("-")
    return {
        "type": "bill",
        "id": bill_key,
        "attributes": {
            "congress": int(congress),
            "bill_type": bill_type.upper(),
            "bill_number": int(number),
            "title": f"A bill about {bill_key}",
            "policy_area": "Education",
            "crs_summary": "<p>...</p>",
            **attr_overrides,
        },
        "meta": {"matches": [{"via": via}]},
    }


def _vote(bill_key: str | None, *, cast: str = "YEA", date_: str = "2026-05-20") -> dict:
    resource = {
        "type": "roll_call_vote",
        "id": f"119-house-1-1:{bill_key or 'x'}",
        "attributes": {
            "vote_cast": cast,
            "vote_question": "On Passage",
            "result": "Passed",
            "vote_date": date_,
        },
    }
    if bill_key is not None:
        resource["relationships"] = {
            "member": {"data": {"type": "member", "id": "K000401"}},
            "roll_call": {"data": {"type": "roll_call", "id": "119-house-1-1"}},
            "bill": {"data": {"type": "bill", "id": bill_key}},
        }
    return resource


def _bills_doc(*bills: dict, query: str = "schools") -> CollectionDocument[Bill]:
    return CollectionDocument[Bill].model_validate({"data": list(bills), "meta": {"query": query}})


def _votes_doc(*votes: dict, without: list[str] | None = None) -> CollectionDocument[RollCallVote]:
    return CollectionDocument[RollCallVote].model_validate(
        {"data": list(votes), "meta": {"bills_without_votes": without or []}}
    )


class _StubCdApi:
    def __init__(self, bills_doc, votes_doc=None):
        self._bills_doc = bills_doc
        self._votes_doc = votes_doc
        self.search_calls: list[tuple[str, int | None]] = []
        self.votes_calls: list[tuple[str, list[str]]] = []

    async def search_bills(self, query, page_size=None):
        self.search_calls.append((query, page_size))
        return self._bills_doc

    async def member_votes(self, bioguide_id, bill_keys):
        self.votes_calls.append((bioguide_id, bill_keys))
        return self._votes_doc


def test_discover_returns_bills_with_matches_and_no_votes():
    stub = _StubCdApi(_bills_doc(_bill("119-hr-2616", via="subject")))
    results = asyncio.run(BillSearchService(stub).discover("schools", page_size=5))

    assert stub.search_calls == [("schools", 5)]
    assert stub.votes_calls == []  # discover never asks for votes
    assert len(results) == 1
    assert results[0].bill_key == "119-hr-2616"
    assert results[0].bill_type == "HR"
    assert results[0].matches == [{"via": "subject"}]
    assert results[0].votes == []


def test_search_zips_votes_onto_the_right_bill_by_id():
    stub = _StubCdApi(
        _bills_doc(_bill("119-hr-2616"), _bill("119-s-5")),
        _votes_doc(_vote("119-hr-2616", cast="NAY"), without=["119-s-5"]),
    )
    results = asyncio.run(BillSearchService(stub).search("K000401", "schools"))

    by_key = {r.bill_key: r for r in results}
    assert [v.vote_cast for v in by_key["119-hr-2616"].votes] == ["NAY"]
    # matched but no vote on record -- explicit empty list, not a missing entry
    assert by_key["119-s-5"].votes == []


def test_search_passes_the_returned_bill_keys_to_member_votes():
    stub = _StubCdApi(
        _bills_doc(_bill("119-hr-2616"), _bill("119-s-5")),
        _votes_doc(),
    )
    asyncio.run(BillSearchService(stub).search("K000401", "schools"))

    assert stub.votes_calls == [("K000401", ["119-hr-2616", "119-s-5"])]


def test_search_preserves_search_relevance_order():
    stub = _StubCdApi(
        _bills_doc(_bill("119-s-5"), _bill("119-hr-2616")),
        _votes_doc(),
    )
    results = asyncio.run(BillSearchService(stub).search("K000401", "q"))
    assert [r.bill_key for r in results] == ["119-s-5", "119-hr-2616"]


def test_search_groups_multiple_votes_on_one_bill_keeping_order():
    stub = _StubCdApi(
        _bills_doc(_bill("119-hr-2616")),
        _votes_doc(
            _vote("119-hr-2616", cast="NAY", date_="2026-03-01"),
            _vote("119-hr-2616", cast="YEA", date_="2026-05-20"),
        ),
    )
    results = asyncio.run(BillSearchService(stub).search("K000401", "q"))
    assert [v.vote_cast for v in results[0].votes] == ["NAY", "YEA"]


def test_search_with_no_matching_bills_skips_the_votes_call():
    stub = _StubCdApi(_bills_doc())  # empty data
    results = asyncio.run(BillSearchService(stub).search("K000401", "nothing matches"))

    assert results == []
    assert stub.votes_calls == []  # an empty filter[bill] would 422


def test_search_drops_a_vote_resource_with_no_bill_linkage():
    stub = _StubCdApi(
        _bills_doc(_bill("119-hr-2616")),
        _votes_doc(_vote(None), _vote("119-hr-2616")),  # first has no relationships
    )
    results = asyncio.run(BillSearchService(stub).search("K000401", "q"))
    assert [v.vote_cast for v in results[0].votes] == ["YEA"]  # only the linked one
