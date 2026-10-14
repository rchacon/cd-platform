import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cd.server.origin_guard import OriginGuard


def _client(secret: str, environment: str) -> TestClient:
    inner = FastAPI()

    @inner.get("/thing")
    def thing() -> dict:
        return {"ok": True}

    outer = FastAPI()
    outer.mount("/v1", OriginGuard(inner, secret, environment))
    return TestClient(outer)


def test_local_dev_without_a_secret_lets_v1_through():
    # make start-server has no API Gateway in front of it at all.
    assert _client("", "local").get("/v1/thing").status_code == 200


def test_non_local_without_a_secret_fails_closed():
    response = _client("", "production").get(
        "/v1/thing", headers={"X-Origin-Verify": ""}
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")


def test_correct_origin_header_is_let_through():
    response = _client("s3cret", "production").get(
        "/v1/thing", headers={"X-Origin-Verify": "s3cret"}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("headers", [{}, {"X-Origin-Verify": "wrong"}])
def test_missing_or_wrong_origin_header_is_rejected(headers):
    # The direct-to-server.<domain> case: no API Gateway, so no header.
    response = _client("s3cret", "production").get("/v1/thing", headers=headers)

    assert response.status_code == 403


def test_a_configured_secret_is_enforced_in_local_dev_too():
    response = _client("s3cret", "local").get("/v1/thing")

    assert response.status_code == 403


def test_a_rejected_jsonapi_path_gets_a_jsonapi_error_document():
    # /members and /bills speak JSON:API, so their 403 must too -- see
    # cd.api.jsonapi.is_jsonapi_path.
    response = _client("s3cret", "production").get("/v1/members")

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert response.json() == {
        "errors": [{"status": "403", "title": "Forbidden", "detail": "Forbidden."}]
    }


def test_a_rejected_mount_prefix_lookalike_stays_problem_json():
    # /v1/membersX isn't under /members -- same boundary check routing uses.
    response = _client("s3cret", "production").get("/v1/membersX")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
