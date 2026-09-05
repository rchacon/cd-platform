import os
from datetime import date, datetime
from pathlib import Path

import strawberry
import strawberry.experimental.pydantic as strawberry_pydantic
from cd.lib.models import Member
from cd.lib.models import MemberDetail as MemberDetailModel
from cd.lib.version import read_version
from strawberry.extensions import DisableIntrospection
from strawberry.scalars import JSON

from cd.server.services.ai_summary_service import AiSummaryRecord, get_ai_summary_service
from cd.server.services.bill_search_service import BillResult, BillSearchService
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
bill_search_service = BillSearchService(cd_api_service)
geocoder_service = GeocoderService()
states_service = StatesService()
users_service = get_users_service()
# Holds its own asyncpg pool against cd_customers (opened/closed via
# app.py's lifespan, same as users_service) and reuses the two services
# above as the data source generate_voting_record_summary() feeds into
# the prompt. get_ai_summary_service() constructs the Bedrock chat client
# here at import time -- so a non-"local" CD_SERVER_ENVIRONMENT with no
# BEDROCK_CHAT_MODEL_ID set fails fast at startup (see
# get_bedrock_chat_client()), the same deploy-time guard
# get_cd_api_service()/get_users_service() already apply to their own
# required config.
ai_summary_service = get_ai_summary_service(cd_api_service, bill_search_service)


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
class BillVote:
    """One cast position by the `searchBills` member on one roll call --
    from `BillSearchService`'s `VoteResult`."""

    vote_cast: str
    vote_question: str
    result: str
    vote_date: date


@strawberry.type
class Bill:
    """A bill from cd-api's `GET /bills` semantic search, merged with the
    queried member's votes on it (`searchBills`) or with `votes` empty
    (`discoverBills`) -- from `BillSearchService`'s `BillResult`.

    `matches` (why this bill surfaced -- `[{"via": "policy_area"}, ...]`)
    is passed through as `JSON` rather than a typed field: its shape is
    cd-api's to evolve (cd-platform#131/#132 add more `via` kinds and
    per-entry detail), and cd-server merges bills by `billKey`, never on
    `matches`.
    """

    bill_key: str
    congress: int
    bill_type: str
    bill_number: int
    title: str | None
    policy_area: str | None
    crs_summary: str | None
    matches: JSON
    votes: list[BillVote]


def _to_bill(result: BillResult) -> Bill:
    return Bill(
        bill_key=result.bill_key,
        congress=result.congress,
        bill_type=result.bill_type,
        bill_number=result.bill_number,
        title=result.title,
        policy_area=result.policy_area,
        crs_summary=result.crs_summary,
        matches=result.matches,
        votes=[
            BillVote(
                vote_cast=v.vote_cast,
                vote_question=v.vote_question,
                result=v.result,
                vote_date=v.vote_date,
            )
            for v in result.votes
        ],
    )


class NotAuthenticatedError(Exception):
    """Raised by an auth-gated resolver when `info.context["user_id"]` is
    None -- no `Authorization` header, or an anonymous caller. Strawberry
    surfaces it as a normal GraphQL field error; this schema doesn't mask
    resolver exception messages (`ApiClientError`'s raw text already
    reaches clients via `searchBills`/`getMember`). An actually-invalid
    token never reaches a resolver -- `app.py`'s `context_getter` turns
    that into an HTTP 401 before Strawberry runs."""


@strawberry.type
class AiSummary:
    """One stored voting-record summary -- freshly generated by
    `summarizeVotingRecord` or read back via `myAiSummaries`. Flat and
    voting-record-shaped even though storage (`ai_summaries`) is generic
    over a `kind`: `bioguideId`/`query` are pulled out of the stored
    `subject` payload, not separate columns. A polymorphic shape is a
    later problem, once a second `kind` exists."""

    id: strawberry.ID
    bioguide_id: str
    query: str
    summary: str
    created_at: datetime


