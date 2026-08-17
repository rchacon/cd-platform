from cd.lib.apportionment import NON_VOTING_TERRITORIES, SEATS_PER_STATE


def test_seats_per_state_covers_all_50_states_and_sums_to_435():
    # The main real risk with 50 hand-transcribed numbers is a typo --
    # this would catch one without needing to know which state was wrong.
    states = {k: v for k, v in SEATS_PER_STATE.items() if k not in NON_VOTING_TERRITORIES}
    assert len(states) == 50
    assert sum(states.values()) == 435


def test_seats_per_state_includes_non_voting_delegate_territories():
    assert NON_VOTING_TERRITORIES <= SEATS_PER_STATE.keys()
    assert all(SEATS_PER_STATE[t] == 1 for t in NON_VOTING_TERRITORIES)
