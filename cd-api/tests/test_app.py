import datetime
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

from cd.lib import bedrock
from cd.api import db
from cd.api.app import app, handler
from conftest import random_number

STATE = "ZZ"
DISTRICT = 1
CONGRESS = 119

# A year the view's `end_year >= EXTRACT(YEAR FROM CURRENT_DATE)` test
# treats as "already left" -- derived, not hard-coded, so these tests
# don't silently pin a wall-clock year.
LAST_YEAR = datetime.date.today().year - 1

JSONAPI_MEDIA_TYPE = "application/vnd.api+json"


def _follow_ref(schema: dict, ref: str) -> dict:
    # "#/components/schemas/Foo" -> schema["components"]["schemas"]["Foo"]
    node = schema
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


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
    end_year: int | None = None,
) -> None:
    if member_type is None:
        member_type = "Senator" if chamber == "SENATE" else "Representative"
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO member_terms (
                bioguide_id, congress, chamber, member_type, state, district,
                start_year, end_year, source_hash
            ) VALUES (%s, 119, %s, %s, %s, %s, 2023, %s, %s)
            """,
            (
                bioguide_id, chamber, member_type, state, district, end_year,
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

    yield {"senator_a": senator_a, "senator_b": senator_b, "rep": rep}

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
    # dependency -- pins that build_openapi() still documents it by
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


def test_openapi_members_list_route_is_a_jsonapi_collection():
    # cd-platform#104 PR B: GET /members went from a bespoke
    # {senators, representatives} body to a JSON:API
    # CollectionDocument[MemberDetail] -- the same `member` resource
    # shape the by-id route serves.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    get = schema["paths"]["/members"]["get"]
    responses = get["responses"]
    for status in ("400", "404", "405", "406", "415", "422", "500"):
        assert list(responses[status]["content"]) == [JSONAPI_MEDIA_TYPE]
        ref = responses[status]["content"][JSONAPI_MEDIA_TYPE]["schema"]["$ref"]
        assert ref.endswith("/JsonApiErrorDocument")

    document = _follow_ref(
        schema, responses["200"]["content"][JSONAPI_MEDIA_TYPE]["schema"]["$ref"]
    )
    assert set(document["properties"]) == {"data", "meta"}
    resource = _follow_ref(schema, document["properties"]["data"]["items"]["$ref"])
    assert set(resource["properties"]) == {
        "type", "id", "attributes", "relationships", "meta"
    }
    detail = _follow_ref(schema, resource["properties"]["attributes"]["$ref"])
    assert {"state", "in_office"} <= set(detail["required"])
    assert "bioguide_id" not in detail["properties"]
    assert "Puerto Rico" in detail["properties"]["role"]["description"]

    # No leftover FastAPI-default validation schema.
    schemas = schema["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_openapi_members_list_documents_filter_parameters():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    parameters = {
        p["name"]: p for p in schema["paths"]["/members"]["get"]["parameters"]
    }
    assert set(parameters) == {
        "filter[state]", "filter[chamber]", "filter[district]", "state", "district"
    }
    # The bare aliases cd-server still sends during the migration are
    # marked deprecated.
    assert parameters["state"]["deprecated"] is True
    assert parameters["district"]["deprecated"] is True
    assert parameters["filter[state]"].get("deprecated") is not True

    district_desc = parameters["filter[district]"]["description"].lower()
    assert "at-large" in district_desc
    assert "senator" in district_desc


def test_openapi_version_errors_use_problem_json_shared_refs():
    # /version is the one remaining bespoke (RFC 9457 problem+json)
    # endpoint -- its error responses still $ref the shared ProblemDetail
    # component rather than inlining it.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    schemas = schema["components"]["schemas"]
    assert "ProblemDetail" in schemas

    responses = schema["paths"]["/version"]["get"]["responses"]
    for status in ("405", "500"):
        assert list(responses[status]["content"]) == ["application/problem+json"]
        assert responses[status]["content"]["application/problem+json"]["schema"] == {
            "$ref": "#/components/schemas/ProblemDetail"
        }


def test_openapi_documents_405_for_disallowed_method():
    # Regression test: the 405 for a disallowed method wasn't documented
    # in responses= for either route.
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    assert "405" in schema["paths"]["/members"]["get"]["responses"]
    assert "405" in schema["paths"]["/version"]["get"]["responses"]


def test_get_version_returns_dev_when_version_file_absent(monkeypatch, tmp_path):
    # cd-platform#29: local dev/CI never has a VERSION file -- only the
    # deploy workflow writes one into the Lambda zip -- so this is the
    # default a developer actually sees.
    monkeypatch.setattr("cd.api.routes.version.VERSION_FILE", tmp_path / "VERSION")
    client = TestClient(app)
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"version": "dev"}


def test_get_version_returns_file_contents_when_present(monkeypatch, tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.1.0\n")
    monkeypatch.setattr("cd.api.routes.version.VERSION_FILE", version_file)
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
    monkeypatch.setattr("cd.api.routes.version.VERSION_FILE", tmp_path / "VERSION")
    response = handler(_api_gateway_event("/v1/version"), None)

    assert response["statusCode"] == 200
    assert response["body"] == '{"version":"dev"}'


def test_handler_leaves_unprefixed_path_unchanged(monkeypatch, tmp_path):
    # The existing execute-api URL never had a /v1 segment (its stage
    # segment is excluded from event["path"] entirely by API Gateway
    # itself) -- api_gateway_base_path must not affect that request shape.
    monkeypatch.setattr("cd.api.routes.version.VERSION_FILE", tmp_path / "VERSION")
    response = handler(_api_gateway_event("/version"), None)

    assert response["statusCode"] == 200
    assert response["body"] == '{"version":"dev"}'


def test_handler_returns_decoded_problem_json_not_base64(monkeypatch, tmp_path):
    # Regression test for cd-platform#38: Mangum's default text_mime_types
    # doesn't include application/problem+json, so every error response
    # was base64-encoded with isBase64Encoded=true, and API Gateway (no
    # matching binaryMediaTypes entry) forwarded the raw base64 string to
    # clients untouched instead of decoding it. Exercised via /version,
    # the one endpoint still on problem+json (GET /members moved to
    # JSON:API in cd-platform#104 PR B -- see the vnd.api+json test below).
    monkeypatch.setattr("cd.api.routes.version.VERSION_FILE", tmp_path / "VERSION")
    event = _api_gateway_event("/v1/version")
    event["httpMethod"] = "POST"  # 405 -> problem+json
    response = handler(event, None)

    assert response["statusCode"] == 405
    assert response["isBase64Encoded"] is False
    body = json.loads(response["body"])
    assert body["type"] == "about:blank"
    assert body["status"] == 405


def test_handler_returns_decoded_vnd_api_json_not_base64(monkeypatch, tmp_path):
    # cd-platform#38 again, for the JSON:API media type: application/vnd.api+json
    # must also be in Mangum's text_mime_types or every JSON:API response
    # (success and error) comes back base64-encoded with isBase64Encoded=true.
    monkeypatch.setattr("cd.api.routes.version.VERSION_FILE", tmp_path / "VERSION")
    # no filter[bill] -> 422 from the JSON:API route
    response = handler(_api_gateway_event("/v1/members/K000001/votes"), None)

    assert response["statusCode"] == 422
    assert response["isBase64Encoded"] is False
    body = json.loads(response["body"])
    assert body["errors"][0]["status"] == "422"


def test_handler_jsonapi_path_405_is_a_jsonapi_error_document(monkeypatch, tmp_path):
    # A routing-layer 405 (wrong method) never reaches JsonApiRoute -- app.py
    # must still format it as JSON:API for a JSON:API path.
    monkeypatch.setattr("cd.api.routes.version.VERSION_FILE", tmp_path / "VERSION")
    event = _api_gateway_event("/v1/members/K000001/votes")
    event["httpMethod"] = "POST"
    response = handler(event, None)

    assert response["statusCode"] == 405
    assert "application/vnd.api+json" in response["headers"]["content-type"]
    body = json.loads(response["body"])
    assert body["errors"][0]["status"] == "405"


def test_jsonapi_namespace_near_miss_404_is_a_jsonapi_error_document():
    # A typo'd path under /members/<id>/... has no route -> Starlette 404.
    # app.py's _JSONAPI_PATH_RE covers the whole namespace, so it comes
    # back as a JSON:API error document, not problem+json.
    client = TestClient(app)
    response = client.get("/members/K000001/votez")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert response.json()["errors"][0]["status"] == "404"


def test_bills_405_is_a_jsonapi_error_document():
    # POST /bills never reaches JsonApiRoute (routing-layer 405) --
    # _JSONAPI_PATH_RE covers the /bills namespace so app.py formats it.
    client = TestClient(app)
    response = client.post("/bills")

    assert response.status_code == 405
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert response.json()["errors"][0]["status"] == "405"


def _members_by_id(body: dict) -> dict:
    return {r["id"]: r for r in body["data"]}


def test_get_members_returns_a_jsonapi_collection_of_members(seeded_state):
    client = TestClient(app)
    response = client.get("/members", params={"filter[state]": STATE})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.api+json"
    body = response.json()
    assert set(body) == {"data"}

    by_id = _members_by_id(body)
    assert set(by_id) == {
        seeded_state["senator_a"], seeded_state["senator_b"], seeded_state["rep"]
    }
    for resource in body["data"]:
        assert resource["type"] == "member"
        assert "bioguide_id" not in resource["attributes"]
        assert resource["attributes"]["state"] == STATE
        assert resource["attributes"]["in_office"] is True
        assert "relationships" not in resource

    # Senators first, then House by district (fetch_members ORDER BY).
    assert [r["attributes"]["role"] for r in body["data"]] == [
        "Senator", "Senator", "Representative"
    ]
    rep = by_id[seeded_state["rep"]]
    assert rep["attributes"]["last_name"] == "Clark"
    assert rep["attributes"]["district"] == DISTRICT


def test_get_members_filter_by_chamber(seeded_state):
    client = TestClient(app)

    senate = client.get(
        "/members", params={"filter[state]": STATE, "filter[chamber]": "senate"}
    ).json()
    assert {r["id"] for r in senate["data"]} == {
        seeded_state["senator_a"], seeded_state["senator_b"]
    }

    house = client.get(
        "/members", params={"filter[state]": STATE, "filter[chamber]": "HOUSE"}
    ).json()
    assert [r["id"] for r in house["data"]] == [seeded_state["rep"]]


def test_get_members_filter_by_district_does_not_bundle_senators(seeded_state):
    client = TestClient(app)
    response = client.get(
        "/members", params={"filter[state]": STATE, "filter[district]": DISTRICT}
    )

    assert response.status_code == 200
    assert [r["id"] for r in response.json()["data"]] == [seeded_state["rep"]]


def test_get_members_accepts_deprecated_bare_state_and_district_aliases(seeded_state):
    # cd-server's #127 dual-send keeps sending bare `state`/`district`
    # until PR C -- they must still bind, not 400 as unsupported params.
    client = TestClient(app)
    response = client.get(
        "/members", params={"state": STATE, "district": DISTRICT}
    )

    assert response.status_code == 200
    assert [r["id"] for r in response.json()["data"]] == [seeded_state["rep"]]


def test_get_members_dual_sent_filter_and_bare_params_do_not_conflict(seeded_state):
    # cd-server sends BOTH `state` and `filter[state]` (same for district)
    # during the migration window -- distinct keys, so JsonApiRoute's
    # repeated-param check doesn't fire, and the canonical one wins.
    client = TestClient(app)
    response = client.get(
        "/members",
        params={
            "state": STATE, "filter[state]": STATE,
            "district": DISTRICT, "filter[district]": DISTRICT,
        },
    )

    assert response.status_code == 200
    assert [r["id"] for r in response.json()["data"]] == [seeded_state["rep"]]


def test_get_members_role_comes_from_member_type_for_a_delegate(pg_conn):
    # A HOUSE row whose member_type differs from "Representative" -- proves
    # role passes member_type through, and that filter[chamber]=house
    # folds Delegates in.
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id, "Eleanor", "Norton")
    _insert_term(pg_conn, bioguide_id, "HOUSE", 0, member_type="Delegate")
    pg_conn.commit()

    try:
        client = TestClient(app)
        response = client.get(
            "/members", params={"filter[state]": STATE, "filter[chamber]": "house"}
        )

        assert response.status_code == 200
        by_id = _members_by_id(response.json())
        assert by_id[bioguide_id]["attributes"]["role"] == "Delegate"
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


def test_get_members_unknown_state_returns_jsonapi_404(pg_conn):
    client = TestClient(app)
    response = client.get("/members", params={"filter[state]": "QQ"})

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/vnd.api+json"
    error = response.json()["errors"][0]
    assert error["status"] == "404"
    assert error["detail"] == "No data found for state QQ"


def test_get_members_missing_state_returns_jsonapi_422():
    client = TestClient(app)
    response = client.get("/members")

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert "filter[state]" in response.json()["errors"][0]["detail"]


def test_get_members_bad_chamber_returns_jsonapi_422(seeded_state):
    client = TestClient(app)
    response = client.get(
        "/members", params={"filter[state]": STATE, "filter[chamber]": "upper"}
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert "filter[chamber]" in response.json()["errors"][0]["detail"]


def test_get_members_rejects_an_unsupported_query_param(seeded_state):
    client = TestClient(app)
    response = client.get(
        "/members", params={"filter[state]": STATE, "include": "terms"}
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert "include" in response.json()["errors"][0]["detail"]


def test_disallowed_method_on_members_is_a_jsonapi_error():
    # POST /members -- a routing-layer 405, never reaches JsonApiRoute;
    # _JSONAPI_PATH_RE covers the whole /members namespace now, so app.py
    # formats it as JSON:API, not problem+json.
    client = TestClient(app)
    response = client.post("/members")

    assert response.status_code == 405
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert response.json()["errors"][0]["status"] == "405"


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


def test_get_members_invalid_state_returns_jsonapi_422():
    client = TestClient(app)
    response = client.get("/members", params={"filter[state]": "California"})

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert response.json()["errors"][0]["status"] == "422"


def test_get_members_unhandled_exception_returns_jsonapi_500(monkeypatch, caplog):
    def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr("cd.api.routes.members.fetch_members", _boom)
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level("ERROR"):
        response = client.get("/members", params={"filter[state]": "ZZ"})

    assert response.status_code == 500
    # JsonApiRoute catches it -> JSON:API document, not problem+json.
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert response.json()["errors"][0]["status"] == "500"

    # The client only ever sees the generic message above -- confirm the
    # real exception is still captured server-side.
    assert "Unhandled exception" in caplog.text
    assert "db exploded" in caplog.text


def test_get_members_omitted_district_returns_the_whole_state(seeded_state):
    client = TestClient(app)
    response = client.get("/members", params={"filter[state]": STATE})

    assert response.status_code == 200
    assert len(response.json()["data"]) == 3  # 2 senators + 1 rep


def test_get_members_real_state_with_no_members_is_200_empty():
    # An "honest collection": a real (apportionment-table) state that
    # simply has nobody synced is an empty collection, not a 404. (A
    # state NOT in the table -- only synthetic test codes, never a real
    # 2-letter USPS code -- still 404s; see the unknown-state test.) MT
    # has 2 districts so it's a real state; nothing is seeded for it.
    client = TestClient(app)
    response = client.get("/members", params={"filter[state]": "MT"})

    assert response.status_code == 200
    assert response.json() == {"data": []}


def test_get_members_out_of_range_district_returns_jsonapi_404():
    # cd-platform#12: GA has 14 districts (see apportionment.SEATS_PER_STATE)
    # -- 99 is out of range regardless of what's seeded; the check happens
    # before any DB query.
    client = TestClient(app)
    response = client.get(
        "/members", params={"filter[state]": "GA", "filter[district]": 99}
    )

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/vnd.api+json"
    detail = response.json()["errors"][0]["detail"]
    assert "District 99 does not exist for state GA" in detail
    assert "14 districts" in detail


def test_get_members_district_one_invalid_for_at_large_state_returns_404():
    # cd-platform#12: WY has exactly 1 seat, which uses district=0
    # (at-large) -- district=1 is invalid for a 1-seat state.
    client = TestClient(app)
    response = client.get(
        "/members", params={"filter[state]": "WY", "filter[district]": 1}
    )

    assert response.status_code == 404
    detail = response.json()["errors"][0]["detail"]
    assert "District 1 does not exist for state WY" in detail
    assert "1 district)" in detail


def test_get_members_valid_but_vacant_district_returns_200_empty_data(pg_conn):
    # cd-platform#12: a real, in-range district with no current
    # representative (a genuine vacancy) must NOT be confused with an
    # out-of-range one -- 200 with `data: []`. Unlike the old shape, no
    # senators are bundled in (honest collection -- filter[district]
    # selects House members only). GA has 14 districts; nothing is seeded
    # for district 5.
    senator = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, senator, "Dana", "Diaz")
    _insert_term(pg_conn, senator, "SENATE", None, state="GA")
    pg_conn.commit()

    try:
        client = TestClient(app)
        response = client.get(
            "/members", params={"filter[state]": "GA", "filter[district]": 5}
        )

        assert response.status_code == 200
        assert response.json()["data"] == []
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (senator,))
        pg_conn.commit()


def test_get_members_excludes_a_representative_who_left_mid_term(pg_conn):
    # current_members (cd-etl 0007) keeps departed members, flagged
    # in_office=false -- this list must still exclude them.
    sen, rep = (f"TEST{uuid.uuid4().hex[:8].upper()}" for _ in range(2))
    _insert_member(pg_conn, sen, "Sam", "Stone")
    _insert_member(pg_conn, rep, "Rita", "Reyes")
    _insert_term(pg_conn, sen, "SENATE", None, state="GA")
    _insert_term(pg_conn, rep, "HOUSE", 5, state="GA", end_year=LAST_YEAR)
    pg_conn.commit()

    try:
        client = TestClient(app)
        by_state = client.get("/members", params={"filter[state]": "GA"}).json()
        assert sen in {r["id"] for r in by_state["data"]}
        assert rep not in {r["id"] for r in by_state["data"]}

        by_district = client.get(
            "/members", params={"filter[state]": "GA", "filter[district]": 5}
        ).json()
        assert by_district["data"] == []
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = ANY(%s)", ([sen, rep],))
        pg_conn.commit()


def test_get_member_by_id_returns_a_sitting_member_as_a_jsonapi_document(seeded_state):
    client = TestClient(app)
    response = client.get(f"/members/{seeded_state['rep']}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data"}
    assert body["data"]["type"] == "member"
    assert body["data"]["id"] == seeded_state["rep"]
    # No relationships on this resource -> the member is omitted, never
    # `"relationships": null` (invalid per JSON:API).
    assert "relationships" not in body["data"]
    attributes = body["data"]["attributes"]
    # Identity lives on the resource, not in attributes.
    assert "bioguide_id" not in attributes
    assert attributes["last_name"] == "Clark"
    assert attributes["role"] == "Representative"
    assert attributes["district"] == DISTRICT
    assert attributes["state"] == STATE
    assert attributes["in_office"] is True


def test_get_member_by_id_serves_a_departed_member_with_in_office_false(pg_conn):
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id, "Gone", "Gomez")
    _insert_term(pg_conn, bioguide_id, "HOUSE", 4, state="TX", end_year=LAST_YEAR)
    pg_conn.commit()

    try:
        client = TestClient(app)
        response = client.get(f"/members/{bioguide_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["data"]["id"] == bioguide_id
        assert body["data"]["attributes"]["state"] == "TX"
        assert body["data"]["attributes"]["in_office"] is False
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


def test_get_member_by_id_unknown_bioguide_id_returns_404(pg_conn):
    client = TestClient(app)
    response = client.get("/members/NOTAREALID99")

    assert response.status_code == 404
    # JSON:API error document, not RFC 9457 problem+json.
    assert response.headers["content-type"] == "application/vnd.api+json"
    error = response.json()["errors"][0]
    assert error["status"] == "404"
    assert error["title"] == "Not Found"


def test_get_member_by_id_rejects_an_unsupported_query_param(seeded_state):
    client = TestClient(app)
    response = client.get(f"/members/{seeded_state['rep']}", params={"include": "terms"})

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/vnd.api+json"
    assert "include" in response.json()["errors"][0]["detail"]


def test_get_member_by_id_success_is_vnd_api_json(seeded_state):
    client = TestClient(app)
    response = client.get(f"/members/{seeded_state['rep']}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.api+json"


def test_openapi_member_by_id_route_is_documented():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    get = schema["paths"]["/members/{bioguide_id}"]["get"]
    responses = get["responses"]
    assert "200" in responses
    for status in ("400", "404", "405", "406", "415", "422", "500"):
        assert list(responses[status]["content"]) == [JSONAPI_MEDIA_TYPE]
        ref = responses[status]["content"][JSONAPI_MEDIA_TYPE]["schema"]["$ref"]
        assert ref.endswith("/JsonApiErrorDocument")

    # 200 is a JSON:API single-resource document wrapping MemberDetail.
    document = _follow_ref(
        schema, responses["200"]["content"][JSONAPI_MEDIA_TYPE]["schema"]["$ref"]
    )
    assert set(document["properties"]) == {"data"}
    resource = _follow_ref(schema, document["properties"]["data"]["$ref"])
    assert set(resource["properties"]) == {"type", "id", "attributes", "relationships", "meta"}
    detail = _follow_ref(schema, resource["properties"]["attributes"]["$ref"])
    assert {"state", "in_office"} <= set(detail["required"])
    assert set(detail["properties"]) == {
        "first_name", "middle_name", "last_name", "nickname", "suffix",
        "role", "party", "phone", "website", "photo_url", "district",
        "state", "in_office",
    }
    assert "bioguide_id" not in detail["properties"]


def test_openapi_member_votes_route_is_documented():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    get = schema["paths"]["/members/{bioguide_id}/votes"]["get"]
    responses = get["responses"]
    assert "200" in responses
    for status in ("400", "404", "405", "406", "415", "422", "500"):
        assert list(responses[status]["content"]) == [JSONAPI_MEDIA_TYPE]

    parameters = {p["name"]: p for p in get["parameters"]}
    assert parameters["filter[bill]"]["required"] is True

    document = _follow_ref(
        schema, responses["200"]["content"][JSONAPI_MEDIA_TYPE]["schema"]["$ref"]
    )
    assert set(document["properties"]) == {"data", "meta"}
    resource = _follow_ref(
        schema, document["properties"]["data"]["items"]["$ref"]
    )
    assert set(resource["properties"]) == {"type", "id", "attributes", "relationships", "meta"}
    attrs = _follow_ref(schema, resource["properties"]["attributes"]["$ref"])
    assert set(attrs["properties"]) == {
        "vote_cast", "vote_question", "result", "vote_date",
    }


def test_openapi_registers_the_jsonapi_error_document_schema():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    doc = schema["components"]["schemas"]["JsonApiErrorDocument"]
    assert doc["required"] == ["errors"]
    item = doc["properties"]["errors"]["items"]
    assert set(item["required"]) == {"status", "title"}
    assert {"status", "title", "detail", "source"} <= set(item["properties"])


def _seed_roll_call(
    pg_conn, bioguide_id: str, bill_id: int, *, cast: str | None = "YEA",
    question: str = "On Passage", vote_date: str = "2026-05-20",
    chamber: str = "HOUSE",
) -> int:
    # Inserts one roll call on `bill_id` and, unless cast is None, this
    # member's vote on it. Returns vote_number so the caller can build the
    # expected roll_call id "<congress>-<chamber>-1-<vote_number>".
    vote_number = random_number(40000, 49000)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO roll_calls (
                chamber, congress, session, vote_number, bill_id,
                vote_question, result, vote_date, source_hash
            ) VALUES (%s, %s, 1, %s, %s, %s, 'Passed', %s, %s)
            RETURNING roll_call_id
            """,
            (chamber, CONGRESS, vote_number, bill_id, question, vote_date,
             f"hash-rc-{bill_id}-{vote_number}"),
        )
        roll_call_id = cur.fetchone()[0]
        if cast is not None:
            cur.execute(
                "INSERT INTO roll_call_member_votes (roll_call_id, bioguide_id, vote_cast) "
                "VALUES (%s, %s, %s)",
                (roll_call_id, bioguide_id, cast),
            )
    return vote_number


