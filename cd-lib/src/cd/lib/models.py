from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Member(BaseModel):
    # Guards against cd-api's transform.py (_person()) growing a field
    # this model doesn't know about -- response validation then fails
    # loudly (a 500 via cd-api's catch-all Exception handler) instead of
    # the exported spec silently drifting out of sync with the real
    # response shape again.
    model_config = ConfigDict(extra="forbid")

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


class MembersResponse(BaseModel):
    senators: list[Member]
    representatives: list[Member]
