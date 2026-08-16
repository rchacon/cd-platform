from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter

from cd.server.schema import schema

app = FastAPI(title="cd-server")

app.include_router(GraphQLRouter(schema), prefix="/graphql")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