def _to_ai_summary(record: AiSummaryRecord) -> AiSummary:
    # bioguide_id / query live inside the stored subject envelope
    # ({"bioguideId", "topic", "bills": [...]} for kind="voting_record"),
    # not on the record itself -- see migrations/versions/0002's docstring.
    return AiSummary(
        id=strawberry.ID(str(record.id)),
        bioguide_id=record.subject["bioguideId"],
        query=record.subject["topic"],
        summary=record.summary,
        created_at=record.created_at,
    )


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
    async def discover_bills(self, q: str, limit: int = 10) -> list[Bill]:
        """Semantic topic search over synced bills (cd-platform#9) -- the
        bills matching a free-text topic and why, with no member context.
        `limit` caps the result; cd-api may return fewer (even zero) when
        little in the corpus is relevant. A Bedrock outage surfaces as a
        GraphQL error (cd-api 503).
        """
        results = await bill_search_service.discover(q, limit)
        return [_to_bill(r) for r in results]

    @strawberry.field
    async def search_bills(
        self, bioguide_id: str, q: str, limit: int = 10
    ) -> list[Bill]:
        """`discoverBills` plus how the given member voted on each matched
        bill. A matched bill the member never voted on comes back with
        `votes: []` (not omitted). An unknown `bioguideId` surfaces as a
        GraphQL error (cd-api 404), a Bedrock outage likewise (503).
        """
        results = await bill_search_service.search(bioguide_id, q, limit)
        return [_to_bill(r) for r in results]

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

    @strawberry.field
    async def my_ai_summaries(
        self, info: strawberry.Info, limit: int = 20
    ) -> list[AiSummary]:
        """This caller's own past `summarizeVotingRecord` results, newest
        first. Requires a verified caller (`Authorization: Bearer <Cognito
        id token>`), same as the mutation -- `NotAuthenticatedError`
        otherwise. Only `kind="voting_record"` rows are returned; storage
        may hold other kinds later, but this surface doesn't expose them
        yet.
        """
        user_id = info.context["user_id"]
        if user_id is None:
            raise NotAuthenticatedError("myAiSummaries requires authentication")
        records = await ai_summary_service.history(user_id, limit, kind="voting_record")
        return [_to_ai_summary(r) for r in records]


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def summarize_voting_record(
        self, info: strawberry.Info, bioguide_id: str, q: str, limit: int = 10
    ) -> AiSummary:
        """Generate (and store) a nonpartisan AI summary of how
        `bioguideId` voted on the bills matching topic `q` -- the same
        bills+votes `searchBills(bioguideId, q, limit)` returns, fed to
        Bedrock. Field names mirror `searchBills`'.

        Requires a verified caller (`Authorization: Bearer <Cognito id
        token>`); raises `NotAuthenticatedError` otherwise. An unknown
        `bioguideId` surfaces as a cd-api 404 GraphQL error, a Bedrock
        outage as `BedrockConverseError`, a `q` over 200 chars as
        `ValueError` -- none caught here, same "let it propagate" style as
        the rest of this schema. Every call generates fresh (no dedup of
        repeat identical requests -- keeps the usage signal honest).
        """
        user_id = info.context["user_id"]
        if user_id is None:
            raise NotAuthenticatedError("summarizeVotingRecord requires authentication")
        record = await ai_summary_service.generate_voting_record_summary(
            user_id, bioguide_id, q, limit
        )
        return _to_ai_summary(record)


class _Schema(strawberry.Schema):
    """Strawberry's default `process_errors` logs every GraphQL field
    error at ERROR with a full traceback. `NotAuthenticatedError` is a
    routine "caller isn't signed in" signal, not a server fault -- an
    anonymous hit on `myAiSummaries` (e.g. cd-webapp rendering a History
    view for a logged-out visitor) shouldn't spew ERROR logs or trip
    alerts. Every other error still logs exactly as before -- an
    `ApiClientError` is a genuine upstream failure worth seeing."""

    def process_errors(self, errors, execution_context=None):
        loud = [
            e
            for e in errors
            if not isinstance(getattr(e, "original_error", None), NotAuthenticatedError)
        ]
        if loud:
            super().process_errors(loud, execution_context)


schema = _Schema(
    query=Query,
    mutation=Mutation,
    extensions=[] if GRAPHIQL_ENABLED else [DisableIntrospection],
)
