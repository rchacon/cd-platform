from fastapi import FastAPI
from fastapi.testclient import TestClient

from cd.api.app import app


def _mounted() -> TestClient:
    # The way cd-server serves it (cd/server/app.py), minus OriginGuard.
    outer = FastAPI()
    outer.mount("/v1", app)
    return TestClient(outer, follow_redirects=False)


def test_trailing_slash_is_served_like_the_slashless_route():
    client = TestClient(app, follow_redirects=False)
    response = client.get("/version/")
    assert response.status_code == 200
    assert response.json() == client.get("/version").json()


def test_trailing_slash_under_the_v1_mount_is_served_not_redirected():
    # A 307 here would point at cd-server's own host (cd-infra#79 review),
    # which 403s callers that came through API Gateway.
    client = _mounted()
    response = client.get("/v1/version/")
    assert response.status_code == 200
    assert response.json() == client.get("/v1/version").json()


def test_repeated_trailing_slashes_are_stripped_too():
    assert _mounted().get("/v1/version//").status_code == 200


def test_mount_root_is_left_alone():
    # /v1/ has no slashless route; it stays cd.api's own 404, not a strip
    # down to the mount prefix itself.
    response = _mounted().get("/v1/")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_unknown_path_with_trailing_slash_is_still_a_404():
    response = _mounted().get("/v1/nope/")
    assert response.status_code == 404
