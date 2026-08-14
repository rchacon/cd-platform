from cd.api.apportionment import SEATS_PER_STATE, is_valid_district, max_valid_district

TERRITORIES = {"AS", "DC", "GU", "MP", "PR", "VI"}


def test_seats_per_state_covers_all_50_states_and_sums_to_435():
    # The main real risk with 50 hand-transcribed numbers is a typo --
    # this would catch one without needing to know which state was wrong.
    states = {k: v for k, v in SEATS_PER_STATE.items() if k not in TERRITORIES}
    assert len(states) == 50
    assert sum(states.values()) == 435


def test_seats_per_state_includes_non_voting_delegate_territories():
    assert TERRITORIES <= SEATS_PER_STATE.keys()
    assert all(SEATS_PER_STATE[t] == 1 for t in TERRITORIES)


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
