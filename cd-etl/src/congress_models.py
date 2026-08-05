from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, model_validator
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
