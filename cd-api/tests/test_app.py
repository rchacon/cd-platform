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


def _insert_term(
    pg_conn,
    bioguide_id: str,
    chamber: str,
    district: int | None,
    member_type: str | None = None,
    state: str = STATE,
) -> None:
    if member_type is None:
        member_type = "Senator" if chamber == "SENATE" else "Representative"
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO member_terms (
                bioguide_id, congress, chamber, member_type, state, district,
                start_year, source_hash
            ) VALUES (%s, 119, %s, %s, %s, %s, 2023, %s)
            """,
            (bioguide_id, chamber, member_type, state, district, f"hash-term-{bioguide_id}"),
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


def test_get_members_returns_member_type_as_role_for_delegate(pg_conn):
    # Regression test: the seeded_state fixture always sets member_type to
    # exactly what the old chamber-only role derivation would have produced
    # anyway ("Representative" for HOUSE), so it can't distinguish that bug
    # from the fix. This seeds a HOUSE row with a member_type that actually
    # differs from "Representative" to prove role comes from member_type.
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id, "Eleanor", "Norton")
    _insert_term(pg_conn, bioguide_id, "HOUSE", 0, member_type="Delegate")
    pg_conn.commit()

    try:
        client = TestClient(app)
        response = client.get("/members", params={"state": STATE, "district": 0})

        assert response.status_code == 200
        body = response.json()
        assert body["representatives"][0]["role"] == "Delegate"
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


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


def test_unmatched_route_returns_problem_detail():
    # Regression test: the exception handler used to be registered on
    # fastapi.HTTPException, a subclass, which Starlette's own router
    # never raises for its own 404s/405s -- only app-raised exceptions
    # hit the handler. Registering on the Starlette base class instead
    # covers framework-raised errors too.
    client = TestClient(app)
    response = client.get("/nonexistent")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == 404


def test_disallowed_method_returns_problem_detail():
    client = TestClient(app)
    response = client.post("/members")

    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == 405


def test_get_members_invalid_state_returns_422_problem_detail():
    client = TestClient(app)
    response = client.get("/members", params={"state": "California", "district": 1})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == 422
    assert "errors" in body


def test_get_members_unhandled_exception_returns_500_problem_detail(monkeypatch, caplog):
    def _boom(state, district):
        raise RuntimeError("db exploded")

    monkeypatch.setattr("app.fetch_current_members", _boom)
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level("ERROR"):
        response = client.get("/members", params={"state": "ZZ", "district": 1})

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Internal Server Error"
    assert body["status"] == 500

    # The client only ever sees the generic message above -- confirm the
    # real exception is still captured server-side (logs are the only
    # trace of what actually failed, since nothing else logs it).
    assert "Unhandled exception" in caplog.text
    assert "db exploded" in caplog.text


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"state": STATE, "district": 99}, id="non-matching-district"),
        pytest.param({"state": STATE}, id="omitted-district"),
    ],
)
def test_get_members_returns_senators_only_without_a_matching_district(seeded_state, params):
    client = TestClient(app)
    response = client.get("/members", params=params)

    assert response.status_code == 200
    body = response.json()
    assert body["representatives"] == []
    assert len(body["senators"]) == 2


def test_get_members_out_of_range_district_returns_404():
    # cd-platform#12: GA has 14 districts (see apportionment.SEATS_PER_STATE)
    # -- 99 is out of range regardless of what's seeded, so this needs no
    # fixture at all; the check happens before any DB query.
    client = TestClient(app)
    response = client.get("/members", params={"state": "GA", "district": 99})

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 404
    assert "District 99 does not exist for state GA" in body["detail"]
    assert "14 districts" in body["detail"]


def test_get_members_district_one_invalid_for_at_large_state_returns_404():
    # cd-platform#12: WY has exactly 1 seat, which uses district=0 (at-large)
    # per this project's own NULL/0/1+ convention -- district=1 sounds like
    # a reasonable request but is actually invalid for a 1-seat state, not
    # "the first of 1 districts."
    client = TestClient(app)
    response = client.get("/members", params={"state": "WY", "district": 1})

    assert response.status_code == 404
    body = response.json()
    assert "District 1 does not exist for state WY" in body["detail"]
    assert "1 district)" in body["detail"]


def test_get_members_valid_but_vacant_district_still_returns_200(pg_conn):
    # cd-platform#12's actual regression concern: a real, in-range district
    # with no current representative (a genuine vacancy) must NOT be
    # confused with an out-of-range one -- still 200 + empty
    # representatives, only senators returned. Uses GA (14 districts);
    # district 5 is in-range but nothing is seeded for it below.
    senator_a, senator_b = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(2))
    _insert_member(pg_conn, senator_a, "Dana", "Diaz")
    _insert_member(pg_conn, senator_b, "Eli", "Evans")
    _insert_term(pg_conn, senator_a, "SENATE", None, state="GA")
    _insert_term(pg_conn, senator_b, "SENATE", None, state="GA")
    pg_conn.commit()

    try:
        client = TestClient(app)
        response = client.get("/members", params={"state": "GA", "district": 5})

        assert response.status_code == 200
        body = response.json()
        assert body["representatives"] == []
        assert len(body["senators"]) == 2
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM members WHERE bioguide_id = ANY(%s)",
                ([senator_a, senator_b],),
            )
        pg_conn.commit()
