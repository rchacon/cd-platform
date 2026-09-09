import re

import httpx
from cd.lib.apportionment import normalize_district

CENSUS_GEOCODER_ENDPOINT = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
CENSUS_GEOCODER_COORDS_ENDPOINT = (
    "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
)

# The rest of this stack represents a non-voting delegate seat (DC, PR,
# GU, VI, AS, MP) as district 0. That's not a choice made here -- it comes
# straight from the Congress.gov API, whose list endpoint returns
# "district": 0 for every at-large House-side seat, delegates included;
# cd-etl stores that in member_terms.district (NULL=Senator / 0=at-large /
# 1+=numbered), and cd-api's is_valid_district validates against it.
#
# The Census geocoder is the one outlier: its "...Congressional Districts"
# layer reports the FIPS "nonvoting delegate" code 98 for those same six
# jurisdictions. Left as 98, a getDistrict -> getRepresentatives chain
# 404s for them, so get_district() below runs cd-lib's normalize_district
# (98 -> 0, scoped to those six) at this boundary -- the same helper
# cd-api applies to GET /members' filter[district] for a caller that
# geocodes for itself. See cd-platform#72.


class GeocoderError(Exception):
    """A network/unexpected-response failure talking to the Census
    geocoder itself, as opposed to a problem with the address (see
    InvalidAddressError below)."""


class InvalidAddressError(Exception):
    """A problem with the address itself (no match, or too ambiguous to
    resolve) rather than a geocoder/network failure."""


class NoAddressMatchError(InvalidAddressError):
    """The address didn't match any known location."""


class AmbiguousAddressError(InvalidAddressError):
    """The address matched more than one candidate location; the caller
    should ask for a more specific address."""


class NoLocationMatchError(InvalidAddressError):
    """A lat/lon pair that doesn't fall within any U.S. congressional
    district -- offshore, or outside the United States. The input parsed
    fine, there's just nothing there, so it's an input problem
    (InvalidAddressError), not a GeocoderError."""


# Both the layer name and its district field embed the Congress number
# (e.g. "119th Congressional Districts" / "CD119"), so match by pattern
# instead of a hardcoded Congress number that will go stale -- but require
# the field name to match the *same* layer's Congress number, rather than
# taking the first CD* field found, so a stray/legacy layer can't silently
# supply the wrong district. If multiple qualifying layers disagree on the
# district, that's an unresolvable ambiguity, not a guess. A non-numeric
# CD value is also treated as unresolvable rather than cast to 0, so a
# malformed response can't masquerade as an at-large district. Ported
# from cd-lookup's LookupDistrict.php (extract_congressional_district),
# same algorithm, same test cases. Module-level, not a method -- doesn't
# need any GeocoderService instance state.
_LEADING_DIGITS_RE = re.compile(r"^(\d+)")


def _extract_congressional_district(geographies: dict) -> str | None:
    district: str | None = None

    for layer_name, entries in geographies.items():
        if "Congressional Districts" not in layer_name or not entries or not entries[0]:
            continue

        match = _LEADING_DIGITS_RE.match(layer_name)
        if not match:
            continue

        field = f"CD{match.group(1)}"
        if field not in entries[0]:
            continue

        try:
            found = str(int(entries[0][field]))
        except (TypeError, ValueError):
            return None

        if district is not None and district != found:
            return None

        district = found

    return district


