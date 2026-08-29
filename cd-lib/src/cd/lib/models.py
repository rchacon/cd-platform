from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class Member(BaseModel):
    # Guards against cd-api's transform.py (_person()) growing a field
    # this model doesn't know about -- response validation then fails
    # loudly (a 500 via cd-api's catch-all Exception handler) instead of
    # the exported spec silently drifting out of sync with the real
    # response shape again.
    model_config = ConfigDict(extra="forbid")

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


class BillVote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vote_cast: str = Field(description="YEA, NAY, PRESENT, or NOT_VOTING.")
    vote_question: str
    result: str
    vote_date: date


class Bill(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

    query: str
    bioguide_id: str
    bills: list[Bill]
