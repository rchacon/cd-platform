from __future__ import annotations

# SEATS_PER_STATE moved to cd-lib -- cd-server's getStates GraphQL field
# also needs it (to expose seat counts/voting status per state), so it's
# shared rather than duplicated. See cd-lib/README.md.
from cd.lib.apportionment import SEATS_PER_STATE


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
