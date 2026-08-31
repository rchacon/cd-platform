from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query

from cd.api.db import fetch_current_members, fetch_member, fetch_member_votes
from cd.api.jsonapi import JsonApiResponse, JsonApiRoute
from cd.api.openapi import error_response, jsonapi_error_response
from cd.api.transform import group_representatives, member_document, shape_member_votes
# SEATS_PER_STATE lives in cd-lib -- cd-server's getStates also needs the
# seat counts. Only the validation built on top of it is cd-api's, and
# with GET /members its only caller it lives here. Pull it into a
# validation/ package if more request-validation helpers accrue.
from cd.lib.apportionment import SEATS_PER_STATE
from cd.lib.jsonapi import CollectionDocument, Document
from cd.lib.models import MemberDetail, MembersResponse, RollCallVote

# GET /members (the bespoke {senators, representatives} list) stays on
# this plain router. The two resource endpoints below speak JSON:API and
# live on their own router: JsonApiRoute adds the spec's request-side
# strictness (400 on unsupported params, 415/406 on a parametrized media
# type) and renders errors as JSON:API documents, and JsonApiResponse
# serves `application/vnd.api+json`.
router = APIRouter(tags=["members"])
jsonapi_router = APIRouter(route_class=JsonApiRoute, tags=["members"])

# Upper bound on how many bills GET /members/{bioguide_id}/votes'
# filter[bill] will accept in one call -- a real search response is
# `limit`-capped (<=50), so this is generous headroom, not a tight fit.
MAX_VOTE_BILLS = 50

# The query key for the votes filter: a JSON:API relationship filter
# naming the `bill` relationship the roll_call_vote resource carries
# directly. (JSON:API is agnostic about filter strategy, so the key just
# has to be a valid member name -- `filter[roll_call.bill]`, the
# traversal path, would be equally compliant; `filter[bill]` matches a
# relationship the resource actually declares.)
_BILL_FILTER_KEY = "filter[bill]"

# A canonical bill id: "<congress>-<bill_type lowercased>-<bill_number>",
# e.g. "119-hr-2616" or "119-hjres-5". Matches the shape of the
# bills.bill_key generated column (cd-etl migration 0006). A syntactically
# valid id that names no synced bill is not an error (it's just omitted
# from the response); only a malformed one is a 400.
_BILL_KEY_RE = re.compile(r"^[0-9]+-[a-z]+-[0-9]+$")


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


@jsonapi_router.get(
    "/members/{bioguide_id}",
    response_model=Document[MemberDetail],
    response_class=JsonApiResponse,
    # This resource carries no `relationships`; exclude_none drops the
    # wrapper's default `None` so the response omits the member rather
    # than emitting `"relationships": null` (invalid per JSON:API). It
    # also omits any null optional attribute (`middle_name` etc.) --
    # absent, not null, which JSON:API treats the same.
    response_model_exclude_none=True,
    responses={
        404: jsonapi_error_response(
            "No member of the current Congress has this bioguide_id. A "
            "member who left the current Congress mid-term is still "
            "returned (200, `in_office: false`), not 404."
        ),
        405: jsonapi_error_response("HTTP method not allowed for this path."),
        406: jsonapi_error_response(
            "`Accept` offers the JSON:API media type only with a media-type parameter other than `profile`/`ext`."
        ),
        415: jsonapi_error_response(
            "`Content-Type` is the JSON:API media type with a media-type parameter other than `profile`/`ext`."
        ),
        # No path-param constraints make a 422 practically unreachable, but
        # declaring it keeps FastAPI from auto-generating its own
        # HTTPValidationError-shaped one (see the other routes). An
        # unsupported query parameter is a 400 (JsonApiRoute).
        400: jsonapi_error_response("An unsupported query parameter was sent."),
        422: jsonapi_error_response("Request parameters failed validation."),
        500: jsonapi_error_response("An unexpected error occurred."),
    },
)
def get_member(bioguide_id: str) -> dict:
    """Look up a single member of the current Congress by bioguide id.

    Serves both sitting members and those who left the current Congress
    mid-term (`in_office: false`) -- so a bookmarked page keeps resolving
    after a resignation. `404` only when the id has no current-Congress
    term at all (e.g. a member of a past Congress -- not served here).

    Returns a JSON:API single-resource document -- `{"data": {"type":
    "member", "id": "<bioguide_id>", "attributes": {...}}}` -- with
    `state` and `in_office` carried on top of the `GET /members` field
    set. The bioguide id lives on the resource, not in `attributes`.
    """
    row = fetch_member(bioguide_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"No current-Congress member with bioguide_id {bioguide_id}"
        )
    return member_document(row)


