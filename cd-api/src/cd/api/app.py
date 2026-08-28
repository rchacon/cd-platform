from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from mangum import Mangum
from mangum.adapter import DEFAULT_TEXT_MIME_TYPES
from starlette.exceptions import HTTPException as StarletteHTTPException

from cd.api import bedrock
from cd.api.apportionment import is_valid_district, max_valid_district
from cd.api.db import (
    fetch_bills_by_policy_area,
    fetch_bills_by_similarity,
    fetch_bills_by_subject,
    fetch_closest_vocab_term,
    fetch_current_members,
    fetch_votes_for_bills,
    member_exists,
)
from cd.api.models import (
    PROBLEM_DETAIL_SCHEMA,
    VALIDATION_PROBLEM_DETAIL_SCHEMA,
    VersionResponse,
)
from cd.api.problem import MEDIA_TYPE, problem_response
from cd.api.search import shape_bill_search_response
from cd.api.transform import group_representatives
from cd.lib.models import BillSearchResponse, MembersResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VERSION_FILE = Path(__file__).parent / "VERSION"

# cd-platform#46: this used to live only as hand-written prose in
# cd-website's api.astro, disconnected from the code it describes and
# with nothing forcing it to stay in sync. Living here instead means a
# PR that changes the error contract or the 404/vacancy behavior below
# has to touch this same description, in the same diff.
DESCRIPTION = """\
REST API for `cd-lookup` (the WordPress plugin), replacing its GovTrack \
HTML scrape with a direct HTTP interface over `current_members`.

**Auth:** every request requires an `X-Api-Key` header. Enforced by API \
Gateway ahead of this application -- a missing or invalid key never \
reaches this code, so it isn't reflected in any route's documented \
responses below.

**Errors:** every non-2xx response follows \
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) ("Problem Details for \
HTTP APIs") -- `Content-Type: application/problem+json`, body shaped \
`{"type", "title", "status", "detail", ...}`, never a bespoke \
`{"error": "..."}` shape.\
"""

# Matches api_gateway_base_path below -- API Gateway's custom domain
# fronts requests at this exact path, so it's what every documented
# example/client call should actually be made against.
PRODUCTION_SERVER_URL = "https://api.civicdog.com/v1"


def _read_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "dev"


app = FastAPI(
    title="cd-api",
    version=_read_version(),
    description=DESCRIPTION,
    servers=[{"url": PRODUCTION_SERVER_URL, "description": "Production"}],
)

# Built once at import time (Lambda cold start), same precedent as
# cd-etl's own bedrock_embeddings._BEDROCK_CLIENT module-level construction
# in bills_etl.py/house_votes_etl.py.
_BEDROCK_CLIENT = bedrock.build_bedrock_client()

# A query embedding within this cosine distance of the closest vocab term
# is treated as a confident tier-1 match (exact policy_area/subject_name
# lookup); anything farther falls through to tier-2 similarity search
# over bills.crs_summary_embedding instead. Placeholder -- tune
# empirically once real query traffic exists.
VOCAB_MATCH_THRESHOLD = 0.25

# Relevance floor for tier-2 similarity search: a bill farther than this
# from the query embedding is treated as "not actually about this topic"
# and excluded, rather than backfilled in just to pad the response out
# to `limit`. Unlike VOCAB_MATCH_THRESHOLD (a pure guess), this was
# calibrated against real Titan V2 embeddings of real synced bills:
# genuinely on-topic matches clustered at ~0.72-0.78 cosine distance
# across several test queries, while a query with no genuinely related
# bill in the corpus only produced matches at 0.87+ -- 0.80 sits
# cleanly in the gap between the two. Still worth re-tuning once real
# query traffic and a full-size corpus (a few hundred bills, not 61)
# exist.
BILL_SIMILARITY_THRESHOLD = 0.80


def _problem_response(description: str, model_name: str) -> dict:
    return {
        "description": description,
        "content": {
            "application/problem+json": {"schema": {"$ref": f"#/components/schemas/{model_name}"}}
        },
    }


