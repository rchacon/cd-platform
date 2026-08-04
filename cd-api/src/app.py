from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from mangum import Mangum
from starlette.exceptions import HTTPException as StarletteHTTPException

from apportionment import is_valid_district, max_valid_district
from db import fetch_current_members
from models import (
    PROBLEM_DETAIL_SCHEMA,
    VALIDATION_PROBLEM_DETAIL_SCHEMA,
    MembersResponse,
    VersionResponse,
)
from problem import problem_response
from transform import group_representatives

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VERSION_FILE = Path(__file__).parent / "VERSION"


def _read_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "dev"


app = FastAPI(title="cd-api", version=_read_version())


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

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    # ProblemDetail/ValidationProblemDetail are never used as a route's
    # response_model (only referenced by hand-written $refs above), so
    # nothing else registers them as reusable components the way FastAPI
    # does automatically for MembersResponse/Person/VersionResponse.
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas["ProblemDetail"] = PROBLEM_DETAIL_SCHEMA
    schemas["ValidationProblemDetail"] = VALIDATION_PROBLEM_DETAIL_SCHEMA

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


# API Gateway's custom-domain base_path_mapping ("v1") is used to select
# which API/stage a request routes to, but AWS does not strip it from the
# path forwarded to the Lambda integration -- confirmed empirically against
# api.civicdog.com/v1 (cd-infra#19), which 404'd since FastAPI's routes are
# plain /members, not /v1/members. api_gateway_base_path tells Mangum to
# strip it itself before routing to the ASGI app.
handler = Mangum(app, api_gateway_base_path="/v1")