@jsonapi_router.get(
    "/members/{bioguide_id}/votes",
    response_model=CollectionDocument[RollCallVote],
    response_class=JsonApiResponse,
    responses={
        400: jsonapi_error_response(
            "A `filter[bill]` id is malformed (not "
            "`<congress>-<type>-<number>`), or an unsupported query "
            "parameter was sent."
        ),
        404: jsonapi_error_response(
            "No member of the current Congress has this bioguide_id "
            "(same rule as `GET /members/{bioguide_id}`)."
        ),
        405: jsonapi_error_response("HTTP method not allowed for this path."),
        406: jsonapi_error_response(
            "`Accept` offers the JSON:API media type only with a media-type parameter other than `profile`/`ext`."
        ),
        415: jsonapi_error_response(
            "`Content-Type` is the JSON:API media type with a media-type parameter other than `profile`/`ext`."
        ),
        422: jsonapi_error_response(
            "Request parameters failed validation (e.g. `filter[bill]` "
            f"omitted, empty, or naming more than {MAX_VOTE_BILLS} bills)."
        ),
        500: jsonapi_error_response("An unexpected error occurred."),
    },
)
def get_member_votes(
    bioguide_id: str,
    bill_filter: str = Query(
        ...,
        alias=_BILL_FILTER_KEY,
        min_length=1,
        description=(
            "Comma-separated canonical bill ids -- the bill resource "
            "`id`s from a `GET /bills/search` response, passed back "
            "verbatim, e.g. `119-hr-2616,119-s-5`. Required; 1 to "
            f"{MAX_VOTE_BILLS} ids. A JSON:API relationship filter on the "
            "`roll_call_vote` resource's `bill` relationship: it narrows "
            "this member's roll-call votes to those on the named bills."
        ),
    ),
) -> dict:
    """How one member voted on a specific set of bills.

    The companion to `GET /bills/search`: that endpoint finds bills for a
    topic, this one returns this member's roll-call votes on them as
    `roll_call_vote` resources, each with `relationships` linkage to its
    `member`, `roll_call`, and `bill` -- so a caller (e.g. cd-server) can
    group votes onto search results by `relationships.bill.data.id`.

    ```jsonc
    { "data": [
        { "type": "roll_call_vote", "id": "119-house-1-327:K000401",
          "attributes": { "vote_cast": "YEA", "vote_question": "On Passage",
                          "result": "Passed", "vote_date": "2026-05-20" },
          "relationships": {
            "member":    { "data": { "type": "member",    "id": "K000401" } },
            "roll_call": { "data": { "type": "roll_call", "id": "119-house-1-327" } },
            "bill":      { "data": { "type": "bill",      "id": "119-hr-2616" } } } } ],
      "meta": { "bills_without_votes": ["119-s-5"] } }
    ```

    Votes are ordered by requested bill, then oldest-first within a bill.
    A requested id that names a synced bill the member never had a floor
    vote on is reported in `meta.bills_without_votes` (not as a
    resource); a well-formed id for a bill cd-api hasn't synced appears
    in neither `data` nor `meta`. Malformed id -> `400`; unknown member
    -> `404`, on the same rule as the parent route.
    """
    # Order-preserving de-dupe: a repeated id must not fan out to a
    # repeated resource.
    seen: set[str] = set()
    keys = [
        k for raw in bill_filter.split(",")
        if (k := raw.strip()) and not (k in seen or seen.add(k))
    ]
    if not keys:
        raise HTTPException(
            status_code=422,
            detail=f"`{_BILL_FILTER_KEY}` must name at least one bill id.",
        )
    if len(keys) > MAX_VOTE_BILLS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"`{_BILL_FILTER_KEY}` accepts at most {MAX_VOTE_BILLS} "
                f"ids, got {len(keys)}."
            ),
        )
    malformed = [k for k in keys if not _BILL_KEY_RE.match(k)]
    if malformed:
        raise HTTPException(
            status_code=400,
            detail=f"Malformed bill id(s): {', '.join(malformed)}.",
        )

    if fetch_member(bioguide_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"No current-Congress member with bioguide_id {bioguide_id}",
        )

    rows = fetch_member_votes(bioguide_id, keys)
    return shape_member_votes(rows, bioguide_id, keys)
