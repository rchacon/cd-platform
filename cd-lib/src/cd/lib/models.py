from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

# These models are a shared *contract*, consumed by cd-server (which
# bundles its own, independently-versioned copy of cd-lib) as well as by
# cd-api. So they follow the robustness principle: liberal in what they
# accept. Pydantic's default extra="ignore" means a field cd-api adds to
# a response never breaks a not-yet-updated consumer -- it's just dropped
# on the consumer side until that consumer's cd-lib catches up. cd-api
# keeps its own producer-side drift guard via its OpenAPI schema tests
# (test_openapi_*_documents_*_fields) and its shaper unit tests, which
# assert the exact field set -- that strictness belongs with the
# producer, not baked into the shared model.

# Shared field descriptions -- `Member` (cd-server's flattened member
# shape, `bioguide_id` in-body) and `MemberDetail` (cd-api's JSON:API
# resource attributes for the `/members` list and
# GET /members/{bioguide_id}, where `bioguide_id` is the resource `id`)
# declare their common fields independently rather than one extending the
# other (see MemberDetail's own comment), so the prose lives here once
# instead of being copy-pasted.
_ROLE_DESCRIPTION = (
    'Congress.gov\'s member_type for this seat -- "Senator", '
    '"Representative", "Delegate" for a non-voting territory seat '
    "(DC, American Samoa, Guam, Northern Mariana Islands, or the "
    "US Virgin Islands), or "
    '"Resident Commissioner" specifically for Puerto Rico\'s '
    "non-voting seat."
)
_DISTRICT_DESCRIPTION = (
    "House district number -- 0 for an at-large seat, 1+ for a "
    "numbered district, None for a Senator."
)


class Member(BaseModel):
    # cd-server-only: it flattens cd-api's JSON:API `/members` collection
    # (each `member` resource -> one of these, resource `id` -> the
    # `bioguide_id` field below) and derives its GraphQL `Representative`/
    # `Senator` types from this model. cd-api itself builds `MemberDetail`,
    # not this.
    bioguide_id: str = Field(description="Congress.gov's stable identifier for this member.")
    first_name: str | None = Field(None, description="Given name.")
    middle_name: str | None = None
    last_name: str | None = Field(None, description="Family name.")
    nickname: str | None = None
    suffix: str | None = None
    role: str = Field(description=_ROLE_DESCRIPTION)
    party: str | None = None
    phone: str | None = None
    website: str | None = None
    photo_url: str | None = None
    district: int | None = Field(None, description=_DISTRICT_DESCRIPTION)


class MemberDetail(BaseModel):
    # The `attributes` payload of cd-api's JSON:API `member` resource,
    # served by both GET /members (CollectionDocument[MemberDetail]) and
    # GET /members/{bioguide_id} (Document[MemberDetail]) -- i.e.
    # {"data": {"type": "member", "id": "<bioguide_id>", "attributes":
    # {...this model...}}}. The bioguide id is the resource `id`, so it
    # is deliberately NOT a field here.
    #
    # This does NOT extend `Member`, even though it shares eleven fields:
    # `Member` is cd-server's flattened shape and keeps `bioguide_id`
    # in-body (its GraphQL types derive from it), whereas here the id is
    # the resource `id` and absent from attributes. Keeping the two
    # independent lets each carry exactly its own field set; the shared
    # descriptions above stop the prose from drifting.
    first_name: str | None = Field(None, description="Given name.")
    middle_name: str | None = None
    last_name: str | None = Field(None, description="Family name.")
    nickname: str | None = None
    suffix: str | None = None
    role: str = Field(description=_ROLE_DESCRIPTION)
    party: str | None = None
    phone: str | None = None
    website: str | None = None
    photo_url: str | None = None
    district: int | None = Field(None, description=_DISTRICT_DESCRIPTION)
    state: str = Field(
        description="2-letter USPS state/territory code for this member's seat."
    )
    in_office: bool = Field(
        description=(
            "True while this member is serving the current Congress; "
            "false once they have left it (resigned, died, expelled) -- "
            "the endpoint still serves them so a bookmarked page keeps "
            "resolving. Always scoped to the current Congress; members of "
            "a past Congress are not served here."
        )
    )


class RollCallVote(BaseModel):
    # The `attributes` payload of each resource in
    # GET /members/{bioguide_id}/votes' collection document --
    # CollectionDocument[RollCallVote]. Each resource is one member's cast
    # position in one roll call: type "roll_call_vote", id
    # "<roll_call>:<bioguide_id>" (e.g. "119-house-1-327:K000401"), with
    # `relationships` linking to the `member` and the `roll_call`, plus a
    # derived `bill` edge (a vote reaches its bill *through* the roll
    # call, but the link is carried directly so a caller can group votes
    # by bill without the traversal).
    #
    # `vote_cast` is the only field truly owned by this join entity;
    # `vote_question`/`result`/`vote_date` are denormalised from the
    # roll call it belongs to. They're carried here because this API
    # doesn't do JSON:API compound documents (no `included`), and a
    # client rendering a vote needs them -- when a standalone `roll_call`
    # resource/endpoint lands they become authoritative there.
    vote_cast: str = Field(description="YEA, NAY, PRESENT, or NOT_VOTING.")
    vote_question: str = Field(
        description='The question put to the chamber, e.g. "On Passage".'
    )
    result: str = Field(description='Chamber-level outcome, e.g. "Passed".')
    vote_date: date


class Bill(BaseModel):
    # The `attributes` payload of each `bill` resource in GET /bills'
    # collection document -- CollectionDocument[Bill], {"data": [{"type":
    # "bill", "id": "119-hr-2616", "attributes": {...this model...},
    # "meta": {"matches": [{"via": "policy_area"}]}}, ...],
    # "meta": {"query": "..."}}.
    #
    # Only intrinsic bill data. The canonical bill id ("<congress>-<type
    # lowercased>-<number>", the bills.bill_key generated column) is the
    # resource `id`, not a field here -- a caller reads it off the
    # resource and passes it back as GET /members/{bioguide_id}/votes'
    # filter[bill]. Why the bill matched *this* search
    # (`meta.matches` -- a list of `{"via": "policy_area"|"subject"|
    # "summary"|...}`) is per-resource `meta`, not an attribute -- it has
    # no meaning outside the one response, so it must not ride on the
    # reusable model cd-server validates and merges by id.
    congress: int
    bill_type: str
    bill_number: int
    title: str | None = None
    policy_area: str | None = None
    crs_summary: str | None = None
