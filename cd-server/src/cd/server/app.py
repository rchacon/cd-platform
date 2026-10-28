import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from strawberry.fastapi import GraphQLRouter

from cd.api import db as api_db
from cd.api.app import app as api_app
from cd.server import settings
from cd.server.origin_guard import OriginGuard
from cd.server.schema import (
    GRAPHIQL_ENABLED,
    VERSION,
    ai_summary_service,
    cd_api_service,
    geocoder_service,
    schema,
    users_service,
)
from cd.server.services.users_service import InvalidTokenError

logger = logging.getLogger(__name__)


def require_cd_api_db_config(environment: str, environ=os.environ) -> None:
    # cd.api.db falls back to localhost when CD_API_PGHOST is unset, which
    # only makes sense in local dev. Elsewhere /v1 and every resolver backed
    # by CdApiService (members, bill search, AI summaries) would fail on a
    # connection error to localhost, while /health still passed. Failing at
    # import instead keeps a misconfigured deploy from replacing healthy
    # tasks -- same fail-fast precedent as get_users_service().
    if environment != "local" and not environ.get("CD_API_PGHOST"):
        raise RuntimeError(
            'CD_API_PGHOST must be set when CD_SERVER_ENVIRONMENT is not "local" '
            "-- cd.api's database pool would otherwise try localhost."
        )


require_cd_api_db_config(settings.ENVIRONMENT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # users_service, ai_summary_service and cd.api each hold an asyncpg pool that
    # must be opened before serving any request, not just closed after the
    # last one -- unlike cd_api_service/geocoder_service, whose pools open
    # synchronously at import time (see schema.py), asyncpg.create_pool()
    # is a coroutine and has no synchronous equivalent.
    #
    # An AsyncExitStack, so a startup step that raises still closes
    # everything opened before it, and one aclose() that raises doesn't
    # stop the rest from running. Callbacks run in reverse order.
    async with AsyncExitStack() as stack:
        # cd_api_service and geocoder_service each hold an httpx client,
        # already open (see services/cd_api_service.py and
        # services/geocoder_service.py): cd_api_service's calls cd.api
        # in-process, geocoder_service's the (unrelated) Census geocoder.
        stack.push_async_callback(cd_api_service.aclose)
        stack.push_async_callback(geocoder_service.aclose)
        await users_service.connect()
        stack.push_async_callback(users_service.aclose)
        await ai_summary_service.connect()
        stack.push_async_callback(ai_summary_service.aclose)
        # cd.api's congressional_app pool (mounted at /v1). Its own lifespan
        # never runs under a mount, so it's opened here.
        await api_db.connect()
        stack.push_async_callback(api_db.close)
        yield


app = FastAPI(title="cd-server", lifespan=lifespan)

# cd-webapp is the only browser client this needs to allow -- POST is all
# it needs (GraphQL queries/mutations both go over POST; GraphiQL's own
# in-browser requests are same-origin, not subject to CORS at all).
# allow_credentials=False since there's no cookie/session auth yet (see
# AGENTS.md's cd-server section) -- a future API-key scheme would use the
# Authorization header, already allowed below, not credentialed cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_methods=["POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)

# GRAPHIQL_ENABLED (read in schema.py, imported here) is off by default --
# only docker-compose's dev service sets it. The production image (what's
# actually built and deployed to ECS) ships with both the IDE and schema
# introspection (see schema.py) disabled, since nothing gates /graphql
# behind auth yet. Query execution via POST is unaffected either way --
# this only controls whether a browser GET serves the IDE.


# Runs before every GraphQL request (query or mutation) -- upserts the
# caller into cd_customers if the Authorization header carries a valid
# Cognito ID token, and silently does nothing if there's no header at all
# (see UsersService.upsert_user_from_authorization_header's own docstring
# -- most resolvers don't require auth, so an anonymous request must
# never be blocked). A bearer token that IS present but fails to verify
# is a different case: InvalidTokenError propagates here as an HTTP 401,
# rejecting the whole request before Strawberry ever executes it.
#
# The verified Cognito `sub` (or None for an anonymous caller) is placed
# in GraphQL context as "user_id" -- a resolver that requires auth (e.g.
# schema.py's summarizeVotingRecord/myAiSummaries) reads
# info.context["user_id"] and raises its own error if it's None. A
# resolver only ever sees "anonymous" or "verified" here, never "invalid"
# -- an actually-bad token 401s above before any resolver runs.
async def get_graphql_context(request: Request) -> dict:
    try:
        user_id = await users_service.upsert_user_from_authorization_header(
            request.headers.get("Authorization")
        )
    except InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    return {"user_id": user_id}


app.include_router(
    GraphQLRouter(
        schema,
        graphql_ide="graphiql" if GRAPHIQL_ENABLED else None,
        context_getter=get_graphql_context,
    ),
    prefix="/graphql",
)

# cd-api's REST API (cd/api/), mounted as its own sub-application rather
# than merged in as routers: it keeps its own exception handlers (JSON:API /
# problem+json errors on /v1, Strawberry's own on /graphql) and its own
# OpenAPI document (/v1/openapi.json, cd-api's routes only). API Gateway
# enforces X-Api-Key in front of /v1 -- this app never checks it, but
# OriginGuard rejects any /v1 request that didn't come through API Gateway.
app.mount(
    "/v1",
    OriginGuard(api_app, settings.CD_API_ORIGIN_SECRET, settings.ENVIRONMENT),
)


# async def, not a plain def: FastAPI runs sync endpoints on AnyIO's shared
# threadpool (40 tokens), the same one cd.api's sync /v1 routes use. A
# burst of slow /v1 requests holding every token would otherwise queue the
# ALB's health check behind them until it times out and ECS replaces a
# task that's actually fine. Neither of these does any blocking work, so
# they run directly on the event loop instead.
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Same shape as cd-api's GET /version -- a plain REST endpoint alongside
# the GraphQL `version` query, so a version check doesn't need a GraphQL
# client/curl -X POST just to confirm what's running.
@app.get("/version")
async def version() -> dict:
    return {"version": VERSION}
