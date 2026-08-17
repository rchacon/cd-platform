from typing import NamedTuple

from cd.lib.apportionment import NON_VOTING_TERRITORIES, SEATS_PER_STATE

# Maps a USPS state/territory abbreviation (as returned by the Census
# geocoder's addressComponents.state, e.g. "GA") to its full display name
# (e.g. "Georgia"). The Census geocoder never spells the name out, even
# when the input address did, so this hardcoded table is the only way to
# get a full name for display -- ported from cd-lookup's StateNames.php
# (cd-lookup#15), same table, same reasoning.
STATE_NAMES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia", "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
    "GU": "Guam", "AS": "American Samoa", "MP": "Northern Mariana Islands",
}


class StateInfo(NamedTuple):
    name: str
    seats: int
    voting_seats: bool


class StatesService:
    # No I/O, unlike CdApiService/GeocoderService -- kept as a service
    # anyway for consistency (schema.py depends on a uniform services
    # layer regardless of whether an implementation happens to be static
    # today; if getStates ever needs to become dynamic, this is the one
    # place that'd change).
    def get_states(self) -> dict[str, StateInfo]:
        return {
            abbr: StateInfo(
                name=name,
                seats=SEATS_PER_STATE[abbr],
                voting_seats=abbr not in NON_VOTING_TERRITORIES,
            )
            for abbr, name in STATE_NAMES.items()
        }
