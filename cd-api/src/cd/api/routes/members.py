from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from cd.api.db import fetch_current_members, fetch_member
from cd.api.openapi import error_response
from cd.api.transform import group_representatives, person
# SEATS_PER_STATE lives in cd-lib -- cd-server's getStates also needs the
# seat counts. Only the validation built on top of it is cd-api's, and
# with GET /members its only caller it lives here. Pull it into a
# validation/ package if more request-validation helpers accrue.
from cd.lib.apportionment import SEATS_PER_STATE
from cd.lib.models import MemberDetail, MembersResponse

router = APIRouter(tags=["members"])


def max_valid_district(state: str) -> int | None:
    """Highest valid district number for a state, or None if unrecognized.

    District 0 (at-large) is the only valid value for a 1-seat
    state/territory -- this isn't "district 1 through 1", it's a
    different numbering scheme entirely (see member_terms.district's
    NULL/0/1+ convention).
    """
    return SEATS_PER_STATE.get(state.upper())


def is_valid_district(state: str, district: int, seats: int | None = None) -> bool:
    """Whether `district` is valid for `state`.

    `seats` can be passed in when the caller already looked it up via
    `max_valid_district` (e.g. to build an error message), so this doesn't
    repeat the same dict lookup -- pass nothing to have it looked up here.
    """
    if seats is None:
        seats = max_valid_district(state)
    if seats is None:
        return True  # unrecognized state -- let the existing state-not-found path handle it
    return district == 0 if seats == 1 else 1 <= district <= seats


@router.get(
    "/members",
    response_model=MembersResponse,
    responses={
        404: error_response(
            "Unknown state, or a district that doesn't exist for the given state.",
            "ProblemDetail",
        ),
        405: error_response("HTTP method not allowed for this path.", "ProblemDetail"),
        422: error_response(
            "Request parameters failed validation.", "ValidationProblemDetail"
        ),
        500: error_response("An unexpected error occurred.", "ProblemDetail"),
    },
)
def get_members(
    state: str = Query(..., min_length=2, max_length=2, pattern="^[A-Za-z]{2}$"),
    district: int | None = Query(
        None,
        ge=0,
        description=(
            "Omit entirely to get senators only. `0` selects the state's "
            "single at-large House seat (only valid for 1-seat states/"
            "territories, e.g. WY, DC). `1` and above selects a specific "
            "numbered House district. There is no explicit \"null\" form -- "
            "HTTP query strings can't express it, so `district=` or "
            "`district=null` both fail validation; omission is the only "
            "way to get the senators-only behavior."
        ),
    ),
) -> dict:
    """Look up current senators and representative(s) for a state.

    Optionally scoped to one House district via `district` -- see that
    parameter's own description for its omitted/0/1+ semantics.

    `district` draws a distinction between two different kinds of "no
    representative": a district number that doesn't exist for the given
    state (validated against real House apportionment) returns `404`,
    while a district that does exist but is currently vacant (a real
    seat between office-holders) still returns `200` with an empty
    `representatives` list. `senators` is populated either way.
    """
    # Distinguishes "this district doesn't exist" from "this district
    # exists but is currently vacant" (see cd-platform#12) -- without this,
    # both cases fall through to the same 200 + empty representatives list
    # below, since current_members' query includes the state's senators
    # regardless of whether any representative matches the district.
    seats = max_valid_district(state)
    if district is not None and not is_valid_district(state, district, seats=seats):
        raise HTTPException(
            status_code=404,
            detail=(
                f"District {district} does not exist for state {state.upper()} "
                f"({seats} district{'s' if seats != 1 else ''})."
            ),
        )

    rows = fetch_current_members(state.upper(), district)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No data found for state {state.upper()}")
    return group_representatives(rows)


@router.get(
    "/members/{bioguide_id}",
    response_model=MemberDetail,
    responses={
        404: error_response(
            "No member of the current Congress has this bioguide_id. A "
            "member who left the current Congress mid-term is still "
            "returned (200, `in_office: false`), not 404.",
            "ProblemDetail",
        ),
        405: error_response("HTTP method not allowed for this path.", "ProblemDetail"),
        # No path-param constraints make a 422 practically unreachable, but
        # declaring it keeps FastAPI from auto-generating its own
        # HTTPValidationError-shaped one (see the other routes).
        422: error_response(
            "Request parameters failed validation.", "ValidationProblemDetail"
        ),
        500: error_response("An unexpected error occurred.", "ProblemDetail"),
    },
)
def get_member(bioguide_id: str) -> dict:
    """Look up a single member of the current Congress by bioguide id.

    Serves both sitting members and those who left the current Congress
    mid-term (`in_office: false`) -- so a bookmarked page keeps resolving
    after a resignation. `404` only when the id has no current-Congress
    term at all (e.g. a member of a past Congress -- not served here).
    Carries `state` and `in_office` on top of the `GET /members` shape.
    """
    row = fetch_member(bioguide_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"No current-Congress member with bioguide_id {bioguide_id}"
        )
    return {**person(row), "state": row["state"], "in_office": row["in_office"]}
