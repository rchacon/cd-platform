import httpx
import pytest
from fastapi.testclient import TestClient

from cd.server import geocoder
from cd.server.app import app
from cd.server.schema import api_client


@pytest.fixture
def client():
    # Starlette's TestClient only triggers ASGI lifespan startup/shutdown
    # (app.py's lifespan(), which closes api_client on exit) when used as
    # a context manager -- a bare TestClient(app) never runs it at all.
    with TestClient(app) as client:
        yield client


def test_lifespan_closes_both_clients_on_shutdown():
    # One test, not two -- api_client/geocoder's own connection pools are
    # module-level singletons shared across the whole test session, and
    # aclose() is a one-way transition (idempotent, but not reversible).
    # Splitting this into separate tests would make the second one
    # order-dependent on whether some earlier test's own
    # `with TestClient(app):` already triggered this same shutdown.
    assert api_client._client.is_closed is False
    assert geocoder._client.is_closed is False
    with TestClient(app):
        assert api_client._client.is_closed is False
        assert geocoder._client.is_closed is False
    assert api_client._client.is_closed is True
    assert geocoder._client.is_closed is True


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
    response = client.post("/graphql", json={"query": "{ getStates { abbreviation name } }"})
    assert response.status_code == 200
    states = response.json()["data"]["getStates"]
    assert len(states) == 56
    assert {"abbreviation": "CA", "name": "California"} in states


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