def test_get_member_votes_returns_roll_call_vote_resources_with_relationships(pg_conn):
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id, "Vera", "Voss")
    _insert_term(pg_conn, bioguide_id, "HOUSE", 3, state="CA")
    voted = random_number(20000, 24000)
    no_vote = random_number(24000, 28000)
    b_voted = _insert_bill(pg_conn, voted)
    b_no_vote = _insert_bill(pg_conn, no_vote)  # synced, member never voted
    vote_number = _seed_roll_call(pg_conn, bioguide_id, b_voted)
    pg_conn.commit()

    try:
        client = TestClient(app)
        response = client.get(
            f"/members/{bioguide_id}/votes",
            params={"filter[bill]": f"119-hr-{no_vote},119-hr-{voted},119-hr-99999"},
        )

        assert response.status_code == 200
        body = response.json()

        # One roll_call_vote resource, for the bill actually voted on.
        assert len(body["data"]) == 1
        vote = body["data"][0]
        assert vote["type"] == "roll_call_vote"
        assert vote["id"] == f"119-house-1-{vote_number}:{bioguide_id}"
        assert vote["attributes"] == {
            "vote_cast": "YEA", "vote_question": "On Passage",
            "result": "Passed", "vote_date": "2026-05-20",
        }
        assert vote["relationships"] == {
            "member": {"data": {"type": "member", "id": bioguide_id}},
            "roll_call": {"data": {"type": "roll_call", "id": f"119-house-1-{vote_number}"}},
            "bill": {"data": {"type": "bill", "id": f"119-hr-{voted}"}},
        }

        # Synced-but-no-vote bill -> meta; the unknown id -> nowhere.
        assert body["meta"]["bills_without_votes"] == [f"119-hr-{no_vote}"]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM roll_calls WHERE bill_id = ANY(%s)", ([b_voted, b_no_vote],)
            )
            cur.execute(
                "DELETE FROM bills WHERE bill_id = ANY(%s)", ([b_voted, b_no_vote],)
            )
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


