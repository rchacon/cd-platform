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


class Member(BaseModel):
    bioguide_id: str = Field(description="Congress.gov's stable identifier for this member.")
    first_name: str | None = Field(None, description="Given name.")
    middle_name: str | None = None
    last_name: str | None = Field(None, description="Family name.")
    nickname: str | None = None
    suffix: str | None = None
    role: str = Field(
        description=(
            'Congress.gov\'s member_type for this seat -- "Senator", '
            '"Representative", "Delegate" for a non-voting territory seat '
            '(DC, American Samoa, Guam, Northern Mariana Islands, or the '
            'US Virgin Islands), or "Resident Commissioner" specifically '
            "for Puerto Rico's non-voting seat."
        )
    )
    party: str | None = None
    phone: str | None = None
    website: str | None = None
    photo_url: str | None = None
    district: int | None = Field(
        None,
        description=(
            "House district number -- 0 for an at-large seat, 1+ for a "
            "numbered district, None for a Senator."
        ),
    )


class MembersResponse(BaseModel):
    senators: list[Member]
    representatives: list[Member]


class MemberDetail(Member):
    # A superset of Member for GET /members/{bioguide_id}: that endpoint
    # can afford to carry `state` (a single member, not a list already
    # scoped to one state). Kept separate from Member rather than adding
    # a nullable `state` there, so GET /members' shape -- and every
    # consumer validating it against Member -- is untouched.
    state: str = Field(
        description="2-letter USPS state/territory code for this member's seat."
    )


class BillVote(BaseModel):
    vote_cast: str = Field(description="YEA, NAY, PRESENT, or NOT_VOTING.")
    vote_question: str
    result: str
    vote_date: date


class Bill(BaseModel):
    id: str = Field(
        description=(
            'Canonical bill id -- "<congress>-<bill_type lowercased>-'
            '<bill_number>", e.g. "119-hr-2616". A stable handle a caller '
            "reads here and passes back verbatim to a later request "
            "(e.g. GET /members/{bioguide_id}/votes); sourced from the "
            "bills.bill_key generated column, not the internal bill_id."
        )
    )
    congress: int
    bill_type: str
    bill_number: int
    title: str | None = None
    policy_area: str | None = None
    crs_summary: str | None = None
    votes: list[BillVote] = Field(
        default_factory=list,
        description=(
            "A list, not a single nullable vote -- one bill can have more "
            "than one roll call in a member's own chamber (e.g. a "
            "procedural vote plus final passage). Empty if the bill "
            "matched the search but this representative never voted on it."
        ),
    )


class BillSearchResponse(BaseModel):
    query: str
    bioguide_id: str
    bills: list[Bill]
