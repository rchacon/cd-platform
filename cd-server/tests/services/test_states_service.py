from cd.server.services.states_service import StatesService


def test_resolves_known_state_abbreviations():
    states = StatesService().get_states()
    assert states["GA"].name == "Georgia"
    assert states["CA"].name == "California"
    assert states["WY"].name == "Wyoming"
    assert states["NY"].name == "New York"


def test_resolves_dc_and_territories():
    states = StatesService().get_states()
    assert states["DC"].name == "District of Columbia"
    assert states["PR"].name == "Puerto Rico"
    assert states["VI"].name == "U.S. Virgin Islands"
    assert states["GU"].name == "Guam"
    assert states["AS"].name == "American Samoa"
    assert states["MP"].name == "Northern Mariana Islands"


def test_covers_all_50_states_plus_dc_and_5_territories():
    assert len(StatesService().get_states()) == 56


def test_voting_states_have_real_apportionment_seats():
    states = StatesService().get_states()
    assert states["CA"].seats == 52
    assert states["WY"].seats == 1
    assert states["CA"].voting_seats is True
    assert states["WY"].voting_seats is True


def test_territories_have_one_non_voting_seat():
    states = StatesService().get_states()
    for abbr in ("DC", "PR", "VI", "GU", "AS", "MP"):
        assert states[abbr].seats == 1
        assert states[abbr].voting_seats is False


def test_voting_seats_sum_to_435():
    states = StatesService().get_states()
    assert sum(info.seats for info in states.values() if info.voting_seats) == 435
