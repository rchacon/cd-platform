from cd.api.apportionment import is_valid_district, max_valid_district

# SEATS_PER_STATE's own data (50-state coverage, non-voting territories)
# is tested in cd-lib/tests/test_apportionment.py, where the data now
# lives -- this file only tests the validation logic built on top of it.


def test_max_valid_district_unknown_state_returns_none():
    assert max_valid_district("ZZ") is None


def test_is_valid_district_at_large_state_only_accepts_zero():
    assert is_valid_district("WY", 0) is True
    assert is_valid_district("WY", 1) is False


def test_is_valid_district_multi_seat_state_accepts_range_and_rejects_zero():
    assert is_valid_district("GA", 1) is True
    assert is_valid_district("GA", 14) is True
    assert is_valid_district("GA", 0) is False
    assert is_valid_district("GA", 15) is False


def test_is_valid_district_unknown_state_defers_to_existing_state_check():
    # Not this module's job to reject an unrecognized state -- app.py's
    # existing "no data found for state X" path already handles that.
    assert is_valid_district("ZZ", 99) is True
