from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mangum import Mangum
from mangum.adapter import DEFAULT_TEXT_MIME_TYPES
from starlette.exceptions import HTTPException as StarletteHTTPException

from cd.api.openapi import build_openapi
from cd.api.problem import MEDIA_TYPE, problem_response
from cd.api.routes import bills, members, version
from cd.api.routes.version import read_version

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# cd-platform#46: this used to live only as hand-written prose in
# cd-website's api.astro, disconnected from the code it describes and
# with nothing forcing it to stay in sync. Living here instead means a
# PR that changes the error contract or a route's 404 behavior (in
# routes/) has to touch this same description, in the same diff.
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


app = FastAPI(
    title="cd-api",
    version=read_version(),
    description=DESCRIPTION,
    servers=[{"url": PRODUCTION_SERVER_URL, "description": "Production"}],
)


def _openapi() -> dict:
    return build_openapi(app)


app.openapi = _openapi


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


app.include_router(version.router)
app.include_router(members.router)
app.include_router(bills.router)


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
