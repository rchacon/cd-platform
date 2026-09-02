import asyncio

import httpx
import pytest

from cd.server.services.geocoder_service import (
    AmbiguousAddressError,
    GeocoderError,
    GeocoderService,
    InvalidAddressError,
    NoAddressMatchError,
    _extract_congressional_district,
)

# _extract_congressional_district test cases ported from cd-lookup's
# LookupDistrictTest.php (extract_congressional_district) -- same
# algorithm, same fixtures, same expected results.


def test_extract_congressional_district_finds_district_field():
    geographies = {
        "States": [{"STATE": "13", "STUSAB": "GA"}],
        "119th Congressional Districts": [{"STATE": "13", "CD119": "05"}],
    }
    assert _extract_congressional_district(geographies) == "5"


def test_extract_congressional_district_strips_leading_zero():
    geographies = {"119th Congressional Districts": [{"CD119": "05"}]}
    assert _extract_congressional_district(geographies) == "5"


def test_extract_congressional_district_at_large_district_returns_zero():
    geographies = {"119th Congressional Districts": [{"CD119": "00"}]}
    assert _extract_congressional_district(geographies) == "0"


def test_extract_congressional_district_not_pinned_to_a_specific_congress_number():
    geographies = {"116th Congressional Districts": [{"CD116": "12"}]}
    assert _extract_congressional_district(geographies) == "12"


def test_extract_congressional_district_returns_null_when_layer_absent():
    geographies = {"States": [{"STATE": "13", "STUSAB": "GA"}]}
    assert _extract_congressional_district(geographies) is None


def test_extract_congressional_district_returns_null_for_empty_geographies():
    assert _extract_congressional_district({}) is None


def test_extract_congressional_district_ignores_field_from_a_different_congress():
    geographies = {"119th Congressional Districts": [{"CD116": "05"}]}
    assert _extract_congressional_district(geographies) is None


def test_extract_congressional_district_returns_null_when_layers_disagree():
    geographies = {
        "119th Congressional Districts": [{"CD119": "05"}],
        "119th Congressional Districts (legacy)": [{"CD119": "07"}],
    }
    assert _extract_congressional_district(geographies) is None


def test_extract_congressional_district_returns_null_for_non_numeric_value():
    geographies = {"119th Congressional Districts": [{"CD119": "ZZ"}]}
    assert _extract_congressional_district(geographies) is None


def test_extract_congressional_district_passes_the_fips_delegate_code_through_raw():
    # The 98 -> 0 normalisation is deliberately in get_district() (which
    # knows the state), not here -- this stays a faithful port of
    # cd-lookup's extractor. See cd-platform#72.
    geographies = {"119th Congressional Districts": [{"CD119": "98"}]}
    assert _extract_congressional_district(geographies) == "98"


def test_extract_congressional_district_returns_null_for_falsy_first_entry():
    geographies = {"119th Congressional Districts": [None]}
    assert _extract_congressional_district(geographies) is None


def test_no_address_match_error_is_an_invalid_address_error():
    assert issubclass(NoAddressMatchError, InvalidAddressError)


def test_ambiguous_address_error_is_an_invalid_address_error():
    assert issubclass(AmbiguousAddressError, InvalidAddressError)


# GeocoderService.get_district() -- mocked HTTP-level tests.


def _fake_response(payload, status_code=200):
    async def fake_get(self, url, params=None, timeout=None):
        return httpx.Response(status_code, json=payload, request=httpx.Request("GET", url))

    return fake_get


def _match(state="CA", cd_field="CD119", district="11", layer="119th Congressional Districts"):
    return {
        "addressComponents": {"state": state},
        "geographies": {layer: [{cd_field: district}]},
    }


def test_get_district_returns_state_and_district_on_success(monkeypatch):
    payload = {"result": {"addressMatches": [_match()]}}
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response(payload))
    service = GeocoderService()
    assert asyncio.run(
        service.get_district("1 Dr Carlton B Goodlett Pl, San Francisco, CA")
    ) == ("CA", 11)