class GeocoderService:
    def __init__(self):
        # One client, reused across calls -- same connection-pool-reuse
        # rationale as HttpApiClient in cd_api_service.py. Closed via
        # aclose(), called from app.py's lifespan on shutdown.
        self._client = httpx.AsyncClient()

    async def get_district(self, address: str) -> tuple[str, int]:
        """Resolve a free-text address to (state abbreviation, district
        number) via the Census Bureau's geocoding API. Raises
        NoAddressMatchError/AmbiguousAddressError for a problem with the
        address itself, GeocoderError for anything else (network failure,
        unexpected response shape)."""
        try:
            response = await self._client.get(
                CENSUS_GEOCODER_ENDPOINT,
                params={
                    "address": address,
                    "benchmark": "Public_AR_Current",
                    "vintage": "Current_Current",
                    "format": "json",
                },
                timeout=10,
            )
        except httpx.HTTPError as e:
            raise GeocoderError(f"Failed to reach the Census geocoder: {e}") from e

        if response.is_error:
            raise GeocoderError(f"Census geocoder returned HTTP {response.status_code}")

        try:
            matches = response.json()["result"]["addressMatches"]
        except (ValueError, KeyError, TypeError) as e:
            raise GeocoderError("Census geocoder returned an unexpected response") from e

        if len(matches) == 0:
            raise NoAddressMatchError(f'No address match found for "{address}"')
        if len(matches) > 1:
            raise AmbiguousAddressError(
                f'Multiple possible matches found for "{address}"; please provide a more specific address'
            )

        match = matches[0]
        state = (match.get("addressComponents") or {}).get("state")
        district = _extract_congressional_district(match.get("geographies") or {})

        if state is None:
            raise GeocoderError("Census geocoder response was missing addressComponents.state")
        if district is None:
            raise GeocoderError(
                "Census geocoder response was missing a Congressional Districts geography"
            )

        return state, normalize_district(state, int(district))

    async def get_district_for_coords(
        self, latitude: float, longitude: float
    ) -> tuple[str, int]:
        """Resolve a lat/lon pair to (state abbreviation, district number)
        via the Census Bureau's coordinate geocoding API -- the "use my
        location" path, where there's no address to type. Same 98 -> 0
        delegate normalisation and same failure style as get_district();
        raises NoLocationMatchError when the point is outside every U.S.
        congressional district, GeocoderError for a network/response-shape
        failure."""
        try:
            response = await self._client.get(
                CENSUS_GEOCODER_COORDS_ENDPOINT,
                params={
                    # The Census geocoder takes x=longitude, y=latitude.
                    "x": longitude,
                    "y": latitude,
                    "benchmark": "Public_AR_Current",
                    "vintage": "Current_Current",
                    "format": "json",
                },
                timeout=10,
            )
        except httpx.HTTPError as e:
            raise GeocoderError(f"Failed to reach the Census geocoder: {e}") from e

        if response.is_error:
            raise GeocoderError(f"Census geocoder returned HTTP {response.status_code}")

        try:
            geographies = response.json()["result"]["geographies"]
        except (ValueError, KeyError, TypeError) as e:
            raise GeocoderError("Census geocoder returned an unexpected response") from e

        # `geographies` should be an object. A JSON `null` still subscripts
        # fine above (it's `result.geographies`, present but null), and any
        # non-object value would turn the `.get()`/`.items()` calls below
        # into an uncaught AttributeError -- so coerce `null` to `{}` (the
        # same defence get_district() uses: `match.get("geographies") or
        # {}`) and reject any other non-object shape as a response-shape
        # failure.
        geographies = geographies or {}
        if not isinstance(geographies, dict):
            raise GeocoderError("Census geocoder returned an unexpected response")

        # A point in the ocean or outside the US comes back HTTP 200 with
        # every geography layer empty -- not a geocoder fault, just nothing
        # there. `_extract_congressional_district` already returns None for
        # a missing/empty Congressional Districts layer; the States layer
        # (STUSAB, the 2-letter abbreviation) is the coordinate path's
        # equivalent of get_district's addressComponents.state.
        states = geographies.get("States") or []
        state = states[0].get("STUSAB") if states and states[0] else None
        district = _extract_congressional_district(geographies)
        if state is None or district is None:
            raise NoLocationMatchError(
                f"({latitude}, {longitude}) is not inside a U.S. congressional district"
            )

        return state, normalize_district(state, int(district))

    async def aclose(self) -> None:
        await self._client.aclose()
