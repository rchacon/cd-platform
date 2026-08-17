from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from strawberry.fastapi import GraphQLRouter

from cd.server import settings
from cd.server.schema import GRAPHIQL_ENABLED, VERSION, cd_api_service, geocoder_service, schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # cd_api_service's aclose() delegates to its underlying ApiClient
    # (HttpApiClient holds an open httpx.AsyncClient connection pool;
    # LambdaApiClient's is a no-op, boto3 needs no explicit close).
    # geocoder_service holds its own separate connection pool for the
    # (unrelated) Census geocoder. See services/cd_api_service.py and
    # services/geocoder_service.py.
    await cd_api_service.aclose()
    await geocoder_service.aclose()


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
app.include_router(
    GraphQLRouter(schema, graphql_ide="graphiql" if GRAPHIQL_ENABLED else None),
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
