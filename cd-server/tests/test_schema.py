import httpx
import pytest
from fastapi.testclient import TestClient

from cd.server.app import app
from cd.server.schema import cd_api_service, geocoder_service


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
