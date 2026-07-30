from __future__ import annotations

# 2020 census apportionment (118th/119th Congress onward) -- fixed until
# the 2030 census. Source: U.S. Census Bureau, "Apportionment Population
# and Number of Representatives by State: 2020 Census."
SEATS_PER_STATE: dict[str, int] = {
    "AL": 7, "AK": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8, "CT": 5,
    "DE": 1, "FL": 28, "GA": 14, "HI": 2, "ID": 2, "IL": 17, "IN": 9,
    "IA": 4, "KS": 4, "KY": 6, "LA": 6, "ME": 2, "MD": 8, "MA": 9,
    "MI": 13, "MN": 8, "MS": 4, "MO": 8, "MT": 2, "NE": 3, "NV": 4,
    "NH": 2, "NJ": 12, "NM": 3, "NY": 26, "NC": 14, "ND": 1, "OH": 15,
    "OK": 5, "OR": 6, "PA": 17, "RI": 2, "SC": 7, "SD": 1, "TN": 9,
    "TX": 38, "UT": 4, "VT": 1, "VA": 11, "WA": 10, "WV": 2, "WI": 8,
    "WY": 1,
    # Non-voting delegate jurisdictions -- not part of the Census Bureau's
    # formal apportionment (constitutionally excluded), but cd-api already
    # serves these as real seats (role "Delegate"/"Resident Commissioner"),
    # each with exactly one at-large seat.
    "AS": 1, "DC": 1, "GU": 1, "MP": 1, "PR": 1, "VI": 1,
}


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
