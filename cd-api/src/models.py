from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Person(BaseModel):
    # Guards against transform.py's _person() growing a field this model
    # doesn't know about -- response validation then fails loudly (a 500
    # via the catch-all Exception handler) instead of the exported spec
    # silently drifting out of sync with the real response shape again.
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
    senators: list[Person]
    representatives: list[Person]


class VersionResponse(BaseModel):
    version: str


class ProblemDetail(BaseModel):
    """RFC 9457 "Problem Details for HTTP APIs"."""

    type: Literal["about:blank"] = "about:blank"
    title: str
    status: int
    detail: str | None = None


class ValidationProblemDetail(ProblemDetail):
    errors: list[dict[str, Any]]


# Computed once and reused verbatim across every route's `responses=`
# declaration in app.py, so the documented error shape can't drift between
# routes and doesn't get recomputed per route.
PROBLEM_DETAIL_SCHEMA = ProblemDetail.model_json_schema()
VALIDATION_PROBLEM_DETAIL_SCHEMA = ValidationProblemDetail.model_json_schema()
