from fastapi.testclient import TestClient

from cd.server.app import app

client = TestClient(app)


def test_version_query_returns_dev_when_no_version_file():
    response = client.post("/graphql", json={"query": "{ version }"})
    assert response.status_code == 200
    assert response.json() == {"data": {"version": "dev"}}


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_endpoint_returns_dev_when_no_version_file():
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"version": "dev"}


def test_introspection_disabled_by_default():
    response = client.post("/graphql", json={"query": "{ __schema { queryType { name } } }"})
    assert response.status_code == 200
    assert response.json()["data"] is None
    assert "introspection has been disabled" in response.json()["errors"][0]["message"]


def test_senator_type_does_not_expose_role():
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
