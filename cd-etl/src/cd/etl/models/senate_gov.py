"""Pydantic models for senate.gov's roll-call vote XML feed, as parsed by
`cd.etl.clients.senate_gov`."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class SenateVoteMenuItem(BaseModel):
    vote_number: int
    # None for an en-bloc vote (a single roll call covering several
    # matters, each with its own <issue>) -- those are always nomination
    # groups in practice, so the DAG drops a null-issue item anyway.
    issue: str | None = None


class SenateVoteDocument(BaseModel):
    # All optional: an amendment or procedural vote can leave every field
    # blank. document_number is a string (Congress.gov bill numbers are
    # ints, but keep the raw form and let the DAG int() it).
    document_type: str | None = None
    document_number: str | None = None
    document_congress: int | None = None


class SenateVoteAmendment(BaseModel):
    # senate.gov gives the amended bill as one display string
    # ("H.R. 4"), not a type/number pair -- see parse_bill_reference().
    amendment_to_document_number: str | None = None


class SenateMemberVote(BaseModel):
    lis_member_id: str
    vote_cast: str  # "Yea" / "Nay" / "Present" / "Not Voting"


class SenateVoteDetail(BaseModel):
    congress: int
    session: int
    vote_number: int
    vote_date: date
    question: str  # "On Passage of the Bill", "On the Motion", ...
    result: str  # "Bill Passed", "Motion Agreed to", ...
    document: SenateVoteDocument
    amendment: SenateVoteAmendment
    member_votes: list[SenateMemberVote]
