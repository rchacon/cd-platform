import json
import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

from cd.api.app import app, handler

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


def test_openapi_json_has_expected_title_and_version():
    # app.version is read from VERSION_FILE once at import time (cd-website#1:
    # the exported spec needs a real title/version, not FastAPI's placeholder
    # "FastAPI"/"0.1.0") -- no VERSION file exists in tests, so it's "dev".
    client = TestClient(app)
    response = client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "cd-api"
    assert schema["info"]["version"] == "dev"


def test_openapi_json_description_documents_auth_and_error_contract():
    # cd-platform#46: this used to live only as hand-written prose in
    # cd-website's api.astro, disconnected from the code it describes --
    # pins that the OpenAPI spec itself now carries it.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    description = schema["info"]["description"]
    assert "X-Api-Key" in description
    assert "RFC 9457" in description
    assert "application/problem+json" in description


def test_openapi_json_documents_production_server_url():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    assert schema["servers"] == [
        {"url": "https://api.civicdog.com/v1", "description": "Production"}
    ]


def test_openapi_json_documents_api_key_security_scheme():
    # X-Api-Key is enforced by API Gateway, not a FastAPI Security(...)
    # dependency -- pins that _custom_openapi() still documents it by
    # hand, since nothing derives it automatically from the routes.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    scheme = schema["components"]["securitySchemes"]["ApiKeyAuth"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "X-Api-Key"
    assert schema["security"] == [{"ApiKeyAuth": []}]


def test_openapi_members_route_documents_404_vs_vacancy_distinction():
    # cd-platform#46: this behavior used to only be explained in
    # cd-website's prose and an internal code comment -- pins that the
    # route's own OpenAPI description now covers it too.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    description = schema["paths"]["/members"]["get"]["description"]
    assert "vacant" in description.lower()


def test_openapi_members_response_documents_person_fields():
    # cd-platform#40: /members' 200 response used to be an undocumented
    # {"type": "object", "additionalProperties": true} placeholder --
    # response_model=MembersResponse should make the real shape show up.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    schemas = schema["components"]["schemas"]
    assert "MembersResponse" in schemas
    assert "Person" in schemas
    assert schemas["Person"]["required"] == ["role"]
    assert set(schemas["Person"]["properties"]) == {
        "first_name", "middle_name", "last_name", "nickname", "suffix",
        "role", "party", "phone", "website", "photo_url",
    }

    # Regression test: role's description used to claim "Resident
    # Commissioner" applies to any DC/territory seat, but member_type only
    # ever uses it for Puerto Rico -- DC and other territories use
    # "Delegate" (see test_transform.py's role tests).
    role_description = schemas["Person"]["properties"]["role"]["description"]
    assert "Puerto Rico" in role_description


def test_openapi_error_responses_use_problem_json_content_type():
    # cd-platform#40: the app always returns application/problem+json for
    # errors (see problem.py), but FastAPI's default-generated 422 used to
    # document application/json + its own HTTPValidationError shape instead.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    responses = schema["paths"]["/members"]["get"]["responses"]
    for status in ("404", "422", "500"):
        content = responses[status]["content"]
        assert list(content) == ["application/problem+json"]

    schemas = schema["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_openapi_error_schemas_are_shared_ref_components():
    # Regression test: ProblemDetail/ValidationProblemDetail used to be
    # inlined in full at every use (404/422/500 on /members, 500 on
    # /version) instead of being registered once under components.schemas
    # and referenced by $ref, unlike MembersResponse/Person/VersionResponse.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    schemas = schema["components"]["schemas"]
    assert "ProblemDetail" in schemas
    assert "ValidationProblemDetail" in schemas

    responses = schema["paths"]["/members"]["get"]["responses"]
    assert responses["404"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/ProblemDetail"
    }
    assert responses["422"]["content"]["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/ValidationProblemDetail"
    }


def test_openapi_documents_405_for_disallowed_method():
    # Regression test: http_exception_handler genuinely returns a
    # problem+json 405 for a disallowed method (see
    # test_disallowed_method_returns_problem_detail), but it wasn't
    # documented in responses= for either route.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    assert "405" in schema["paths"]["/members"]["get"]["responses"]
    assert "405" in schema["paths"]["/version"]["get"]["responses"]


def test_openapi_district_parameter_documents_semantics():
    # cd-platform#40: district's omitted/0/1+ meaning isn't derivable from
    # its bare `int | None, ge=0` schema alone.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    parameters = schema["paths"]["/members"]["get"]["parameters"]
    district_param = next(p for p in parameters if p["name"] == "district")

    assert "omit" in district_param["description"].lower()
    assert "at-large" in district_param["description"].lower()


def test_get_version_returns_dev_when_version_file_absent(monkeypatch, tmp_path):
    # cd-platform#29: local dev/CI never has a VERSION file -- only the
    # deploy workflow writes one into the Lambda zip -- so this is the
    # default a developer actually sees.
    monkeypatch.setattr("cd.api.app.VERSION_FILE", tmp_path / "VERSION")
    client = TestClient(app)
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"version": "dev"}


