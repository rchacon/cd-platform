from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class _CamelModel(BaseModel):
    # Congress.gov's JSON uses camelCase keys throughout; fields here
    # are named idiomatically in snake_case, with the alias generator
    # mapping each one onto its camelCase wire name automatically (e.g.
    # bioguide_id <-> bioguideId). populate_by_name also allows
    # constructing instances directly via the snake_case field names
    # (used by tests), not just via model_validate() against raw JSON.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PartyHistoryEntry(_CamelModel):
    party_name: str | None = None
    start_year: int | None = None
    end_year: int | None = None


class Term(_CamelModel):
    chamber: str
    congress: int
    member_type: str
    state_code: str
    district: int | None = None
    start_year: int | None = None
    end_year: int | None = None

    @model_validator(mode="after")
    def _normalize_house_district(self) -> "Term":
        # The item-level API omits "district" entirely for at-large
        # seats (unlike the list endpoint, which returns 0) -- confirmed
        # directly against the live API (e.g. M001238/McBride, at-large
        # DE: item-level omits district, list-level shows district: 0;
        # same pattern for DC/territory delegates). A missing value for
        # a House seat means at-large, not unknown. Senate seats have no
        # district at all, regardless of what the source sends.
        if self.chamber == "House of Representatives":
            if self.district is None:
                self.district = 0
        else:
            self.district = None
        return self


class Depiction(_CamelModel):
    image_url: str | None = None


class AddressInformation(_CamelModel):
    phone_number: str | None = None


class MemberSummary(_CamelModel):
    bioguide_id: str
    update_date: datetime | None = None


class MemberDetail(_CamelModel):
    bioguide_id: str
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    nick_name: str | None = None
    suffix_name: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    depiction: Depiction = Depiction()
    address_information: AddressInformation = AddressInformation()
    official_website_url: str | None = None
    party_history: list[PartyHistoryEntry] = []
    update_date: datetime | None = None
    terms: list[Term] = []


class CongressSession(_CamelModel):
    start_date: date | None = None


class CongressCurrent(_CamelModel):
    number: int
    start_year: int
    sessions: list[CongressSession] = []


class CongressCurrentResponse(_CamelModel):
    congress: CongressCurrent


class HouseVoteListItem(_CamelModel):
    roll_call_number: int
    session_number: int
    result: str

    # Kept as datetime, not date -- pydantic's exact date-only coercion
    # behavior for a full offset-aware string like
    # "2025-09-08T18:56:00-04:00" isn't something this codebase has
    # verified, so the .date() extraction happens explicitly in Python
    # instead (see house_votes_etl.py). That also preserves the vote's
    # own Eastern local calendar date rather than risking a UTC-shift
    # landing a late-evening vote on the wrong day.
    start_date: datetime

    # A vote references either a bill directly, an amendment (resolved
    # back to its bill via a separate API call), or neither (a purely
    # procedural motion, e.g. "Elected Speaker") -- never both.
    legislation_type: str | None = None
    legislation_number: str | None = None
    amendment_type: str | None = None
    amendment_number: str | None = None


class HouseVoteDetail(_CamelModel):
    # The only field this endpoint is actually fetched for -- result and
    # startDate are already present on the list item (see
    # HouseVoteListItem), so they aren't duplicated here.
    vote_question: str


class HouseVoteDetailResponse(_CamelModel):
    house_roll_call_vote: HouseVoteDetail


class HouseVoteMemberVote(_CamelModel):
    # The member-votes sub-resource uses "bioguideID" (capital ID),
    # inconsistent with the rest of the API's "bioguideId" convention and
    # with to_camel's own output for this field name -- needs an explicit
    # alias rather than the generator.
    bioguide_id: str = Field(alias="bioguideID")
    vote_cast: str


class HouseRollCallVoteMemberVotes(_CamelModel):
    # Deliberately loose (list[dict], not list[HouseVoteMemberVote]) --
    # transform() validates each cast itself, one at a time, so one
    # malformed cast can be skipped without losing every other cast in
    # the same roll call. Validating the whole list here would make that
    # per-item fault isolation impossible: one bad element would fail
    # the envelope's own model_validate and drop the entire roll call.
    results: list[dict[str, Any]] = []


class HouseVoteMemberVotesResponse(_CamelModel):
    house_roll_call_vote_member_votes: HouseRollCallVoteMemberVotes


class PolicyArea(_CamelModel):
    name: str | None = None


class BillDetail(_CamelModel):
    congress: int
    type: str
    number: str
    policy_area: PolicyArea | None = None
    update_date: datetime

    @property
    def policy_area_name(self) -> str | None:
        # Flattens the nested policy_area.name (itself nullable -- not
        # every bill has been assigned one) into a plain scalar, so
        # callers don't each need to repeat the None-check.
        return self.policy_area.name if self.policy_area else None


class BillDetailResponse(_CamelModel):
    bill: BillDetail


class LegislativeSubject(_CamelModel):
    name: str
    update_date: datetime | None = None


class BillSubjects(_CamelModel):
    legislative_subjects: list[LegislativeSubject] = []


class BillSubjectsResponse(_CamelModel):
    subjects: BillSubjects


class AmendedBill(_CamelModel):
    congress: int
    type: str
    number: str


class AmendmentDetail(_CamelModel):
    # None is a real, expected case -- not every amendment resolves to a
    # specific bill.
    amended_bill: AmendedBill | None = None


class AmendmentResponse(_CamelModel):
    amendment: AmendmentDetail
