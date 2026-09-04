"""Reads the Senate's public roll-call vote XML feed.

api.congress.gov has no Senate roll-call vote endpoint at all (only
`HouseRollCallVoteEndpoint`), so Senate votes come from senate.gov's own
static XML, no API key required (rchacon/cd-platform#8):

  - a per-session *menu* listing every vote's number + `<issue>` string
    (e.g. "S. 1071", "PN373"), used to pre-filter before fetching detail;
  - a per-vote *detail* document with the `<document>` block (the bill
    the vote is on) and every senator's cast keyed by `<lis_member_id>`.

This module is the source adapter only -- fetch + parse into models, plus
the pure helpers that interpret senate.gov's display-form bill strings
("S.J.Res." etc.) against cd-etl's `bill_type` enum. Deciding *which*
votes to keep (bill-referencing only, matching house_votes_etl's scope --
nominations and treaties are dropped) and the LIS -> bioguide crosswalk
live in the DAG (senate_votes_etl).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from xml.etree import ElementTree as ET

import requests
from pydantic import BaseModel

logger = logging.getLogger(__name__)

SENATE_VOTE_MENU_URL = (
    "https://www.senate.gov/legislative/LIS/roll_call_lists/"
    "vote_menu_{congress}_{session}.xml"
)
SENATE_VOTE_DETAIL_URL = (
    "https://www.senate.gov/legislative/LIS/roll_call_votes/"
    "vote{congress}{session}/vote_{congress}_{session}_{vote_number:05d}.xml"
)

# senate.gov spells a document type as a dotted display form; cd-etl's
# `bill_type` enum (migration 0002) uses the bare code. Only the eight
# bill types Congress.gov's /bill endpoint actually serves are mapped --
# a "PN" (nomination), "Treaty Doc.", "S.Amdt."/"H.Amdt." or any
# unmapped/blank type resolves to None and the DAG skips that vote.
_DISPLAY_FORM_TO_BILL_TYPE = {
    "s.": "S",
    "h.r.": "HR",
    "s.j.res.": "SJRES",
    "h.j.res.": "HJRES",
    "s.con.res.": "SCONRES",
    "h.con.res.": "HCONRES",
    "s.res.": "SRES",
    "h.res.": "HRES",
}

# "December 17, 2025,  11:39 AM" (double space is real); older rows can
# omit the time. Whitespace is collapsed before matching either.
_VOTE_DATE_FORMATS = ("%B %d, %Y, %I:%M %p", "%B %d, %Y")

# "H.R. 4" / "S.J.Res. 82" -> ("H.R.", "4") / ("S.J.Res.", "82").
_BILL_REFERENCE_RE = re.compile(r"^\s*([A-Za-z.]+)\s*(\d+)\s*$")


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


def bill_type_for(display_form: str | None) -> str | None:
    """Map a senate.gov document-type display form onto cd-etl's
    `bill_type` enum, or None for a type that isn't a Congress.gov bill
    (nomination, treaty, amendment, blank)."""
    if not display_form:
        return None
    return _DISPLAY_FORM_TO_BILL_TYPE.get(display_form.strip().lower())


def parse_bill_reference(text: str | None) -> tuple[str, int] | None:
    """Parse a combined bill display string ("H.R. 4") into
    `(bill_type_enum, number)` -- e.g. `("HR", 4)`. None if it doesn't
    look like `<type> <number>` or the type isn't a known bill type."""
    if not text:
        return None
    match = _BILL_REFERENCE_RE.match(text)
    if match is None:
        return None
    bill_type = bill_type_for(match.group(1))
    if bill_type is None:
        return None
    return bill_type, int(match.group(2))


def _parse_vote_date(raw: str) -> date:
    collapsed = " ".join(raw.split())
    for fmt in _VOTE_DATE_FORMATS:
        try:
            return datetime.strptime(collapsed, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized Senate vote_date format: {raw!r}")


def _text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    child = element.find(path)
    if child is None or child.text is None:
        return None
    stripped = child.text.strip()
    return stripped or None


def parse_vote_menu(xml: str | bytes) -> list[SenateVoteMenuItem]:
    """Parse a `vote_menu_{congress}_{session}.xml` document into its
    vote list. A row whose `<vote_number>` can't be parsed is skipped
    (logged), not fatal -- the menu is a ~1300-row bulk list."""
    root = ET.fromstring(xml)
    items: list[SenateVoteMenuItem] = []
    for vote in root.findall("votes/vote"):
        raw_number = _text(vote, "vote_number")
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            logger.warning("Skipping Senate vote menu row with bad vote_number %r", raw_number)
            continue
        items.append(SenateVoteMenuItem(vote_number=number, issue=_text(vote, "issue")))
    return items


def parse_vote_detail(xml: str | bytes) -> SenateVoteDetail:
    """Parse a per-vote `vote_{congress}_{session}_{nnnnn}.xml` document."""
    root = ET.fromstring(xml)
    document = root.find("document")
    amendment = root.find("amendment")

    raw_doc_congress = _text(document, "document_congress")
    member_votes = [
        SenateMemberVote(
            lis_member_id=_text(member, "lis_member_id") or "",
            vote_cast=_text(member, "vote_cast") or "",
        )
        for member in root.findall("members/member")
    ]

    return SenateVoteDetail(
        congress=int(_text(root, "congress")),
        session=int(_text(root, "session")),
        vote_number=int(_text(root, "vote_number")),
        vote_date=_parse_vote_date(_text(root, "vote_date") or ""),
        question=_text(root, "question") or "",
        result=_text(root, "vote_result") or "",
        document=SenateVoteDocument(
            document_type=_text(document, "document_type"),
            document_number=_text(document, "document_number"),
            document_congress=int(raw_doc_congress) if raw_doc_congress else None,
        ),
        amendment=SenateVoteAmendment(
            amendment_to_document_number=_text(amendment, "amendment_to_document_number"),
        ),
        member_votes=member_votes,
    )


def fetch_vote_menu(
    session: requests.Session, congress: int, session_number: int
) -> list[SenateVoteMenuItem]:
    response = session.get(
        SENATE_VOTE_MENU_URL.format(congress=congress, session=session_number), timeout=30
    )
    response.raise_for_status()
    return parse_vote_menu(response.content)


def fetch_vote_detail(
    session: requests.Session, congress: int, session_number: int, vote_number: int
) -> SenateVoteDetail:
    response = session.get(
        SENATE_VOTE_DETAIL_URL.format(
            congress=congress, session=session_number, vote_number=vote_number
        ),
        timeout=30,
    )
    response.raise_for_status()
    return parse_vote_detail(response.content)
