from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from strawberry.fastapi import GraphQLRouter

from cd.server import settings
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # users_service and ai_summary_service each hold an asyncpg pool that
    # must be opened before serving any request, not just closed after the
    # last one -- unlike cd_api_service/geocoder_service, whose pools open
    # synchronously at import time (see schema.py), asyncpg.create_pool()
    # is a coroutine and has no synchronous equivalent.
    await users_service.connect()
    await ai_summary_service.connect()
    yield
    # cd_api_service's aclose() delegates to its underlying ApiClient
    # (HttpApiClient holds an open httpx.AsyncClient connection pool;
    # LambdaApiClient's is a no-op, boto3 needs no explicit close).
    # geocoder_service holds its own separate connection pool for the
    # (unrelated) Census geocoder. See services/cd_api_service.py and
    # services/geocoder_service.py.
    await cd_api_service.aclose()
    await geocoder_service.aclose()
    await users_service.aclose()
    await ai_summary_service.aclose()


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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Same shape as cd-api's GET /version -- a plain REST endpoint alongside
# the GraphQL `version` query, so a version check doesn't need a GraphQL
# client/curl -X POST just to confirm what's running.
@app.get("/version")
def version() -> dict:
    return {"version": VERSION}
