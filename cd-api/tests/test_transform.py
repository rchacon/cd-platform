from transform import _full_name, group_representatives


def _row(**overrides) -> dict:
    row = {
        "chamber": "SENATE",
        "given_name": "Maria",
        "middle_name": None,
        "family_name": "Cantwell",
        "suffix": None,
        "party": "DEMOCRATIC",
        "phone": "202-224-3441",
        "website_url": "https://www.cantwell.senate.gov",
        "photo_uri": "https://bioguide.congress.gov/photo/C000127.jpg",
    }
    row.update(overrides)
    return row


def test_full_name_omits_missing_middle_name():
    assert _full_name(_row()) == "Maria Cantwell"


def test_full_name_includes_middle_name_when_present():
    assert _full_name(_row(middle_name="E.")) == "Maria E. Cantwell"


def test_full_name_includes_suffix_when_present():
    assert _full_name(_row(suffix="III")) == "Maria Cantwell III"


def test_full_name_uses_nickname_over_given_and_middle_name_when_present():
    assert _full_name(_row(given_name="Maria", nickname="Cindy")) == "Cindy Cantwell"


def test_full_name_nickname_takes_precedence_over_suffix():
    assert _full_name(_row(nickname="Cindy", suffix="III")) == "Cindy Cantwell"


def test_person_role_senate():
    result = group_representatives([_row(chamber="SENATE")])
    assert result["senators"][0]["role"] == "Senator"


def test_person_role_house():
    result = group_representatives([_row(chamber="HOUSE")])
    assert result["representatives"][0]["role"] == "Representative"


def test_group_representatives_splits_by_chamber():
    rows = [
        _row(chamber="SENATE", family_name="Cantwell"),
        _row(chamber="SENATE", family_name="Murray"),
        _row(chamber="HOUSE", family_name="Smith"),
    ]
    result = group_representatives(rows)
    assert [p["full_name"] for p in result["senators"]] == ["Maria Cantwell", "Maria Murray"]
    assert [p["full_name"] for p in result["representatives"]] == ["Maria Smith"]


def test_group_representatives_empty_house_rows():
    result = group_representatives([_row(chamber="SENATE")])
    assert result["representatives"] == []
