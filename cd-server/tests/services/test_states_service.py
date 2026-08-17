from cd.server.services.states_service import STATE_NAMES, StatesService


def test_resolves_known_state_abbreviations():
    states = StatesService().get_states()
    assert states["GA"] == "Georgia"
    assert states["CA"] == "California"
    assert states["WY"] == "Wyoming"
    assert states["NY"] == "New York"


def test_resolves_dc_and_territories():
    states = StatesService().get_states()
    assert states["DC"] == "District of Columbia"
    assert states["PR"] == "Puerto Rico"
    assert states["VI"] == "U.S. Virgin Islands"
    assert states["GU"] == "Guam"
    assert states["AS"] == "American Samoa"
    assert states["MP"] == "Northern Mariana Islands"


def test_covers_all_50_states_plus_dc_and_5_territories():
    assert len(StatesService().get_states()) == 56


def test_get_states_returns_the_shared_state_names_table():
    assert StatesService().get_states() is STATE_NAMES
