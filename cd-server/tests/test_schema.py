import httpx
import pytest
from fastapi.testclient import TestClient

from cd.server.app import app
from cd.server.schema import cd_api_service, geocoder_service, schema, users_service
from cd.server.services.users_service import InvalidTokenError


@pytest.fixture
def client():
    # Starlette's TestClient only triggers ASGI lifespan startup/shutdown
    # (app.py's lifespan(), which closes cd_api_service/geocoder_service on
    # exit) when used as a context manager -- a bare TestClient(app) never
    # runs it at all.
    with TestClient(app) as client:
        yield client


def test_lifespan_closes_both_services_on_shutdown(monkeypatch):
    # Spies on aclose() itself rather than reaching into internal
    # connection-pool state (e.g. cd_api_service._transport._client.is_closed)
    # -- cd_api_service/geocoder_service are module-level singletons shared
    # across the whole test session, so asserting on their actual open/closed
    # state would make this test's outcome depend on whether some earlier
    # test's own `with TestClient(app):` already triggered this same
    # shutdown (aclose() is idempotent but not reversible). Spying on the
    # call itself sidesteps that ordering fragility entirely.
    cd_api_service_closed = False
    geocoder_service_closed = False

    original_cd_api_aclose = cd_api_service.aclose
    original_geocoder_aclose = geocoder_service.aclose

    async def spy_cd_api_aclose():
        nonlocal cd_api_service_closed
        cd_api_service_closed = True
        await original_cd_api_aclose()

    async def spy_geocoder_aclose():
        nonlocal geocoder_service_closed
        geocoder_service_closed = True
        await original_geocoder_aclose()

    monkeypatch.setattr(cd_api_service, "aclose", spy_cd_api_aclose)
    monkeypatch.setattr(geocoder_service, "aclose", spy_geocoder_aclose)

    with TestClient(app):
        assert cd_api_service_closed is False
        assert geocoder_service_closed is False
    assert cd_api_service_closed is True
    assert geocoder_service_closed is True


def test_lifespan_connects_and_closes_users_service(monkeypatch):
    # Fully replaces connect()/aclose() rather than calling through to the
    # real implementation (unlike the spies above, which safely call
    # through to httpx-backed aclose()) -- the real connect() opens an
    # actual asyncpg pool against Postgres, which this test has no
    # business doing, keeping this file's "no real network/DB calls"
    # convention intact.
    calls = []

    async def fake_connect():
        calls.append("connect")

    async def fake_aclose():
        calls.append("aclose")

    monkeypatch.setattr(users_service, "connect", fake_connect)
    monkeypatch.setattr(users_service, "aclose", fake_aclose)

    with TestClient(app):
        assert calls == ["connect"]
    assert calls == ["connect", "aclose"]


def test_graphql_request_without_authorization_header_still_succeeds(client):
    response = client.post("/graphql", json={"query": "{ version }"})
    assert response.status_code == 200
    assert response.json() == {"data": {"version": "dev"}}


def test_graphql_request_with_authorization_header_still_succeeds(client, monkeypatch):
    # Spies on upsert_user_from_authorization_header rather than sending a
    # real/fake JWT through it -- UsersService's own token-verification
    # behavior (valid, invalid, missing config, etc.) is covered by
    # tests/services/test_users_service.py; this just confirms app.py's
    # context_getter actually wires the header through, and that an
    # existing resolver keeps working regardless of what that call does.
    received_headers = []

    async def fake_upsert(header):
        received_headers.append(header)

    monkeypatch.setattr(
        users_service, "upsert_user_from_authorization_header", fake_upsert
    )

    response = client.post(
        "/graphql",
        json={"query": "{ version }"},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )

    assert response.status_code == 200
    assert response.json() == {"data": {"version": "dev"}}
    assert received_headers == ["Bearer not-a-real-jwt"]


