import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

from app import app

STATE = "ZZ"
DISTRICT = 1


def _insert_member(pg_conn, bioguide_id: str, given_name: str, family_name: str) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO members (
                bioguide_id, given_name, family_name, phone, website_url,
                photo_uri, party_history, source_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                bioguide_id, given_name, family_name, "202-555-0100",
                f"https://{family_name.lower()}.example.gov",
                f"https://example.gov/{bioguide_id}.jpg",
                Json([{
                    "party": "DEMOCRATIC", "source_party_name": "Democrat",
                    "start_year": 2023, "end_year": None,
                }]),
                f"hash-{bioguide_id}",
            ),
        )


def _insert_term(pg_conn, bioguide_id: str, chamber: str, district: int | None) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO member_terms (
                bioguide_id, congress, chamber, member_type, state, district,
                start_year, source_hash
            ) VALUES (%s, 119, %s, %s, %s, %s, 2023, %s)
            """,
            (
                bioguide_id, chamber,
                "Senator" if chamber == "SENATE" else "Representative",
                STATE, district,
                f"hash-term-{bioguide_id}",
            ),
        )


@pytest.fixture
def seeded_state(pg_conn):
    senator_a, senator_b, rep = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(3))

    _insert_member(pg_conn, senator_a, "Alice", "Anderson")
    _insert_member(pg_conn, senator_b, "Bob", "Baker")
    _insert_member(pg_conn, rep, "Carol", "Clark")
    _insert_term(pg_conn, senator_a, "SENATE", None)
    _insert_term(pg_conn, senator_b, "SENATE", None)
    _insert_term(pg_conn, rep, "HOUSE", DISTRICT)
    pg_conn.commit()

    yield

    # member_terms rows cascade-delete via members' ON DELETE CASCADE.
    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM members WHERE bioguide_id = ANY(%s)",
            ([senator_a, senator_b, rep],),
        )
    pg_conn.commit()


def test_get_members_returns_senators_and_representative(seeded_state):
    client = TestClient(app)
    response = client.get("/members", params={"state": STATE, "district": DISTRICT})

    assert response.status_code == 200
    body = response.json()
    assert {p["full_name"] for p in body["senators"]} == {"Alice Anderson", "Bob Baker"}
    assert [p["full_name"] for p in body["representatives"]] == ["Carol Clark"]
    assert body["senators"][0]["role"] == "Senator"
    assert body["representatives"][0]["role"] == "Representative"


def test_get_members_unknown_state_returns_404(pg_conn):
    client = TestClient(app)
    response = client.get("/members", params={"state": "QQ", "district": 1})

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "No data found for state QQ"


def test_get_members_invalid_state_returns_422_problem_detail():
    client = TestClient(app)
    response = client.get("/members", params={"state": "California", "district": 1})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == 422
    assert "errors" in body


def test_get_members_unhandled_exception_returns_500_problem_detail(monkeypatch):
    def _boom(state, district):
        raise RuntimeError("db exploded")

    monkeypatch.setattr("app.fetch_current_members", _boom)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/members", params={"state": "ZZ", "district": 1})

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Internal Server Error"
    assert body["status"] == 500


def test_get_members_bad_district_returns_empty_representatives(seeded_state):
    client = TestClient(app)
    response = client.get("/members", params={"state": STATE, "district": 99})

    assert response.status_code == 200
    body = response.json()
    assert body["representatives"] == []
    assert len(body["senators"]) == 2


def test_get_members_omitted_district_returns_senators_only(seeded_state):
    client = TestClient(app)
    response = client.get("/members", params={"state": STATE})

    assert response.status_code == 200
    body = response.json()
    assert body["representatives"] == []
    assert len(body["senators"]) == 2