def test_get_member_votes_orders_by_requested_bill_then_oldest_first(pg_conn):
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id, "Otto", "Ordway")
    _insert_term(pg_conn, bioguide_id, "HOUSE", 3, state="CA")
    a = random_number(20000, 24000)
    b = random_number(24000, 28000)
    bill_a = _insert_bill(pg_conn, a)
    bill_b = _insert_bill(pg_conn, b)
    _seed_roll_call(pg_conn, bioguide_id, bill_a, question="On Motion to Recommit",
                    vote_date="2026-05-19")
    _seed_roll_call(pg_conn, bioguide_id, bill_a, question="On Passage",
                    vote_date="2026-05-20", cast="NAY")
    _seed_roll_call(pg_conn, bioguide_id, bill_b, question="On Cloture",
                    vote_date="2026-04-01")
    pg_conn.commit()

    try:
        client = TestClient(app)
        # Request bill b first, though its vote is chronologically earliest.
        response = client.get(
            f"/members/{bioguide_id}/votes",
            params={"filter[bill]": f"119-hr-{b},119-hr-{a}"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert [
            (v["relationships"]["bill"]["data"]["id"], v["attributes"]["vote_question"])
            for v in data
        ] == [
            (f"119-hr-{b}", "On Cloture"),
            (f"119-hr-{a}", "On Motion to Recommit"),
            (f"119-hr-{a}", "On Passage"),
        ]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM roll_calls WHERE bill_id = ANY(%s)", ([bill_a, bill_b],))
            cur.execute("DELETE FROM bills WHERE bill_id = ANY(%s)", ([bill_a, bill_b],))
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


def test_get_member_votes_unknown_member_returns_404(pg_conn):
    client = TestClient(app)
    response = client.get(
        "/members/NOTAREALID99/votes", params={"filter[bill]": "119-hr-1"}
    )

    assert response.status_code == 404
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    assert response.json()["errors"][0]["status"] == "404"


def test_get_member_votes_malformed_bill_id_returns_400(seeded_state):
    client = TestClient(app)
    response = client.get(
        f"/members/{seeded_state['rep']}/votes",
        params={"filter[bill]": "119-hr-2616,not-a-bill"},
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    assert "not-a-bill" in response.json()["errors"][0]["detail"]


def test_get_member_votes_missing_filter_returns_422_with_source_parameter(seeded_state):
    client = TestClient(app)
    response = client.get(f"/members/{seeded_state['rep']}/votes")

    assert response.status_code == 422
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    error = response.json()["errors"][0]
    assert error["status"] == "422"
    assert error["source"]["parameter"] == "filter[bill]"


def test_get_member_votes_too_many_bills_returns_422(seeded_state):
    client = TestClient(app)
    bills = ",".join(f"119-hr-{i}" for i in range(1, 52))
    response = client.get(
        f"/members/{seeded_state['rep']}/votes", params={"filter[bill]": bills}
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE


def test_get_member_votes_success_is_vnd_api_json(pg_conn):
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    _insert_member(pg_conn, bioguide_id, "Sue", "Serrano")
    _insert_term(pg_conn, bioguide_id, "HOUSE", 3, state="CA")
    pg_conn.commit()

    try:
        client = TestClient(app)
        response = client.get(
            f"/members/{bioguide_id}/votes",
            params={"filter[bill]": "119-hr-1"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
        pg_conn.commit()


@pytest.mark.parametrize("bad_param", ["include=roll_call", "sort=vote_date", "fields[bill]=title"])
def test_get_member_votes_rejects_unsupported_query_params(seeded_state, bad_param):
    client = TestClient(app)
    key, value = bad_param.split("=")
    response = client.get(
        f"/members/{seeded_state['rep']}/votes",
        params={"filter[bill]": "119-hr-1", key: value},
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    assert key.split("[")[0] in response.json()["errors"][0]["detail"]


def test_jsonapi_route_rejects_a_parametrized_content_type(seeded_state):
    client = TestClient(app)
    response = client.get(
        f"/members/{seeded_state['rep']}",
        headers={"Content-Type": "application/vnd.api+json; charset=utf-8"},
    )

    assert response.status_code == 415
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE


def test_jsonapi_route_rejects_an_only_parametrized_accept(seeded_state):
    client = TestClient(app)
    response = client.get(
        f"/members/{seeded_state['rep']}",
        headers={"Accept": "application/vnd.api+json; version=1.1"},
    )

    assert response.status_code == 406
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE


def test_jsonapi_route_allows_an_unparametrized_accept_alongside_a_parametrized_one(seeded_state):
    client = TestClient(app)
    response = client.get(
        f"/members/{seeded_state['rep']}",
        headers={"Accept": "application/vnd.api+json, application/vnd.api+json; version=1"},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "accept",
    [
        "application/vnd.api+json; q=0.9",  # q is an RFC 9110 weight, not a media-type param
        'application/vnd.api+json; profile="https://example.com/last-modified"',  # 1.1-exempt
        "application/vnd.api+json;q=0.8, text/html;q=0.9",
    ],
)
def test_jsonapi_route_does_not_406_on_q_or_profile(seeded_state, accept):
    client = TestClient(app)
    response = client.get(
        f"/members/{seeded_state['rep']}", headers={"Accept": accept}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE


def test_jsonapi_route_does_not_415_on_a_profile_content_type(seeded_state):
    client = TestClient(app)
    response = client.get(
        f"/members/{seeded_state['rep']}",
        headers={"Content-Type": 'application/vnd.api+json; profile="https://example.com/x"'},
    )

    assert response.status_code == 200


def test_jsonapi_route_tolerates_a_semicolon_inside_a_quoted_profile_value(seeded_state):
    # `profile` is 1.1-exempt; a naive `;` split would see `b"` as a
    # bogus second parameter and 415. Quoted-string-aware parsing must not.
    client = TestClient(app)
    response = client.get(
        f"/members/{seeded_state['rep']}",
        headers={"Content-Type": 'application/vnd.api+json; profile="https://example.com/a;b"'},
    )

    assert response.status_code == 200


def test_get_member_votes_rejects_a_repeated_filter_param(seeded_state):
    # A repeated ?filter[bill]=a&filter[bill]=b would bind only one
    # occurrence and silently drop the rest -> JsonApiRoute 400s it.
    client = TestClient(app)
    response = client.get(
        f"/members/{seeded_state['rep']}/votes",
        params=[("filter[bill]", "119-hr-1"), ("filter[bill]", "119-hr-2")],
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    assert "filter[bill]" in response.json()["errors"][0]["detail"]


def _vector(*first_values: float, dimensions: int = 1024) -> list[float]:
    values = list(first_values) + [0.0] * (dimensions - len(first_values))
    return values


def _insert_bill(
    pg_conn,
    bill_number: int,
    policy_area: str | None = None,
    embedding: list[float] | None = None,
) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bills (
                congress, bill_type, bill_number, title, policy_area,
                crs_summary_embedding, source_hash
            ) VALUES (%s, 'HR', %s, %s, %s, %s::vector, %s)
            RETURNING bill_id
            """,
            (
                CONGRESS, bill_number, f"Test Bill {bill_number}", policy_area,
                db._to_pgvector_literal(embedding) if embedding else None,
                f"hash-bill-{bill_number}",
            ),
        )
        return cur.fetchone()[0]


def _insert_subject(pg_conn, bill_id: int, subject_name: str) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bill_subjects (bill_id, subject_name) VALUES (%s, %s)",
            (bill_id, subject_name),
        )


def _insert_vocab_term(pg_conn, kind: str, term: str, embedding: list[float]) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vocab_term_embeddings (kind, term, embedding) VALUES (%s, %s, %s::vector)",
            (kind, term, db._to_pgvector_literal(embedding)),
        )


def test_openapi_bills_search_is_a_jsonapi_bill_collection():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    schemas = schema["components"]["schemas"]
    assert "BillSearchResponse" not in schemas
    assert "BillVote" not in schemas
    # `match` is per-resource meta, not a Bill attribute.
    assert set(schemas["Bill"]["properties"]) == {
        "congress", "bill_type", "bill_number", "title", "policy_area",
        "crs_summary",
    }

    get = schema["paths"]["/bills"]["get"]
    document = _follow_ref(
        schema, get["responses"]["200"]["content"][JSONAPI_MEDIA_TYPE]["schema"]["$ref"]
    )
    assert set(document["properties"]) == {"data", "meta"}
    resource = _follow_ref(schema, document["properties"]["data"]["items"]["$ref"])
    assert set(resource["properties"]) == {"type", "id", "attributes", "relationships", "meta"}


def test_openapi_bills_search_documents_query_parameters():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    parameters = {p["name"]: p for p in schema["paths"]["/bills"]["get"]["parameters"]}
    assert set(parameters) == {"filter[query]", "page[size]"}
    assert parameters["filter[query]"]["required"] is True
    assert parameters["page[size]"]["required"] is False


def test_openapi_bills_search_errors_use_vnd_api_json():
    client = TestClient(app)
    schema = client.get("/openapi.json").json()

    responses = schema["paths"]["/bills"]["get"]["responses"]
    for status in ("400", "405", "406", "415", "422", "500", "503"):
        assert list(responses[status]["content"]) == [JSONAPI_MEDIA_TYPE]
    assert "404" not in responses


def test_get_bills_search_missing_filter_query_returns_422(monkeypatch):
    monkeypatch.setattr(bedrock, "embed", lambda client, text: _vector(1.0, 0.0))
    client = TestClient(app)
    response = client.get("/bills")

    assert response.status_code == 422
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    assert response.json()["errors"][0]["source"]["parameter"] == "filter[query]"


def test_get_bills_search_rejects_the_old_query_params(monkeypatch):
    # q / bioguide_id / limit are gone; JsonApiRoute 400s any undeclared param.
    monkeypatch.setattr(bedrock, "embed", lambda client, text: _vector(1.0, 0.0))
    client = TestClient(app)
    response = client.get("/bills", params={"q": "dreamers", "bioguide_id": "X"})

    assert response.status_code == 400
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    detail = response.json()["errors"][0]["detail"]
    assert "q" in detail and "bioguide_id" in detail


def test_get_bills_search_bedrock_failure_returns_503(monkeypatch):
    def _boom(client, text):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(bedrock, "embed", _boom)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/bills", params={"filter[query]": "dreamers"})

    assert response.status_code == 503
    assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
    assert response.json()["errors"][0]["status"] == "503"


def test_get_bills_search_tier1_policy_area_match(monkeypatch, pg_conn):
    term = f"test-policy-area-{uuid.uuid4().hex[:8]}"
    bill_number = random_number(20000, 29000)
    _insert_vocab_term(pg_conn, "POLICY_AREA", term, _vector(1.0, 0.0))
    bill_id = _insert_bill(pg_conn, bill_number, policy_area=term)
    pg_conn.commit()
    monkeypatch.setattr(bedrock, "embed", lambda client, text: _vector(1.0, 0.0))

    try:
        client = TestClient(app)
        response = client.get(
            "/bills", params={"filter[query]": "some free text"}
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == JSONAPI_MEDIA_TYPE
        body = response.json()
        assert body["meta"] == {"query": "some free text"}
        matched = next(
            r for r in body["data"] if r["id"] == f"119-hr-{bill_number}"
        )
        assert matched["type"] == "bill"
        assert "relationships" not in matched
        assert matched["attributes"]["policy_area"] == term
        assert matched["meta"] == {"matches": [{"via": "policy_area"}]}
        assert "id" not in matched["attributes"]
        assert "matches" not in matched["attributes"]
        assert "votes" not in matched["attributes"]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM vocab_term_embeddings WHERE term = %s", (term,))
        pg_conn.commit()


def test_get_bills_search_tier1_subject_match(monkeypatch, pg_conn):
    term = f"test-subject-{uuid.uuid4().hex[:8]}"
    bill_number = random_number(20000, 29000)
    _insert_vocab_term(pg_conn, "SUBJECT", term, _vector(1.0, 0.0))
    bill_id = _insert_bill(pg_conn, bill_number)
    _insert_subject(pg_conn, bill_id, term)
    pg_conn.commit()
    monkeypatch.setattr(bedrock, "embed", lambda client, text: _vector(1.0, 0.0))

    try:
        client = TestClient(app)
        response = client.get("/bills", params={"filter[query]": "x"})

        assert response.status_code == 200
        matched = next(
            r for r in response.json()["data"] if r["id"] == f"119-hr-{bill_number}"
        )
        assert matched["meta"]["matches"] == [{"via": "subject"}]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bill_subjects WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute("DELETE FROM vocab_term_embeddings WHERE term = %s", (term,))
        pg_conn.commit()


def test_get_bills_search_falls_back_to_similarity_with_via_summary(monkeypatch, pg_conn):
    unrelated_term = f"test-unrelated-{uuid.uuid4().hex[:8]}"
    bill_number = random_number(20000, 29000)
    # Vocab term far from the query embedding -> tier 1 finds nothing
    # confident, so tier-2 similarity search surfaces the bill.
    _insert_vocab_term(pg_conn, "POLICY_AREA", unrelated_term, _vector(0.0, 1.0))
    bill_id = _insert_bill(pg_conn, bill_number, embedding=_vector(1.0, 0.0))
    pg_conn.commit()
    monkeypatch.setattr(bedrock, "embed", lambda client, text: _vector(1.0, 0.0))

    try:
        client = TestClient(app)
        response = client.get("/bills", params={"filter[query]": "x"})

        assert response.status_code == 200
        matched = next(
            r for r in response.json()["data"] if r["id"] == f"119-hr-{bill_number}"
        )
        assert matched["meta"]["matches"] == [{"via": "summary"}]
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute(
                "DELETE FROM vocab_term_embeddings WHERE term = %s", (unrelated_term,)
            )
        pg_conn.commit()


def test_get_bills_search_omits_bills_beyond_the_relevance_floor(monkeypatch, pg_conn):
    # A query with no genuinely related bill returns fewer (here zero)
    # results rather than backfilling with the least-far bill.
    unrelated_term = f"test-unrelated-{uuid.uuid4().hex[:8]}"
    bill_number = random_number(20000, 29000)
    _insert_vocab_term(pg_conn, "POLICY_AREA", unrelated_term, _vector(0.0, 1.0))
    # cosine distance 1.0 from the query embedding -- past BILL_SIMILARITY_THRESHOLD (0.80).
    bill_id = _insert_bill(pg_conn, bill_number, embedding=_vector(0.0, 1.0))
    pg_conn.commit()
    monkeypatch.setattr(bedrock, "embed", lambda client, text: _vector(1.0, 0.0))

    try:
        client = TestClient(app)
        response = client.get("/bills", params={"filter[query]": "x"})

        assert response.status_code == 200
        assert f"119-hr-{bill_number}" not in {r["id"] for r in response.json()["data"]}
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM bills WHERE bill_id = %s", (bill_id,))
            cur.execute(
                "DELETE FROM vocab_term_embeddings WHERE term = %s", (unrelated_term,)
            )
        pg_conn.commit()
