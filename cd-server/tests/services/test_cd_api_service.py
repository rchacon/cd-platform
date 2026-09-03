import asyncio
import io
import json

import httpx
import pytest

from cd.server import settings
from cd.server.services.cd_api_service import (
    ApiClient,
    ApiClientError,
    CdApiService,
    HttpApiClient,
    LambdaApiClient,
    _build_gateway_event,
    get_cd_api_service,
)


def test_both_clients_implement_the_shared_interface():
    assert issubclass(HttpApiClient, ApiClient)
    assert issubclass(LambdaApiClient, ApiClient)


def test_incomplete_subclass_cannot_be_instantiated():
    class Incomplete(ApiClient):
        pass

    with pytest.raises(TypeError):
        Incomplete()


def test_build_gateway_event_shape():
    event = _build_gateway_event("/v1/members", {"state": "CA", "district": "12"})
    assert event["resource"] == "/{proxy+}"
    assert event["path"] == "/v1/members"
    assert event["httpMethod"] == "GET"
    assert event["multiValueQueryStringParameters"] == {"state": ["CA"], "district": ["12"]}
    assert "requestContext" in event


def test_build_gateway_event_empty_query():
    event = _build_gateway_event("/v1/version", {})
    assert event["multiValueQueryStringParameters"] == {}


