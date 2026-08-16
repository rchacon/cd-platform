from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter

from cd.server.schema import _read_version, schema

app = FastAPI(title="cd-server")

app.include_router(GraphQLRouter(schema), prefix="/graphql")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Same shape as cd-api's GET /version -- a plain REST endpoint alongside
# the GraphQL `version` query, so a version check doesn't need a GraphQL
# client/curl -X POST just to confirm what's running.
@app.get("/version")
def version() -> dict:
    return {"version": _read_version()}