def _custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        servers=app.servers,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})

    # ProblemDetail/ValidationProblemDetail are never used as a route's
    # response_model (only referenced by hand-written $refs above), so
    # nothing else registers them as reusable components the way FastAPI
    # does automatically for MembersResponse/Member/VersionResponse.
    schemas = components.setdefault("schemas", {})
    schemas["ProblemDetail"] = PROBLEM_DETAIL_SCHEMA
    schemas["ValidationProblemDetail"] = VALIDATION_PROBLEM_DETAIL_SCHEMA

    # X-Api-Key isn't a FastAPI Security(...) dependency -- API Gateway
    # enforces it ahead of this application (see DESCRIPTION above), so
    # there's no route-level dependency for FastAPI to derive this from
    # automatically, the way it does for response models. Added by hand
    # here instead, applied globally (every route requires it in
    # production) via the top-level `security` key.
    components["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Api-Key",
            "description": (
                "Required on every request. Enforced by API Gateway in "
                "front of this application, not by cd-api's own code."
            ),
        }
    }
    schema["security"] = [{"ApiKeyAuth": []}]

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = _custom_openapi


# Registered on Starlette's base HTTPException, not FastAPI's subclass:
# Starlette's own router raises the base class directly for unmatched
# routes (404) and disallowed methods (405), which a handler registered
# only on the FastAPI subclass would never catch (a base-class instance
# doesn't match a subclass-keyed handler). FastAPI's HTTPException IS-A
# this base class, so app-raised HTTPExceptions still hit this handler too.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    return problem_response(status=exc.status_code, detail=exc.detail)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return problem_response(
        status=422,
        detail="Request parameters failed validation.",
        errors=exc.errors(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # The client only ever sees the generic detail below -- this is the
    # only place a DB failure, timeout, etc. leaves a trace at all
    # (CloudWatch on Lambda, stderr locally), since nothing upstream logs.
    logger.exception("Unhandled exception for %s %s", request.method, request.url.path)
    return problem_response(status=500, detail="An unexpected error occurred.")


@app.get(
    "/version",
    response_model=VersionResponse,
    responses={
        405: _problem_response("HTTP method not allowed for this path.", "ProblemDetail"),
        500: _problem_response("An unexpected error occurred.", "ProblemDetail"),
    },
)
def get_version() -> dict:
    return {"version": _read_version()}


@app.get(
    "/members",
    response_model=MembersResponse,
    responses={
        404: _problem_response(
            "Unknown state, or a district that doesn't exist for the given state.",
            "ProblemDetail",
        ),
        405: _problem_response("HTTP method not allowed for this path.", "ProblemDetail"),
        422: _problem_response(
            "Request parameters failed validation.", "ValidationProblemDetail"
        ),
        500: _problem_response("An unexpected error occurred.", "ProblemDetail"),
    },
)
def get_members(
    state: str = Query(..., min_length=2, max_length=2, pattern="^[A-Za-z]{2}$"),
    district: int | None = Query(
        None,
        ge=0,
        description=(
            "Omit entirely to get senators only. `0` selects the state's "
            "single at-large House seat (only valid for 1-seat states/"
            "territories, e.g. WY, DC). `1` and above selects a specific "
            "numbered House district. There is no explicit \"null\" form -- "
            "HTTP query strings can't express it, so `district=` or "
            "`district=null` both fail validation; omission is the only "
            "way to get the senators-only behavior."
        ),
    ),
) -> dict:
    """Look up current senators and representative(s) for a state.

    Optionally scoped to one House district via `district` -- see that
    parameter's own description for its omitted/0/1+ semantics.

    `district` draws a distinction between two different kinds of "no
    representative": a district number that doesn't exist for the given
    state (validated against real House apportionment) returns `404`,
    while a district that does exist but is currently vacant (a real
    seat between office-holders) still returns `200` with an empty
    `representatives` list. `senators` is populated either way.
    """
    # Distinguishes "this district doesn't exist" from "this district
    # exists but is currently vacant" (see cd-platform#12) -- without this,
    # both cases fall through to the same 200 + empty representatives list
    # below, since current_members' query includes the state's senators
    # regardless of whether any representative matches the district.
    seats = max_valid_district(state)
    if district is not None and not is_valid_district(state, district, seats=seats):
        raise HTTPException(
            status_code=404,
            detail=(
                f"District {district} does not exist for state {state.upper()} "
                f"({seats} district{'s' if seats != 1 else ''})."
            ),
        )

    rows = fetch_current_members(state.upper(), district)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No data found for state {state.upper()}")
    return group_representatives(rows)


@app.get(
    "/bills/search",
    response_model=BillSearchResponse,
    responses={
        404: _problem_response("Unknown bioguide_id.", "ProblemDetail"),
        405: _problem_response("HTTP method not allowed for this path.", "ProblemDetail"),
        422: _problem_response(
            "Request parameters failed validation.", "ValidationProblemDetail"
        ),
        500: _problem_response("An unexpected error occurred.", "ProblemDetail"),
        503: _problem_response(
            "Search is temporarily unavailable (embedding generation failed).",
            "ProblemDetail",
        ),
    },
)
def get_bills_search(
    q: str = Query(..., min_length=1, max_length=500, description="Free-text search query."),
    bioguide_id: str = Query(
        ..., description="Scopes results to how this specific representative voted."
    ),
    limit: int = Query(10, ge=1, le=50),
) -> dict:
    """Semantic search over bills, scoped to one representative's votes.

    Matches `q` against a bill's policy area or legislative subjects
    first (tier 1, exact controlled-vocabulary match against the
    closest embedding in `vocab_term_embeddings`); any remaining slots
    up to `limit` are then considered for tier-2 cosine-similarity
    search directly against each bill's own summary embedding, subject
    to `BILL_SIMILARITY_THRESHOLD` -- a bill farther than that from `q`
    is excluded rather than backfilled in, so this can return fewer
    than `limit` (even zero) bills when nothing in the corpus is
    genuinely close enough. Each returned bill includes every roll call
    `bioguide_id` cast in their own chamber for it (empty if the bill
    matched but they never voted on it).
    """
    if not member_exists(bioguide_id):
        raise HTTPException(status_code=404, detail=f"Unknown bioguide_id {bioguide_id}")

    try:
        query_embedding = bedrock.embed_query(_BEDROCK_CLIENT, q)
    except Exception as exc:
        # Unlike cd-etl's degrade-gracefully precedent for a stale
        # embedding, there's nothing to fall back to here -- the
        # embedding IS the query. 503 signals retryability more usefully
        # than the generic 500 catch-all below.
        logger.exception("Bedrock embed failed for query %r", q)
        raise HTTPException(
            status_code=503, detail="Search is temporarily unavailable."
        ) from exc

    vocab_match = fetch_closest_vocab_term(query_embedding)
    tier1_bills = []
    if vocab_match is not None and vocab_match["distance"] <= VOCAB_MATCH_THRESHOLD:
        fetch = (
            fetch_bills_by_policy_area
            if vocab_match["kind"] == "POLICY_AREA"
            else fetch_bills_by_subject
        )
        tier1_bills = fetch(vocab_match["term"], limit)

    remaining = limit - len(tier1_bills)
    tier2_bills = (
        fetch_bills_by_similarity(
            query_embedding, [b["bill_id"] for b in tier1_bills], remaining,
            BILL_SIMILARITY_THRESHOLD,
        )
        if remaining > 0
        else []
    )

    all_bills = tier1_bills + tier2_bills
    votes = fetch_votes_for_bills([b["bill_id"] for b in all_bills], bioguide_id)
    return shape_bill_search_response(q, bioguide_id, all_bills, votes)


# API Gateway's custom-domain base_path_mapping ("v1") is used to select
# which API/stage a request routes to, but AWS does not strip it from the
# path forwarded to the Lambda integration -- confirmed empirically against
# api.civicdog.com/v1 (cd-infra#19), which 404'd since FastAPI's routes are
# plain /members, not /v1/members. api_gateway_base_path tells Mangum to
# strip it itself before routing to the ASGI app.
#
# text_mime_types adds MEDIA_TYPE (application/problem+json) on top of
# Mangum's own defaults -- without it, Mangum doesn't recognize that
# content type as text, base64-encodes every error response body, and
# sets isBase64Encoded=true. API Gateway only decodes that back for
# content types in its own binaryMediaTypes config (cd-infra), which
# isn't set for application/problem+json, so clients received raw base64
# instead of JSON for every error response (cd-platform#38).
handler = Mangum(
    app,
    api_gateway_base_path="/v1",
    text_mime_types=[*DEFAULT_TEXT_MIME_TYPES, MEDIA_TYPE],
)
