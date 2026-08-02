from transform import group_representatives


def _row(**overrides) -> dict:
    row = {
        "chamber": "SENATE",
        "member_type": "Senator",
        "given_name": "Maria",
        "middle_name": None,
        "family_name": "Cantwell",
        "nickname": None,
        "suffix": None,
        "party": "DEMOCRATIC",
        "phone": "202-224-3441",
        "website_url": "https://www.cantwell.senate.gov",
        "photo_uri": "https://bioguide.congress.gov/photo/C000127.jpg",
    }
    row.update(overrides)
    return row


def test_person_name_fields_pass_through():
    result = group_representatives([_row()])
    person = result["senators"][0]
    assert person["first_name"] == "Maria"
    assert person["middle_name"] is None
    assert person["last_name"] == "Cantwell"
    assert person["nickname"] is None
    assert person["suffix"] is None


def test_person_name_fields_include_middle_name_nickname_and_suffix_when_present():
    result = group_representatives(
        [_row(middle_name="E.", nickname="Cindy", suffix="III")]
    )
    person = result["senators"][0]
    assert person["middle_name"] == "E."
    assert person["nickname"] == "Cindy"
    assert person["suffix"] == "III"


def test_person_role_senate():
    result = group_representatives([_row(chamber="SENATE", member_type="Senator")])
    assert result["senators"][0]["role"] == "Senator"


def test_person_role_house():
    result = group_representatives([_row(chamber="HOUSE", member_type="Representative")])
    assert result["representatives"][0]["role"] == "Representative"


def test_person_role_uses_member_type_for_delegate():
    # Regression test: role used to be derived from chamber alone, which
    # mislabeled DC's Delegate / Puerto Rico's Resident Commissioner as
    # a plain "Representative". member_type already carries the correct
    # distinction, so role should just pass it through.
    result = group_representatives([_row(chamber="HOUSE", member_type="Delegate")])
    assert result["representatives"][0]["role"] == "Delegate"


def test_person_role_uses_member_type_for_resident_commissioner():
    result = group_representatives([_row(chamber="HOUSE", member_type="Resident Commissioner")])
    assert result["representatives"][0]["role"] == "Resident Commissioner"


def test_group_representatives_splits_by_chamber():
    rows = [
        _row(chamber="SENATE", family_name="Cantwell"),
        _row(chamber="SENATE", family_name="Murray"),
        _row(chamber="HOUSE", family_name="Smith"),
    ]
    result = group_representatives(rows)
    assert [p["last_name"] for p in result["senators"]] == ["Cantwell", "Murray"]
    assert [p["last_name"] for p in result["representatives"]] == ["Smith"]


def test_group_representatives_empty_house_rows():
    result = group_representatives([_row(chamber="SENATE")])
    assert result["representatives"] == []
