from cd.lib.apportionment import (
    NON_VOTING_TERRITORIES,
    SEATS_PER_STATE,
    normalize_district,
)


def test_seats_per_state_covers_all_50_states_and_sums_to_435():
    # The main real risk with 50 hand-transcribed numbers is a typo --
    # this would catch one without needing to know which state was wrong.
    states = {k: v for k, v in SEATS_PER_STATE.items() if k not in NON_VOTING_TERRITORIES}
    assert len(states) == 50
    assert sum(states.values()) == 435


def test_seats_per_state_includes_non_voting_delegate_territories():
    assert NON_VOTING_TERRITORIES <= SEATS_PER_STATE.keys()
    assert all(SEATS_PER_STATE[t] == 1 for t in NON_VOTING_TERRITORIES)


def test_normalize_district_maps_fips_98_to_zero_for_delegate_jurisdictions():
    # cd-platform#72: 98 is the Census FIPS nonvoting-delegate code.
    for territory in NON_VOTING_TERRITORIES:
        assert normalize_district(territory, 98) == 0
        assert normalize_district(territory.lower(), 98) == 0


def test_normalize_district_leaves_everything_else_untouched():
    assert normalize_district("DC", 0) == 0
    assert normalize_district("DC", None) is None
    assert normalize_district("WY", 98) == 98  # voting 1-seat state -- 98 is meaningless
    assert normalize_district("GA", 98) == 98
    assert normalize_district("GA", 5) == 5