def test_get_version_returns_file_contents_when_present(monkeypatch, tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.1.0\n")
    monkeypatch.setattr("cd.api.app.VERSION_FILE", version_file)
    client = TestClient(app)
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"version": "0.1.0"}


def _api_gateway_event(path: str) -> dict:
    return {
        "resource": "/{proxy+}",
        "path": path,
        "httpMethod": "GET",
        "headers": {},
        "multiValueQueryStringParameters": {},
        "requestContext": {"identity": {"sourceIp": "127.0.0.1"}},
        "body": None,
        "isBase64Encoded": False,
    }


def test_handler_strips_v1_base_path(monkeypatch, tmp_path):
    # Regression test for cd-infra#19: api.civicdog.com's custom-domain
    # base_path_mapping ("v1") is used by API Gateway to select which
    # API/stage a request routes to, but is NOT stripped from the path
    # forwarded to the Lambda -- confirmed empirically against the real
    # domain, which 404'd before api_gateway_base_path was added to the
    # Mangum() call below. A TestClient-based test against `app` directly
    # (like the two above) would never catch this class of bug, since it
    # never goes through Mangum's event handling at all.
    monkeypatch.setattr("cd.api.app.VERSION_FILE", tmp_path / "VERSION")
    response = handler(_api_gateway_event("/v1/version"), None)

    assert response["statusCode"] == 200
    assert response["body"] == '{"version":"dev"}'


def test_handler_leaves_unprefixed_path_unchanged(monkeypatch, tmp_path):
    # The existing execute-api URL never had a /v1 segment (its stage
    # segment is excluded from event["path"] entirely by API Gateway
    # itself) -- api_gateway_base_path must not affect that request shape.
    monkeypatch.setattr("cd.api.app.VERSION_FILE", tmp_path / "VERSION")
    response = handler(_api_gateway_event("/version"), None)

    assert response["statusCode"] == 200
    assert response["body"] == '{"version":"dev"}'


def test_handler_returns_decoded_problem_json_not_base64(monkeypatch, tmp_path):
    # Regression test for cd-platform#38: Mangum's default text_mime_types
    # doesn't include application/problem+json, so every error response
    # was base64-encoded with isBase64Encoded=true, and API Gateway (no
    # matching binaryMediaTypes entry) forwarded the raw base64 string to
    # clients untouched instead of decoding it.
    monkeypatch.setattr("cd.api.app.VERSION_FILE", tmp_path / "VERSION")
    response = handler(_api_gateway_event("/v1/members"), None)  # no `state` -> 422

    assert response["statusCode"] == 422
    assert response["isBase64Encoded"] is False
    body = json.loads(response["body"])
    assert body["type"] == "about:blank"
    assert body["status"] == 422


def test_get_members_returns_senators_and_representative(seeded_state):
    client = TestClient(app)
    response = client.get("/members", params={"state": STATE, "district": DISTRICT})

    assert response.status_code == 200
    body = response.json()
    assert {(p["first_name"], p["last_name"]) for p in body["senators"]} == {
        ("Alice", "Anderson"),
        ("Bob", "Baker"),
    }
    assert [(p["first_name"], p["last_name"]) for p in body["representatives"]] == [
        ("Carol", "Clark")
    ]
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

    monkeypatch.setattr("cd.api.app.fetch_current_members", _boom)
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