def test_http_api_client_returns_json_on_success(monkeypatch):
    async def fake_get(self, url, params=None):
        assert url == "http://cd-api:8000/version"
        assert params == {}
        return httpx.Response(200, json={"version": "1.2.3"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    client = HttpApiClient("http://cd-api:8000")
    assert asyncio.run(client.get("/version", {})) == {"version": "1.2.3"}


def test_http_api_client_passes_query_params(monkeypatch):
    captured = {}

    async def fake_get(self, url, params=None):
        captured["url"] = url
        captured["params"] = params
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    asyncio.run(HttpApiClient("http://cd-api:8000").get("/members", {"state": "CA"}))
    assert captured["url"] == "http://cd-api:8000/members"
    assert captured["params"] == {"state": "CA"}


def test_http_api_client_raises_on_error_response(monkeypatch):
    async def fake_get(self, url, params=None):
        return httpx.Response(
            404, json={"detail": "not found"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(HttpApiClient("http://cd-api:8000").get("/members", {"state": "ZZ"}))
    assert exc_info.value.status_code == 404


def test_http_api_client_wraps_connection_errors(monkeypatch):
    async def fake_get(self, url, params=None):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(HttpApiClient("http://cd-api:8000").get("/version", {}))
    assert exc_info.value.status_code == 502


def test_http_api_client_aclose_closes_underlying_client():
    client = HttpApiClient("http://cd-api:8000")
    assert client._client.is_closed is False
    asyncio.run(client.aclose())
    assert client._client.is_closed is True


class _FakeLambdaClient:
    def __init__(self, payload: dict, function_error: str | None = None):
        self._payload = payload
        self._function_error = function_error
        self.last_invoke_kwargs: dict | None = None

    def invoke(self, FunctionName, Payload):
        self.last_invoke_kwargs = {"FunctionName": FunctionName, "Payload": Payload}
        response = {"Payload": io.BytesIO(json.dumps(self._payload).encode())}
        if self._function_error:
            response["FunctionError"] = self._function_error
        return response


def test_lambda_api_client_returns_json_on_success(monkeypatch):
    fake = _FakeLambdaClient({"statusCode": 200, "body": json.dumps({"version": "1.2.3"})})
    monkeypatch.setattr("boto3.client", lambda service: fake)
    client = LambdaApiClient("cd-platform-cd-api")
    assert asyncio.run(client.get("/version", {})) == {"version": "1.2.3"}


def test_lambda_api_client_builds_v1_prefixed_event_with_query(monkeypatch):
    fake = _FakeLambdaClient({"statusCode": 200, "body": "{}"})
    monkeypatch.setattr("boto3.client", lambda service: fake)
    asyncio.run(LambdaApiClient("cd-platform-cd-api").get("/members", {"state": "CA"}))

    assert fake.last_invoke_kwargs["FunctionName"] == "cd-platform-cd-api"
    event = json.loads(fake.last_invoke_kwargs["Payload"])
    assert event["path"] == "/v1/members"
    assert event["multiValueQueryStringParameters"] == {"state": ["CA"]}


def test_lambda_api_client_raises_on_error_status(monkeypatch):
    fake = _FakeLambdaClient({"statusCode": 404, "body": json.dumps({"detail": "not found"})})
    monkeypatch.setattr("boto3.client", lambda service: fake)
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(LambdaApiClient("cd-platform-cd-api").get("/members", {"state": "ZZ"}))
    assert exc_info.value.status_code == 404


def test_lambda_api_client_raises_on_function_error(monkeypatch):
    fake = _FakeLambdaClient({"errorMessage": "boom"}, function_error="Unhandled")
    monkeypatch.setattr("boto3.client", lambda service: fake)
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(LambdaApiClient("cd-platform-cd-api").get("/version", {}))
    assert exc_info.value.status_code == 500


def test_lambda_api_client_wraps_boto_client_error(monkeypatch):
    from botocore.exceptions import ClientError

    class _RaisingLambdaClient:
        def invoke(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no such function"}},
                "Invoke",
            )

    monkeypatch.setattr("boto3.client", lambda service: _RaisingLambdaClient())
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(LambdaApiClient("does-not-exist").get("/version", {}))
    assert exc_info.value.status_code == 502


def test_lambda_api_client_wraps_botocore_error(monkeypatch):
    from botocore.exceptions import NoCredentialsError

    class _RaisingLambdaClient:
        def invoke(self, **kwargs):
            raise NoCredentialsError()

    monkeypatch.setattr("boto3.client", lambda service: _RaisingLambdaClient())
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(LambdaApiClient("cd-platform-cd-api").get("/version", {}))
    assert exc_info.value.status_code == 502


def test_lambda_api_client_wraps_malformed_response(monkeypatch):
    # No "statusCode"/"body" keys and no FunctionError set -- an
    # unexpected/malformed payload Mangum's own contract shouldn't
    # actually produce, but shouldn't surface as a raw KeyError either.
    fake = _FakeLambdaClient({"unexpected": "shape"})
    monkeypatch.setattr("boto3.client", lambda service: fake)
    with pytest.raises(ApiClientError) as exc_info:
        asyncio.run(LambdaApiClient("cd-platform-cd-api").get("/version", {}))
    assert exc_info.value.status_code == 502


def test_lambda_api_client_aclose_is_a_noop(monkeypatch):
    monkeypatch.setattr("boto3.client", lambda service: _FakeLambdaClient({}))
    client = LambdaApiClient("cd-platform-cd-api")
    asyncio.run(client.aclose())  # should not raise


def test_get_cd_api_service_returns_http_client_for_local(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(settings, "CD_API_BASE_URL", "http://example:8000")
    service = get_cd_api_service()
    assert isinstance(service, CdApiService)
    assert isinstance(service._transport, HttpApiClient)
    assert service._transport.base_url == "http://example:8000"


def test_get_cd_api_service_returns_lambda_client_otherwise(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "CD_API_FUNCTION_NAME", "cd-platform-cd-api")
    monkeypatch.setattr("boto3.client", lambda service: _FakeLambdaClient({}))
    service = get_cd_api_service()
    assert isinstance(service, CdApiService)
    assert isinstance(service._transport, LambdaApiClient)
    assert service._transport.function_name == "cd-platform-cd-api"


def test_get_cd_api_service_fails_fast_when_function_name_missing(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "CD_API_FUNCTION_NAME", "")
    with pytest.raises(RuntimeError, match="CD_API_FUNCTION_NAME"):
        get_cd_api_service()


class _FakeTransport(ApiClient):
    def __init__(self, result: dict):
        self._result = result
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    async def get(self, path: str, query: dict[str, str]) -> dict:
        self.calls.append((path, query))
        return self._result

    async def aclose(self) -> None:
        self.closed = True


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
    service = CdApiService(transport)

    members = asyncio.run(service.get_representatives("CA", 12))

    assert transport.calls == [(
        "/members",
        {"filter[state]": "CA", "filter[chamber]": "house", "filter[district]": "12"},
    )]
    assert [m.bioguide_id for m in members] == ["D000001"]
    assert members[0].district == 12


def test_cd_api_service_get_senators_sends_filter_params():
    transport = _FakeTransport({"data": [_resource(_SENATOR)]})
    service = CdApiService(transport)

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

    reps = asyncio.run(CdApiService(_FakeTransport(payload)).get_representatives("CA", 12))
    sens = asyncio.run(CdApiService(_FakeTransport(payload)).get_senators("CA"))

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

    reps = asyncio.run(CdApiService(_FakeTransport(payload)).get_representatives("CA", 12))

    assert [m.bioguide_id for m in reps] == ["R000012"]


def test_cd_api_service_malformed_jsonapi_document_raises_validation_error():
    # The trust boundary holds for the new shape too: a resource missing
    # `attributes` is a ValidationError from CollectionDocument, not a
    # bare KeyError leaking out of the service.
    from pydantic import ValidationError

    payload = {"data": [{"type": "member", "id": "X000001"}]}

    with pytest.raises(ValidationError):
        asyncio.run(CdApiService(_FakeTransport(payload)).get_senators("CA"))


def test_cd_api_service_drops_jsonapi_only_attributes():
    # `state`/`in_office` live in the resource attributes of the new
    # shape but aren't Member fields -- lenient Member drops them.
    payload = {"data": [_resource(_MEMBER)]}

    members = asyncio.run(CdApiService(_FakeTransport(payload)).get_representatives("CA", 12))

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
    service = CdApiService(_FakeTransport({"data": [resource]}))

    members = asyncio.run(service.get_representatives("CA", 12))

    assert len(members) == 1
    assert members[0].bioguide_id == "D000001"
    assert not hasattr(members[0], "some_future_field")


def test_cd_api_service_aclose_delegates_to_transport():
    transport = _FakeTransport({"data": []})
    service = CdApiService(transport)

    asyncio.run(service.aclose())

    assert transport.closed is True


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
    service = CdApiService(transport)

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
    asyncio.run(CdApiService(transport).search_bills("q", page_size=5))
    assert transport.calls == [("/bills", {"filter[query]": "q", "page[size]": "5"})]


def test_search_bills_malformed_envelope_raises_validation_error():
    from pydantic import ValidationError

    # a resource missing `attributes` -- the trust boundary, same as _members
    transport = _FakeTransport({"data": [{"type": "bill", "id": "119-hr-1"}]})
    with pytest.raises(ValidationError):
        asyncio.run(CdApiService(transport).search_bills("q"))


def test_member_votes_sends_comma_joined_filter_bill_and_returns_typed_collection():
    transport = _FakeTransport(
        {"data": [_VOTE_RESOURCE], "meta": {"bills_without_votes": ["119-s-5"]}}
    )
    service = CdApiService(transport)

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
        asyncio.run(CdApiService(transport).member_votes("K000401", ["119-hr-1"]))


def test_search_bills_propagates_transport_error():
    class _Failing(ApiClient):
        async def get(self, path, query):
            raise ApiClientError(503, "bedrock down")

    with pytest.raises(ApiClientError) as exc:
        asyncio.run(CdApiService(_Failing()).search_bills("q"))
    assert exc.value.status_code == 503