def test_graphql_request_with_invalid_token_is_rejected_with_401(client, monkeypatch):
    # Same spying rationale as the test above -- token-verification detail
    # is covered by tests/services/test_users_service.py; this only
    # confirms app.py's context_getter turns InvalidTokenError into an
    # HTTP 401 before Strawberry ever executes the query.
    async def fake_upsert(header):
        raise InvalidTokenError("bad signature")

    monkeypatch.setattr(
        users_service, "upsert_user_from_authorization_header", fake_upsert
    )

    response = client.post(
        "/graphql",
        json={"query": "{ version }"},
        headers={"Authorization": "Bearer not-a-real-jwt"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "bad signature"}


def test_version_query_returns_dev_when_no_version_file(client):
    response = client.post("/graphql", json={"query": "{ version }"})
    assert response.status_code == 200
    assert response.json() == {"data": {"version": "dev"}}


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_endpoint_returns_dev_when_no_version_file(client):
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"version": "dev"}


def test_cors_preflight_allows_configured_production_origin(client):
    response = client.options(
        "/graphql",
        headers={
            "Origin": "https://app.civicdog.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.civicdog.com"


def test_cors_preflight_allows_local_dev_origin(client):
    response = client.options(
        "/graphql",
        headers={
            "Origin": "http://localhost:5183",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5183"


def test_cors_preflight_rejects_unknown_origin(client):
    response = client.options(
        "/graphql",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight_allows_authorization_and_content_type_headers(client):
    response = client.options(
        "/graphql",
        headers={
            "Origin": "https://app.civicdog.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed
    assert "content-type" in allowed


def test_cors_does_not_allow_credentials(client):
    response = client.options(
        "/graphql",
        headers={
            "Origin": "https://app.civicdog.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-credentials" not in response.headers


def test_introspection_disabled_by_default(client):
    response = client.post("/graphql", json={"query": "{ __schema { queryType { name } } }"})
    assert response.status_code == 200
    assert response.json()["data"] is None
    assert "introspection has been disabled" in response.json()["errors"][0]["message"]


def test_senator_type_does_not_expose_role(client):
    # Query validation happens against the schema before any resolver
    # runs, so this fails the same way with or without a reachable
    # cd-api -- role isn't just omitted from the response, it's absent
    # from the schema entirely (Senator's role is always "Senator",
    # redundant with getSenators itself; Representative keeps it, since
    # that's cd-api's only way to distinguish an actual Representative
    # from a Delegate/Resident Commissioner).
    response = client.post(
        "/graphql", json={"query": '{ getSenators(state: "CA") { firstName role } }'}
    )
    assert response.status_code == 200
    assert response.json()["data"] is None
    assert "role" in response.json()["errors"][0]["message"]
    assert "Senator" in response.json()["errors"][0]["message"]


def test_senator_type_does_not_expose_district(client):
    # Same reasoning as role above -- a Senator's district is always
    # null (senators represent the whole state), so it's absent from
    # the schema entirely rather than a field that's always null at
    # runtime. Representative keeps it, since that's the whole point.
    response = client.post(
        "/graphql", json={"query": '{ getSenators(state: "CA") { firstName district } }'}
    )
    assert response.status_code == 200
    assert response.json()["data"] is None
    assert "district" in response.json()["errors"][0]["message"]
    assert "Senator" in response.json()["errors"][0]["message"]


def test_representative_and_senator_types_expose_bioguide_id():
    # Checked against the schema's own SDL directly rather than over
    # HTTP -- introspection is disabled by default (see
    # test_introspection_disabled_by_default above), and a query that
    # actually selects bioguideId would reach the resolver and attempt a
    # real cd-api call this test has no business making.
    sdl = schema.as_str()
    representative_type = sdl.split("type Representative {")[1].split("}")[0]
    senator_type = sdl.split("type Senator {")[1].split("}")[0]
    assert "bioguideId: String!" in representative_type
    assert "bioguideId: String!" in senator_type


def test_get_states_returns_all_states(client):
    response = client.post(
        "/graphql",
        json={"query": "{ getStates { abbr name seats votingSeats } }"},
    )
    assert response.status_code == 200
    states = response.json()["data"]["getStates"]
    assert len(states) == 56
    assert {
        "abbr": "CA",
        "name": "California",
        "seats": 52,
        "votingSeats": True,
    } in states
    assert {
        "abbr": "DC",
        "name": "District of Columbia",
        "seats": 1,
        "votingSeats": False,
    } in states


def test_get_district_returns_state_and_district(client, monkeypatch):
    payload = {
        "result": {
            "addressMatches": [
                {
                    "addressComponents": {"state": "CA"},
                    "geographies": {"119th Congressional Districts": [{"CD119": "11"}]},
                }
            ]
        }
    }

    async def fake_get(self, url, params=None, timeout=None):
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    response = client.post(
        "/graphql",
        json={"query": '{ getDistrict(address: "1 Dr Carlton B Goodlett Pl") { state district } }'},
    )
    assert response.status_code == 200
    assert response.json()["data"] == {"getDistrict": {"state": "CA", "district": 11}}


def test_get_district_returns_at_large_zero_for_a_delegate_jurisdiction(client, monkeypatch):
    # Census returns CD119 "98" for DC (FIPS nonvoting-delegate code);
    # getDistrict must hand back 0 so a getDistrict -> getRepresentatives
    # chain resolves (cd-platform#72).
    payload = {
        "result": {
            "addressMatches": [
                {
                    "addressComponents": {"state": "DC"},
                    "geographies": {"119th Congressional Districts": [{"CD119": "98"}]},
                }
            ]
        }
    }

    async def fake_get(self, url, params=None, timeout=None):
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    response = client.post(
        "/graphql",
        json={"query": '{ getDistrict(address: "1600 Pennsylvania Ave NW, Washington, DC") { state district } }'},
    )
    assert response.status_code == 200
    assert response.json()["data"] == {"getDistrict": {"state": "DC", "district": 0}}


def test_get_district_surfaces_no_match_error(client, monkeypatch):
    async def fake_get(self, url, params=None, timeout=None):
        return httpx.Response(
            200,
            json={"result": {"addressMatches": []}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    response = client.post(
        "/graphql", json={"query": '{ getDistrict(address: "nonsense") { state district } }'}
    )
    assert response.status_code == 200
    assert response.json()["data"] is None
    assert "No address match found" in response.json()["errors"][0]["message"]


# --- getMember (cd-api GET /members/{id}, cd-webapp's detail page) ---

_MEMBER_DETAIL_DOC = {
    "data": {
        "type": "member",
        "id": "K000401",
        "attributes": {
            "first_name": "Kevin",
            "middle_name": None,
            "last_name": "Kiley",
            "nickname": None,
            "suffix": None,
            "role": "Representative",
            "party": "Republican",
            "phone": "202-555-0100",
            "website": "https://kiley.example.gov",
            "photo_url": "https://example.gov/K000401.jpg",
            "district": 3,
            "state": "CA",
            "in_office": True,
        },
    }
}


def _stub_member_detail(monkeypatch, doc=None, *, raises=None):
    from cd.lib.jsonapi import Document
    from cd.lib.models import MemberDetail

    async def fake(bioguide_id):
        if raises is not None:
            raise raises
        return Document[MemberDetail].model_validate(doc)

    monkeypatch.setattr(cd_api_service, "member_detail", fake)


def test_get_member_returns_detail_with_state_and_in_office(client, monkeypatch):
    _stub_member_detail(monkeypatch, _MEMBER_DETAIL_DOC)

    response = client.post(
        "/graphql",
        json={
            "query": (
                '{ getMember(bioguideId: "K000401") '
                "{ bioguideId firstName lastName role district state inOffice } }"
            )
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["getMember"] == {
        "bioguideId": "K000401",
        "firstName": "Kevin",
        "lastName": "Kiley",
        "role": "Representative",
        "district": 3,
        "state": "CA",
        "inOffice": True,
    }


def test_get_member_serves_a_departed_member_with_in_office_false(client, monkeypatch):
    doc = {"data": {**_MEMBER_DETAIL_DOC["data"]}}
    doc["data"]["attributes"] = {**_MEMBER_DETAIL_DOC["data"]["attributes"], "in_office": False}
    _stub_member_detail(monkeypatch, doc)

    response = client.post(
        "/graphql", json={"query": '{ getMember(bioguideId: "K000401") { inOffice } }'}
    )
    assert response.json()["data"]["getMember"] == {"inOffice": False}


def test_get_member_surfaces_cd_api_404_as_a_graphql_error(client, monkeypatch):
    from cd.server.services.cd_api_service import ApiClientError

    _stub_member_detail(monkeypatch, raises=ApiClientError(404, "no current-Congress member"))

    response = client.post(
        "/graphql", json={"query": '{ getMember(bioguideId: "X000000") { bioguideId } }'}
    )
    assert response.status_code == 200
    assert response.json()["data"] is None
    assert "404" in response.json()["errors"][0]["message"]