@pytest.mark.parametrize("territory", ["DC", "PR", "GU", "VI", "AS", "MP"])
def test_get_district_normalises_fips_delegate_code_to_at_large_for_territories(
    monkeypatch, territory
):
    # Census reports CD119 "98" (FIPS nonvoting-delegate code) for the six
    # delegate jurisdictions; cd-api serves them at district 0, so a
    # getDistrict -> getRepresentatives chain needs the normalisation
    # (cd-platform#72).
    payload = {"result": {"addressMatches": [_match(state=territory, district="98")]}}
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response(payload))
    assert asyncio.run(GeocoderService().get_district("1600 Pennsylvania Ave NW")) == (
        territory,
        0,
    )


def test_get_district_leaves_98_alone_for_a_non_delegate_state(monkeypatch):
    # The normalisation is scoped to NON_VOTING_TERRITORIES on purpose: a
    # regular state reporting 98 is an anomaly that should surface, not be
    # silently rewritten to an at-large seat.
    payload = {"result": {"addressMatches": [_match(state="WY", district="98")]}}
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response(payload))
    assert asyncio.run(GeocoderService().get_district("some address")) == ("WY", 98)


def test_get_district_raises_no_match_error_on_zero_matches(monkeypatch):
    payload = {"result": {"addressMatches": []}}
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response(payload))
    with pytest.raises(NoAddressMatchError):
        asyncio.run(GeocoderService().get_district("nonsense"))


def test_get_district_raises_ambiguous_error_on_multiple_matches(monkeypatch):
    payload = {"result": {"addressMatches": [_match(), _match(state="CA", district="12")]}}
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response(payload))
    with pytest.raises(AmbiguousAddressError):
        asyncio.run(GeocoderService().get_district("Main St"))


def test_get_district_raises_geocoder_error_on_missing_state(monkeypatch):
    payload = {
        "result": {
            "addressMatches": [
                {
                    "addressComponents": {},
                    "geographies": {"119th Congressional Districts": [{"CD119": "11"}]},
                }
            ]
        }
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response(payload))
    with pytest.raises(GeocoderError, match="addressComponents.state"):
        asyncio.run(GeocoderService().get_district("some address"))


def test_get_district_raises_geocoder_error_on_null_address_components(monkeypatch):
    payload = {
        "result": {
            "addressMatches": [
                {
                    "addressComponents": None,
                    "geographies": {"119th Congressional Districts": [{"CD119": "11"}]},
                }
            ]
        }
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response(payload))
    with pytest.raises(GeocoderError, match="addressComponents.state"):
        asyncio.run(GeocoderService().get_district("some address"))


def test_get_district_raises_geocoder_error_on_missing_district(monkeypatch):
    payload = {
        "result": {
            "addressMatches": [{"addressComponents": {"state": "CA"}, "geographies": {}}]
        }
    }
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response(payload))
    with pytest.raises(GeocoderError, match="Congressional Districts"):
        asyncio.run(GeocoderService().get_district("some address"))


def test_get_district_raises_geocoder_error_on_http_error_status(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response({}, status_code=503))
    with pytest.raises(GeocoderError, match="503"):
        asyncio.run(GeocoderService().get_district("some address"))


def test_get_district_raises_geocoder_error_on_connection_failure(monkeypatch):
    async def fake_get(self, url, params=None, timeout=None):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(GeocoderError, match="Failed to reach"):
        asyncio.run(GeocoderService().get_district("some address"))


def test_get_district_raises_geocoder_error_on_malformed_response(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_response({"unexpected": "shape"}))
    with pytest.raises(GeocoderError, match="unexpected response"):
        asyncio.run(GeocoderService().get_district("some address"))


def test_geocoder_service_aclose_closes_underlying_client():
    service = GeocoderService()
    assert service._client.is_closed is False
    asyncio.run(service.aclose())
    assert service._client.is_closed is True
