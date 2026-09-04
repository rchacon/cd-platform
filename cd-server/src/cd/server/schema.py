import os
from pathlib import Path

import strawberry
import strawberry.experimental.pydantic as strawberry_pydantic
from cd.lib.models import Member
from cd.lib.models import MemberDetail as MemberDetailModel
from cd.lib.version import read_version
from strawberry.extensions import DisableIntrospection

from cd.server.services.cd_api_service import get_cd_api_service
from cd.server.services.geocoder_service import GeocoderService
from cd.server.services.states_service import StatesService
from cd.server.services.users_service import get_users_service

# Read once at import time, not per-request -- the VERSION file is baked
# into the image and never changes for the life of the process.
VERSION = read_version(Path(__file__).parent)

# Same flag app.py uses to gate the GraphiQL IDE -- introspection is what
# powers GraphiQL's own schema explorer/autocomplete, so the two are
# enabled and disabled together. Leaving introspection on while only
# hiding the IDE would still let any POST client walk the full schema.
GRAPHIQL_ENABLED = os.environ.get("GRAPHIQL_ENABLED", "false").lower() == "true"

# One instance each, reused across requests -- same singleton-at-import-time
# pattern cd-etl's congress_api.py already uses for its own HTTP session.
# cd_api_service/geocoder_service/users_service all hold an open
# connection pool, closed via app.py's lifespan on shutdown (see each
# service's aclose()). users_service's pool can't actually be opened yet
# at this point (asyncpg.create_pool() is a coroutine, unlike
# httpx.AsyncClient()'s synchronous constructor) -- see its own
# connect(), also called from app.py's lifespan, this time on startup.
cd_api_service = get_cd_api_service()
geocoder_service = GeocoderService()
states_service = StatesService()
users_service = get_users_service()


# Derived from cd-lib's Member model (cd_api_service flattens cd-api's
# JSON:API /members collection into these) rather than hand-rolled --
# also carries over each field's Field(description=...) into the
# generated GraphQL schema for free. from_pydantic() below is what
# strawberry_pydantic.type generates for converting a validated Member
# into this GraphQL type.
@strawberry_pydantic.type(model=Member, all_fields=True)
class Representative:
    pass


# `role`/`district` deliberately omitted (not just hidden -- absent from
# the generated schema entirely) rather than all_fields=True: `role` is
# how cd-api's own /members response distinguishes Representative from
# Delegate/Resident Commissioner within the House chamber, but every
# Senator's role is always "Senator" -- redundant with getSenators
# itself. `district` is always null for a Senator (senators represent
# the whole state, not a district) -- same reasoning, not useful
# information on this type, unlike Representative where it's the whole
# point.
@strawberry_pydantic.type(model=Member)
class Senator:
    bioguide_id: strawberry.auto
    first_name: strawberry.auto
    middle_name: strawberry.auto
    last_name: strawberry.auto
    nickname: strawberry.auto
    suffix: strawberry.auto
    party: strawberry.auto
    phone: strawberry.auto
    website: strawberry.auto
    photo_url: strawberry.auto


# The `getMember` detail type -- every `Member` field plus `state` and
# `in_office` (which the list resolvers' `Representative`/`Senator` don't
# carry), for cd-webapp's deep-linkable member page. Derived from
# cd-lib's `MemberDetail` for the field descriptions, same as the two
# list types above; `bioguide_id` is added because it's the JSON:API
# resource id, not a `MemberDetail` attribute -- the resolver threads it
# in via `from_pydantic(..., extra=...)`.
@strawberry_pydantic.type(model=MemberDetailModel, all_fields=True)
class MemberDetail:
    bioguide_id: str


@strawberry.type
class State:
    abbr: str
    name: str
    seats: int
    voting_seats: bool


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
    def get_states(self) -> list[State]:
        return [
            State(
                abbr=abbr,
                name=info.name,
                seats=info.seats,
                voting_seats=info.voting_seats,
            )
            for abbr, info in states_service.get_states().items()
        ]

    @strawberry.field
    async def get_district(self, address: str) -> District:
        state, district = await geocoder_service.get_district(address)
        return District(state=state, district=district)

    @strawberry.field
    async def get_representatives(self, state: str, district: int) -> list[Representative]:
        # cd_api_service already validates cd-api's response against the
        # shared Member model and returns real Member objects -- no JSON
        # handling here, just converting to the GraphQL type.
        members = await cd_api_service.get_representatives(state, district)
        return [Representative.from_pydantic(member) for member in members]

    @strawberry.field
    async def get_senators(self, state: str) -> list[Senator]:
        members = await cd_api_service.get_senators(state)
        return [Senator.from_pydantic(member) for member in members]

    @strawberry.field
    async def get_member(self, bioguide_id: str) -> MemberDetail:
        """One member of the current Congress by bioguide id -- for a
        deep-linkable detail page. Serves a sitting *or* a departed
        member (`inOffice` distinguishes them); a bioguide id with no
        current-Congress term surfaces as a GraphQL error (cd-api 404),
        same as the other resolvers' cd-api failures.
        """
        doc = await cd_api_service.member_detail(bioguide_id)
        return MemberDetail.from_pydantic(
            doc.data.attributes, extra={"bioguide_id": doc.data.id}
        )


schema = strawberry.Schema(
    query=Query,
    extensions=[] if GRAPHIQL_ENABLED else [DisableIntrospection],
)
