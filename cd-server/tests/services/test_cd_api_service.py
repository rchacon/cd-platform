import asyncio

import httpx
import pytest

from cd.server.services.cd_api_service import (
    ApiClientError,
    CdApiService,
    get_cd_api_service,
)


class _FakeTransport:
    """Stands in for cd.api's app: answers every request with `result` at
    `status` (or raises `raises`), and records each call's (path, query)."""

    def __init__(self, result: dict | None = None, status: int = 200, raises=None):
        self.calls: list[tuple[str, dict[str, str]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append((request.url.path, dict(request.url.params)))
            if raises is not None:
                raise raises
            return httpx.Response(status, json=result)

        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://cd-api"
        )


def test_cd_api_service_raises_api_client_error_with_cd_apis_status_and_body():
    body = {"errors": [{"status": "404", "title": "Not Found"}]}
    transport = _FakeTransport(body, status=404)

    with pytest.raises(ApiClientError) as exc:
        asyncio.run(CdApiService(transport.client).member_detail("X000000"))

    assert exc.value.status_code == 404
    assert "Not Found" in exc.value.body


def test_cd_api_service_maps_a_transport_failure_to_502():
    transport = _FakeTransport(raises=httpx.ConnectError("refused"))

    with pytest.raises(ApiClientError) as exc:
        asyncio.run(CdApiService(transport.client).get_senators("CA"))

    assert exc.value.status_code == 502


def test_get_cd_api_service_calls_cd_api_in_process():
    # The real cd.api app, with no network and no database: /version reads
    # cd-server's VERSION file. Paths are relative to the app, not /v1.
    async def call() -> dict:
        service = get_cd_api_service()
        try:
            return await service._get("/version", {})
        finally:
            await service.aclose()

    assert "version" in asyncio.run(call())


def test_get_cd_api_service_surfaces_cd_apis_own_error_status():
    async def call() -> None:
        service = get_cd_api_service()
        try:
            await service._get("/no-such-route", {})
        finally:
            await service.aclose()

    with pytest.raises(ApiClientError) as exc:
        asyncio.run(call())
    assert exc.value.status_code == 404


_MEMBER = {
    "bioguide_id": "D000001",
    "first_name": "Jane",
    "middle_name": None,
    "last_name": "Doe",
    "nickname": None,
    "suffix": None,
    "party": "Democratic",
    "role": "Representative",
    "phone": None,
    "website": None,
    "photo_url": None,
    "district": 12,
}

_SENATOR = {**_MEMBER, "role": "Senator", "district": None}


def _resource(member: dict) -> dict:
    # A member as the new JSON:API `/members` shape sends it: bioguide_id
    # becomes the resource id, and `state`/`in_office` ride in attributes.
    attrs = {k: v for k, v in member.items() if k != "bioguide_id"}
    return {
        "type": "member",
        "id": member["bioguide_id"],
        "attributes": {**attrs, "state": "CA", "in_office": True},
    }


def test_cd_api_service_get_representatives_sends_filter_params():
    transport = _FakeTransport({"data": [_resource(_MEMBER)]})
    service = CdApiService(transport.client)

    members = asyncio.run(service.get_representatives("CA", 12))

    assert transport.calls == [(
        "/members",
        {"filter[state]": "CA", "filter[chamber]": "house", "filter[district]": "12"},
    )]
    assert [m.bioguide_id for m in members] == ["D000001"]
    assert members[0].district == 12


def test_cd_api_service_get_senators_sends_filter_params():
    transport = _FakeTransport({"data": [_resource(_SENATOR)]})
    service = CdApiService(transport.client)

    members = asyncio.run(service.get_senators("CA"))

    assert transport.calls == [
        ("/members", {"filter[state]": "CA", "filter[chamber]": "senate"})
    ]
    assert [m.bioguide_id for m in members] == ["D000001"]
    assert members[0].district is None


def test_cd_api_service_parses_the_jsonapi_collection_shape():
    # cd-api's /members returns one flat JSON:API `data` list for both
    # chambers; get_senators/get_representatives split it client-side.
    payload = {"data": [_resource(_SENATOR), _resource(_SENATOR | {"bioguide_id": "S000002"}),
                        _resource(_MEMBER)]}

    reps = asyncio.run(CdApiService(_FakeTransport(payload).client).get_representatives("CA", 12))
    sens = asyncio.run(CdApiService(_FakeTransport(payload).client).get_senators("CA"))

    assert [m.bioguide_id for m in reps] == ["D000001"]
    assert reps[0].district == 12
    assert {m.bioguide_id for m in sens} == {"D000001", "S000002"}
    assert all(m.district is None for m in sens)


def test_cd_api_service_narrows_representatives_to_the_requested_district():
    # Client-side backstop: if cd-api's filter[district] were ignored and
    # it returned the whole state's House delegation, get_representatives
    # must still hand back only the one district.
    payload = {"data": [
        _resource(_MEMBER | {"bioguide_id": "R000005", "district": 5}),
        _resource(_MEMBER | {"bioguide_id": "R000012", "district": 12}),
        _resource(_SENATOR),
    ]}

    reps = asyncio.run(CdApiService(_FakeTransport(payload).client).get_representatives("CA", 12))

    assert [m.bioguide_id for m in reps] == ["R000012"]


def test_cd_api_service_malformed_jsonapi_document_raises_validation_error():
    # The trust boundary holds for the new shape too: a resource missing
    # `attributes` is a ValidationError from CollectionDocument, not a
    # bare KeyError leaking out of the service.
    from pydantic import ValidationError

    payload = {"data": [{"type": "member", "id": "X000001"}]}

    with pytest.raises(ValidationError):
        asyncio.run(CdApiService(_FakeTransport(payload).client).get_senators("CA"))


def test_cd_api_service_drops_jsonapi_only_attributes():
    # `state`/`in_office` live in the resource attributes of the new
    # shape but aren't Member fields -- lenient Member drops them.
    payload = {"data": [_resource(_MEMBER)]}

    members = asyncio.run(CdApiService(_FakeTransport(payload).client).get_representatives("CA", 12))

    assert members[0].bioguide_id == "D000001"
    assert not hasattr(members[0], "state")
    assert not hasattr(members[0], "in_office")


def test_cd_api_service_ignores_unknown_fields_from_cd_api():
    # cd-lib's MemberDetail is lenient (extra="ignore"), so a field cd-api
    # adds to a /members resource's attributes doesn't break a cd-server
    # whose bundled cd-lib predates it -- the unknown field is dropped,
    # not rejected.
    resource = _resource(_MEMBER)
    resource["attributes"]["some_future_field"] = 1
    service = CdApiService(_FakeTransport({"data": [resource]}).client)

    members = asyncio.run(service.get_representatives("CA", 12))

    assert len(members) == 1
    assert members[0].bioguide_id == "D000001"
    assert not hasattr(members[0], "some_future_field")


def test_cd_api_service_aclose_closes_its_client():
    transport = _FakeTransport({"data": []})
    service = CdApiService(transport.client)

    asyncio.run(service.aclose())

    assert transport.client.is_closed


# --- member_detail (GET /members/{id}, for cd-webapp's detail page) ---


def test_member_detail_fetches_by_id_and_returns_typed_document():
    transport = _FakeTransport({"data": _resource(_MEMBER)})
    doc = asyncio.run(CdApiService(transport.client).member_detail("D000001"))

    assert transport.calls == [("/members/D000001", {})]
    assert doc.data.id == "D000001"
    assert doc.data.attributes.last_name == "Doe"
    # state / in_office -- the fields the list drops but a detail page needs
    assert doc.data.attributes.state == "CA"
    assert doc.data.attributes.in_office is True


def test_member_detail_malformed_envelope_raises_validation_error():
    from pydantic import ValidationError

    # single-resource `data` must be an object, not a list
    transport = _FakeTransport({"data": [_resource(_MEMBER)]})
    with pytest.raises(ValidationError):
        asyncio.run(CdApiService(transport.client).member_detail("D000001"))


def test_member_detail_propagates_a_404():
    transport = _FakeTransport({"errors": [{"status": "404"}]}, status=404)

    with pytest.raises(ApiClientError) as exc:
        asyncio.run(CdApiService(transport.client).member_detail("X000000"))
    assert exc.value.status_code == 404


# --- search_bills / member_votes (cd-platform#104 PR 6) ---

_BILL_RESOURCE = {
    "type": "bill",
    "id": "119-hr-2616",
    "attributes": {
        "congress": 119,
        "bill_type": "HR",
        "bill_number": 2616,
        "title": "A bill for things",
        "policy_area": "Education",
        "crs_summary": "<p>...</p>",
    },
    "meta": {"matches": [{"via": "policy_area"}]},
}

_VOTE_RESOURCE = {
    "type": "roll_call_vote",
    "id": "119-house-1-327:K000401",
    "attributes": {
        "vote_cast": "YEA",
        "vote_question": "On Passage",
        "result": "Passed",
        "vote_date": "2026-05-20",
    },
    "relationships": {
        "member": {"data": {"type": "member", "id": "K000401"}},
        "roll_call": {"data": {"type": "roll_call", "id": "119-house-1-327"}},
        "bill": {"data": {"type": "bill", "id": "119-hr-2616"}},
    },
}


def test_search_bills_sends_filter_query_and_returns_typed_collection():
    transport = _FakeTransport({"data": [_BILL_RESOURCE], "meta": {"query": "schools"}})
    service = CdApiService(transport.client)

    doc = asyncio.run(service.search_bills("schools"))

    assert transport.calls == [("/bills", {"filter[query]": "schools"})]
    assert [r.id for r in doc.data] == ["119-hr-2616"]
    assert doc.data[0].attributes.policy_area == "Education"
    # per-resource meta (why it matched) and document meta (echoed query)
    # both survive validation -- BillSearchService needs both.
    assert doc.data[0].meta == {"matches": [{"via": "policy_area"}]}
    assert doc.meta == {"query": "schools"}


def test_search_bills_passes_page_size_only_when_given():
    transport = _FakeTransport({"data": [], "meta": {"query": "q"}})
    asyncio.run(CdApiService(transport.client).search_bills("q", page_size=5))
    assert transport.calls == [("/bills", {"filter[query]": "q", "page[size]": "5"})]


def test_search_bills_malformed_envelope_raises_validation_error():
    from pydantic import ValidationError

    # a resource missing `attributes` -- the trust boundary, same as _members
    transport = _FakeTransport({"data": [{"type": "bill", "id": "119-hr-1"}]})
    with pytest.raises(ValidationError):
        asyncio.run(CdApiService(transport.client).search_bills("q"))


def test_member_votes_sends_comma_joined_filter_bill_and_returns_typed_collection():
    transport = _FakeTransport(
        {"data": [_VOTE_RESOURCE], "meta": {"bills_without_votes": ["119-s-5"]}}
    )
    service = CdApiService(transport.client)

    doc = asyncio.run(service.member_votes("K000401", ["119-hr-2616", "119-s-5"]))

    assert transport.calls == [
        ("/members/K000401/votes", {"filter[bill]": "119-hr-2616,119-s-5"})
    ]
    assert doc.data[0].attributes.vote_cast == "YEA"
    # the derived bill edge BillSearchService groups on
    assert doc.data[0].relationships["bill"].data.id == "119-hr-2616"
    assert doc.meta == {"bills_without_votes": ["119-s-5"]}


def test_member_votes_malformed_envelope_raises_validation_error():
    from pydantic import ValidationError

    transport = _FakeTransport({"data": [{"type": "roll_call_vote"}]})
    with pytest.raises(ValidationError):
        asyncio.run(CdApiService(transport.client).member_votes("K000401", ["119-hr-1"]))


def test_search_bills_propagates_a_503():
    transport = _FakeTransport({"errors": [{"status": "503"}]}, status=503)

    with pytest.raises(ApiClientError) as exc:
        asyncio.run(CdApiService(transport.client).search_bills("q"))
    assert exc.value.status_code == 503
