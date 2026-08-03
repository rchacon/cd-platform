from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mangum import Mangum
from starlette.exceptions import HTTPException as StarletteHTTPException

from apportionment import is_valid_district, max_valid_district
from db import fetch_current_members
from problem import problem_response
from transform import group_representatives

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

VERSION_FILE = Path(__file__).parent / "VERSION"


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


def _read_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except FileNotFoundError:
        return "dev"


@app.get("/version")
def get_version() -> dict:
    return {"version": _read_version()}


@app.get("/members")
def get_members(
    state: str = Query(..., min_length=2, max_length=2, pattern="^[A-Za-z]{2}$"),
    district: int | None = Query(None, ge=0),
) -> dict:
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
