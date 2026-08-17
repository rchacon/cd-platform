from cd.server.states import STATE_NAMES


def test_resolves_known_state_abbreviations():
    assert STATE_NAMES["GA"] == "Georgia"
    assert STATE_NAMES["CA"] == "California"
    assert STATE_NAMES["WY"] == "Wyoming"
    assert STATE_NAMES["NY"] == "New York"


def test_resolves_dc_and_territories():
    assert STATE_NAMES["DC"] == "District of Columbia"
    assert STATE_NAMES["PR"] == "Puerto Rico"
    assert STATE_NAMES["VI"] == "U.S. Virgin Islands"
    assert STATE_NAMES["GU"] == "Guam"
    assert STATE_NAMES["AS"] == "American Samoa"
    assert STATE_NAMES["MP"] == "Northern Mariana Islands"


def test_covers_all_50_states_plus_dc_and_5_territories():
    assert len(STATE_NAMES) == 56
