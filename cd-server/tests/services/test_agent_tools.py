from datetime import date

import pytest
from cd.lib.jsonapi import CollectionDocument, Document
from cd.lib.models import MemberDetail

from cd.server.services.agent_tools import TOOL_NAMES, AgentTools
from cd.server.services.bill_search_service import BillResult, VoteResult
from cd.server.services.cd_api_service import ApiClientError

pytestmark = pytest.mark.anyio


def _member(bioguide_id: str, **attrs) -> dict:
    return {
        "type": "member",
        "id": bioguide_id,
        "attributes": {
            "first_name": "Bernard",
            "nickname": "Bernie",
            "last_name": "Sanders",
            "role": "Senator",
            "party": "Independent",
            "state": "VT",
            "in_office": True,
            **attrs,
        },
    }


class _StubCdApi:
    def __init__(self, members=None, error=None):
        self._members = members or []
        self._error = error
        self.calls = []

    async def find_members(self, name, state, chamber, district):
        self.calls.append((name, state, chamber, district))
        if self._error:
            raise self._error
        return CollectionDocument[MemberDetail].model_validate({"data": self._members})

    async def member_detail(self, bioguide_id):
        if self._error:
            raise self._error
        return Document[MemberDetail].model_validate({"data": self._members[0]})


class _StubBillSearch:
    def __init__(self, bills=None, votes=None):
        self._bills = bills or []
        self._votes = votes or {}
        self.calls = []

    async def discover(self, query, page_size):
        self.calls.append((query, page_size))
        return self._bills

    async def votes_by_bill(self, bioguide_id, bill_keys):
        self.calls.append((bioguide_id, bill_keys))
        return self._votes


def _tools(cd_api=None, bill_search=None) -> AgentTools:
    return AgentTools(cd_api or _StubCdApi(), bill_search or _StubBillSearch())


def test_tool_names_are_the_public_coroutine_methods():
    public = {n for n in dir(AgentTools) if not n.startswith("_") and n != "TOOL_NAMES"}
    assert set(TOOL_NAMES) == public


async def test_find_members_needs_a_name_or_a_state():
    cd_api = _StubCdApi()

    assert "error" in await _tools(cd_api).find_members()
    assert cd_api.calls == []


async def test_find_members_returns_ids_and_display_names():
    cd_api = _StubCdApi([_member("S000033")])

    result = await _tools(cd_api).find_members(name="sanders", chamber="senate")

    assert cd_api.calls == [("sanders", None, "senate", None)]
    assert result == {
        "members": [
            {
                "id": "S000033",
                "name": "Bernie Sanders",  # the nickname, not the given name
                "role": "Senator",
                "party": "Independent",
                "state": "VT",
                "district": None,
                "in_office": True,
            }
        ]
    }


async def test_a_failed_lookup_is_an_error_result_not_an_exception():
    tools = _tools(_StubCdApi(error=ApiClientError(404, "not found")))
    expected = {"error": "cd-api returned 404: not found"}

    assert await tools.find_members(state="VT") == expected
    assert await tools.get_member(member_id="X000000") == expected


async def test_get_member_adds_contact_details():
    cd_api = _StubCdApi([_member("S000033", phone="202-555-0100", website="https://x.gov")])

    result = await _tools(cd_api).get_member(member_id="S000033")

    assert result["id"] == "S000033"
    assert (result["phone"], result["website"]) == ("202-555-0100", "https://x.gov")


async def test_search_bills_shortens_long_summaries_and_caps_the_limit():
    bill = BillResult(
        bill_key="119-hr-1", congress=119, bill_type="HR", bill_number=1,
        title="A bill", policy_area="Crime", crs_summary="x" * 5000,
        matches=[{"via": "summary"}],
    )
    bill_search = _StubBillSearch(bills=[bill])

    result = await _tools(bill_search=bill_search).search_bills(query="guns", limit=99)

    assert bill_search.calls == [("guns", 20)]
    (found,) = result["bills"]
    assert found["id"] == "119-hr-1"
    assert found["matched_via"] == ["summary"]
    assert len(found["summary"]) == 600 and found["summary"].endswith("…")


async def test_get_member_votes_lists_every_requested_bill_even_without_votes():
    vote = VoteResult("Yea", "On Passage", "Passed", date(2026, 5, 20))
    bill_search = _StubBillSearch(votes={"119-hr-1": [vote]})

    result = await _tools(bill_search=bill_search).get_member_votes(
        member_id="P000197", bill_ids=["119-hr-1", "119-hr-2"]
    )

    assert result == {
        "member_id": "P000197",
        "bills": [
            {
                "id": "119-hr-1",
                "votes": [
                    {"question": "On Passage", "vote": "Yea", "result": "Passed", "date": "2026-05-20"}
                ],
            },
            {"id": "119-hr-2", "votes": []},
        ],
    }
