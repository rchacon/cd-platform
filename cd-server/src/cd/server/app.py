import os

from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter

from cd.server.schema import VERSION, schema

app = FastAPI(title="cd-server")

# Off by default -- only docker-compose's dev service sets this
# (GRAPHIQL_ENABLED=true). The production image (what's actually built
# and deployed to ECS) ships with the interactive IDE disabled, since
# nothing gates /graphql behind auth yet. Query execution via POST is
# unaffected either way -- this only controls whether a browser GET
# serves the IDE.
GRAPHIQL_ENABLED = os.environ.get("GRAPHIQL_ENABLED", "false").lower() == "true"

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
