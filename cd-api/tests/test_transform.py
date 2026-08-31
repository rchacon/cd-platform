import datetime

from cd.api.transform import (
    bill_search_document,
    group_representatives,
    member_document,
    person,
    shape_member_votes,
)


def _row(**overrides) -> dict:
    row = {
        "bioguide_id": "C000127",
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
        "district": None,
    }
    row.update(overrides)
    return row


def test_person_bioguide_id_passes_through():
    result = group_representatives([_row(bioguide_id="X000001")])
    assert result["senators"][0]["bioguide_id"] == "X000001"


def test_person_district_is_null_for_senator():
    result = group_representatives([_row(chamber="SENATE", district=None)])
    assert result["senators"][0]["district"] is None


def test_person_district_passes_through_for_representative():
    result = group_representatives(
        [_row(chamber="HOUSE", member_type="Representative", district=5)]
    )
    assert result["representatives"][0]["district"] == 5


def test_person_district_zero_for_at_large_representative():
    result = group_representatives(
        [_row(chamber="HOUSE", member_type="Representative", district=0)]
    )
    assert result["representatives"][0]["district"] == 0


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


def test_person_does_not_carry_state():
    # `state` is a GET /members/{bioguide_id} (MemberDetail) addition,
    # layered on in the route -- the shared shape stays as GET /members has it.
    assert "state" not in group_representatives([_row()])["senators"][0]


def test_person_returns_exactly_the_documented_field_set():
    # cd-lib's Member is lenient (extra="ignore"), so an accidental extra
    # key here would be silently dropped from the response rather than
    # rejected -- this is the guard that a shaper change stays in sync
    # with the model / OpenAPI spec.
    assert set(person(_row())) == {
        "bioguide_id", "first_name", "middle_name", "last_name", "nickname",
        "suffix", "role", "party", "phone", "website", "photo_url", "district",
    }


def _detail_row(**overrides) -> dict:
    return _row(**{"state": "GA", "in_office": True, **overrides})


def test_member_document_is_a_jsonapi_single_resource():
    doc = member_document(_detail_row(bioguide_id="C000127"))

    assert set(doc) == {"data"}
    assert doc["data"]["type"] == "member"
    assert doc["data"]["id"] == "C000127"


def test_member_document_moves_identity_out_of_attributes():
    attributes = member_document(_detail_row())["data"]["attributes"]

    assert "bioguide_id" not in attributes
    # Exact set so a shaper change can't drift from MemberDetail / the
    # OpenAPI spec (the model is lenient and would just drop extras).
    assert set(attributes) == {
        "first_name", "middle_name", "last_name", "nickname", "suffix",
        "role", "party", "phone", "website", "photo_url", "district",
        "state", "in_office",
    }


def test_member_document_carries_state_and_in_office():
    attributes = member_document(
        _detail_row(state="TX", in_office=False)
    )["data"]["attributes"]

    assert attributes["state"] == "TX"
    assert attributes["in_office"] is False


def _mv_row(**overrides) -> dict:
    # A fetch_member_votes row: bill_key + the roll call's natural-key
    # parts + the roll call's own fields + this member's vote_cast.
    row = {
        "bill_key": "119-hr-2616",
        "chamber": "HOUSE",
        "congress": 119,
        "session": 1,
        "vote_number": 327,
        "vote_question": "On Passage",
        "result": "Passed",
        "vote_date": datetime.date(2026, 5, 20),
        "vote_cast": "YEA",
    }
    row.update(overrides)
    return row


def _no_vote_row(**overrides) -> dict:
    # What the LEFT JOIN emits for a synced bill this member never voted
    # on: the bill_key, everything else NULL.
    return _mv_row(**{
        "chamber": None, "session": None, "vote_number": None,
        "vote_question": None, "result": None, "vote_date": None,
        "vote_cast": None, **overrides,
    })


def test_shape_member_votes_empty_when_no_rows():
    assert shape_member_votes([], "K000401", ["119-hr-2616"]) == {
        "data": [], "meta": {"bills_without_votes": []},
    }


def test_shape_member_votes_builds_one_roll_call_vote_resource_per_vote():
    result = shape_member_votes([_mv_row()], "K000401", ["119-hr-2616"])

    assert result["data"] == [{
        "type": "roll_call_vote",
        "id": "119-house-1-327:K000401",
        "attributes": {
            "vote_cast": "YEA",
            "vote_question": "On Passage",
            "result": "Passed",
            "vote_date": datetime.date(2026, 5, 20),
        },
        "relationships": {
            "member": {"data": {"type": "member", "id": "K000401"}},
            "roll_call": {"data": {"type": "roll_call", "id": "119-house-1-327"}},
            "bill": {"data": {"type": "bill", "id": "119-hr-2616"}},
        },
    }]
    assert result["meta"] == {"bills_without_votes": []}


