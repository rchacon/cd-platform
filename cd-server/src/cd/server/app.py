from contextlib import asynccontextmanager

from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter

from cd.server.schema import GRAPHIQL_ENABLED, VERSION, api_client, schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # HttpApiClient holds an open httpx.AsyncClient connection pool;
    # LambdaApiClient's aclose() is a no-op (boto3 needs no explicit
    # close). See ApiClient.aclose() in clients.py.
    await api_client.aclose()


app = FastAPI(title="cd-server", lifespan=lifespan)

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
