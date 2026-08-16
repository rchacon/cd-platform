import os
from pathlib import Path

import strawberry
import strawberry.experimental.pydantic as strawberry_pydantic
from cd.lib.models import Member, MembersResponse
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

# One client, reused across requests -- same singleton-at-import-time
# pattern cd-etl's congress_api.py already uses for its own HTTP session.
# get()/aclose() are both async (see clients.py); app.py's lifespan
# closes this on shutdown.
api_client = get_api_client()


# Derived from cd-lib's shared Member model (also used by cd-api itself
# to build its own response) rather than hand-rolled -- also carries over
# each field's Field(description=...) into the generated GraphQL schema
# for free. from_pydantic() below is what strawberry_pydantic.type
# generates for converting a validated Member into this GraphQL type.
@strawberry_pydantic.type(model=Member, all_fields=True)
class Representative:
    pass


# `role` deliberately omitted (not just hidden -- absent from the
# generated schema entirely) rather than all_fields=True: it's how
# cd-api's own /members response distinguishes Representative from
# Delegate/Resident Commissioner within the House chamber, but every
# Senator's role is always "Senator" -- redundant with getSenators
# itself, and a GraphQL client shouldn't need to know both roles come
# from the same underlying Member/table to make sense of the field.
@strawberry_pydantic.type(model=Member)
class Senator:
    first_name: strawberry.auto
    middle_name: strawberry.auto
    last_name: strawberry.auto
    nickname: strawberry.auto
    suffix: strawberry.auto
    party: strawberry.auto
    phone: strawberry.auto
    website: strawberry.auto
    photo_url: strawberry.auto


@strawberry.type
class Query:
    @strawberry.field
    def version(self) -> str:
        return VERSION

    @strawberry.field
    async def get_representatives(self, state: str, district: int) -> list[Representative]:
        result = await api_client.get("/members", {"state": state, "district": str(district)})
        # MembersResponse(**result) validates cd-api's actual response
        # against the same shared model cd-api itself built it from,
        # rather than trusting the JSON shape blindly.
        members = MembersResponse(**result)
        return [Representative.from_pydantic(member) for member in members.representatives]

    @strawberry.field
    async def get_senators(self, state: str) -> list[Senator]:
        result = await api_client.get("/members", {"state": state})
        members = MembersResponse(**result)
        return [Senator.from_pydantic(member) for member in members.senators]


schema = strawberry.Schema(
    query=Query,
    extensions=[] if GRAPHIQL_ENABLED else [DisableIntrospection],
)
