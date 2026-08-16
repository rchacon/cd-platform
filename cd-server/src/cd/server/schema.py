import os
from pathlib import Path

import strawberry
from cd.lib.version import read_version
from strawberry.extensions import DisableIntrospection

from cd.server.clients import get_api_client

# Read once at import time, not per-request -- the VERSION file is baked
# into the image and never changes for the life of the process.
VERSION = read_version(Path(__file__).parent)

# Same flag app.py uses to gate the GraphiQL IDE -- introspection is what
# powers GraphiQL's own schema explorer/autocomplete, so the two are
# enabled and disabled together. Leaving introspection on while only
# hiding the IDE would still let any POST client walk the full schema.
GRAPHIQL_ENABLED = os.environ.get("GRAPHIQL_ENABLED", "false").lower() == "true"

# One client, reused across requests -- no per-request state (no auth
# token, no connection to hold open), same singleton-at-import-time
# pattern cd-etl's congress_api.py already uses for its own HTTP session.
api_client = get_api_client()


# Mirrors cd-api's own Person model (cd-api/src/cd/api/models.py) field
# for field -- not shared code, since cd-server receives this as plain
# JSON over HTTP/Lambda, not a live Python object; there's no Pydantic
# model instance to actually share across that boundary, just a response
# shape to agree on independently.
@strawberry.type
class Representative:
    first_name: str | None
    middle_name: str | None
    last_name: str | None
    nickname: str | None
    suffix: str | None
    role: str
    party: str | None
    phone: str | None
    website: str | None
    photo_url: str | None


@strawberry.type
class District:
    state: str
    district: int


@strawberry.type
class Query:
    @strawberry.field
    def version(self) -> str:
        return VERSION

    @strawberry.field
    def get_district(self, address: str) -> District:
        # Resolving a free-text address to a state/district (e.g. via the
        # Census Bureau's geocoding API) is a separate integration from
        # cd-api entirely -- not implemented yet, deliberately, rather
        # than guessed at without a confirmed API contract.
        raise NotImplementedError("Address-to-district lookup isn't implemented yet.")

    @strawberry.field
    def get_representatives(self, state: str, district: int) -> list[Representative]:
        result = api_client.get("/members", {"state": state, "district": str(district)})
        return [Representative(**person) for person in result["representatives"]]

    @strawberry.field
    def get_senators(self, state: str) -> list[Representative]:
        result = api_client.get("/members", {"state": state})
        return [Representative(**person) for person in result["senators"]]


schema = strawberry.Schema(
    query=Query,
    extensions=[] if GRAPHIQL_ENABLED else [DisableIntrospection],
)
