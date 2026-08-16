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