def test_shape_member_votes_synced_bill_with_no_vote_goes_to_meta_not_data():
    result = shape_member_votes(
        [_no_vote_row()], "K000401", ["119-hr-2616"]
    )

    assert result["data"] == []
    assert result["meta"]["bills_without_votes"] == ["119-hr-2616"]


def test_shape_member_votes_unsynced_bill_is_in_neither_data_nor_meta():
    # A requested key that matched no row at all -- not a synced bill.
    result = shape_member_votes([_mv_row()], "K000401", ["119-hr-2616", "119-s-9999"])

    assert [r["relationships"]["bill"]["data"]["id"] for r in result["data"]] == [
        "119-hr-2616"
    ]
    assert result["meta"]["bills_without_votes"] == []


def test_shape_member_votes_orders_by_requested_bill_then_oldest_first():
    rows = [
        # arrives already ordered by (vote_date, roll_call_id) from the query
        _mv_row(bill_key="119-s-5", chamber="SENATE", vote_number=88,
                vote_question="On the Motion", vote_date=datetime.date(2026, 3, 1)),
        _mv_row(vote_number=100, vote_question="On Motion to Recommit",
                vote_date=datetime.date(2026, 5, 19)),
        _mv_row(vote_number=101, vote_question="On Passage",
                vote_date=datetime.date(2026, 5, 20)),
    ]
    result = shape_member_votes(rows, "K000401", ["119-hr-2616", "119-s-5"])

    assert [r["attributes"]["vote_question"] for r in result["data"]] == [
        "On Motion to Recommit", "On Passage", "On the Motion",
    ]
    assert [r["relationships"]["roll_call"]["data"]["id"] for r in result["data"]] == [
        "119-house-1-100", "119-house-1-101", "119-senate-1-88",
    ]


def test_shape_member_votes_mixes_voted_and_no_vote_bills():
    rows = [_no_vote_row(bill_key="119-s-5"), _mv_row(bill_key="119-hr-2616")]
    result = shape_member_votes(rows, "K000401", ["119-hr-2616", "119-s-5"])

    assert [r["relationships"]["bill"]["data"]["id"] for r in result["data"]] == [
        "119-hr-2616"
    ]
    assert result["meta"]["bills_without_votes"] == ["119-s-5"]


def _bill_row(**overrides) -> dict:
    # A fetch_bills_by_* row after the route tags it with `match`.
    row = {
        "bill_id": 1,
        "bill_key": "119-hr-144",
        "congress": 119,
        "bill_type": "HR",
        "bill_number": 144,
        "title": "Dream Act",
        "policy_area": "Immigration",
        "crs_summary": "A bill about dreamers.",
        "match": "policy_area",
    }
    row.update(overrides)
    return row


def test_bill_search_document_is_a_jsonapi_collection_with_query_meta():
    result = bill_search_document("dreamers", [])

    assert result == {"data": [], "meta": {"query": "dreamers"}}


def test_bill_search_document_builds_one_bill_resource_per_row():
    result = bill_search_document("dreamers", [_bill_row()])

    assert result["data"][0] == {
        "type": "bill",
        "id": "119-hr-144",
        "attributes": {
            "congress": 119,
            "bill_type": "HR",
            "bill_number": 144,
            "title": "Dream Act",
            "policy_area": "Immigration",
            "crs_summary": "A bill about dreamers.",
        },
        "meta": {"match": "policy_area"},
    }


def test_bill_search_document_identity_is_the_resource_not_an_attribute():
    resource = bill_search_document("dreamers", [_bill_row()])["data"][0]
    attributes = resource["attributes"]

    assert "id" not in attributes
    assert "bill_id" not in attributes
    # `match` is per-resource meta, not an attribute of the bill.
    assert "match" not in attributes
    # Exact set so a shaper change can't drift from cd-lib's Bill / the
    # OpenAPI spec (the model is lenient and would just drop extras).
    assert set(attributes) == {
        "congress", "bill_type", "bill_number", "title",
        "policy_area", "crs_summary",
    }
    assert set(resource["meta"]) == {"match"}


def test_bill_search_document_passes_through_the_match_tier_in_meta():
    result = bill_search_document(
        "dreamers",
        [_bill_row(match="subject"), _bill_row(bill_key="119-s-9", match="similarity")],
    )

    assert [r["meta"]["match"] for r in result["data"]] == ["subject", "similarity"]


def test_bill_search_document_preserves_row_order():
    # tier-1 (vocab) rows precede tier-2 (similarity) rows -- callers
    # group on `match` but order is still the natural fallback.
    result = bill_search_document(
        "dreamers",
        [_bill_row(bill_key="119-hr-200", bill_number=200),
         _bill_row(bill_key="119-hr-144", bill_number=144)],
    )

    assert [r["id"] for r in result["data"]] == ["119-hr-200", "119-hr-144"]
