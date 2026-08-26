import datetime

from cd.api.search import shape_bill_search_response


def _bill_row(**overrides) -> dict:
    row = {
        "bill_id": 1,
        "congress": 119,
        "bill_type": "HR",
        "bill_number": 144,
        "title": "Dream Act",
        "policy_area": "Immigration",
        "crs_summary": "A bill about dreamers.",
    }
    row.update(overrides)
    return row


def _vote_row(**overrides) -> dict:
    row = {
        "bill_id": 1,
        "vote_cast": "YEA",
        "vote_question": "On Passage",
        "result": "Passed",
        "vote_date": datetime.date(2025, 3, 1),
    }
    row.update(overrides)
    return row


def test_shape_bill_search_response_top_level_fields():
    result = shape_bill_search_response("dreamers", "C000127", [], [])

    assert result["query"] == "dreamers"
    assert result["bioguide_id"] == "C000127"
    assert result["bills"] == []


def test_shape_bill_search_response_bill_fields_pass_through():
    result = shape_bill_search_response("dreamers", "C000127", [_bill_row()], [])

    bill = result["bills"][0]
    assert bill["congress"] == 119
    assert bill["bill_type"] == "HR"
    assert bill["bill_number"] == 144
    assert bill["title"] == "Dream Act"
    assert bill["policy_area"] == "Immigration"
    assert bill["crs_summary"] == "A bill about dreamers."


def test_shape_bill_search_response_bill_with_no_votes_gets_empty_list():
    result = shape_bill_search_response("dreamers", "C000127", [_bill_row()], [])

    assert result["bills"][0]["votes"] == []


def test_shape_bill_search_response_attaches_matching_votes_to_their_bill():
    result = shape_bill_search_response(
        "dreamers", "C000127", [_bill_row(bill_id=1)], [_vote_row(bill_id=1)],
    )

    votes = result["bills"][0]["votes"]
    assert len(votes) == 1
    assert votes[0] == {
        "vote_cast": "YEA", "vote_question": "On Passage",
        "result": "Passed", "vote_date": datetime.date(2025, 3, 1),
    }


def test_shape_bill_search_response_a_bill_can_have_multiple_votes():
    # A bill can have more than one roll call in a member's own chamber
    # (e.g. a procedural vote plus final passage) -- Bill.votes is a
    # list, not a single nullable vote, specifically for this case.
    result = shape_bill_search_response(
        "dreamers", "C000127", [_bill_row(bill_id=1)],
        [
            _vote_row(bill_id=1, vote_question="Procedural Motion"),
            _vote_row(bill_id=1, vote_question="On Passage"),
        ],
    )

    votes = result["bills"][0]["votes"]
    assert [v["vote_question"] for v in votes] == ["Procedural Motion", "On Passage"]


def test_shape_bill_search_response_votes_only_attach_to_their_own_bill():
    result = shape_bill_search_response(
        "dreamers", "C000127",
        [_bill_row(bill_id=1), _bill_row(bill_id=2, bill_number=200)],
        [_vote_row(bill_id=1)],
    )

    bills_by_number = {b["bill_number"]: b for b in result["bills"]}
    assert len(bills_by_number[144]["votes"]) == 1
    assert bills_by_number[200]["votes"] == []


def test_shape_bill_search_response_preserves_bill_row_order():
    # Order matters -- it's tier1 (vocab match) results followed by
    # tier2 (similarity) results, callers rely on this ordering surviving.
    result = shape_bill_search_response(
        "dreamers", "C000127",
        [_bill_row(bill_id=2, bill_number=200), _bill_row(bill_id=1, bill_number=144)],
        [],
    )

    assert [b["bill_number"] for b in result["bills"]] == [200, 144]
