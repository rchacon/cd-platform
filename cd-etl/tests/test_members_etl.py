from datetime import datetime, timezone

import members_etl as etl


def _kiley_party_history(order: str) -> list[dict]:
    republican = {"partyAbbreviation": "R", "partyName": "Republican", "startYear": 2023, "endYear": 2026}
    independent = {"partyAbbreviation": "I", "partyName": "Independent", "startYear": 2026}
    return [independent, republican] if order == "independent_first" else [republican, independent]


def _kiley_member(party_history_order: str) -> dict:
    return {
        "bioguideId": "K000401",
        "firstName": "Kevin",
        "lastName": "Kiley",
        "birthYear": "1985",
        "depiction": {"imageUrl": "https://www.congress.gov/img/member/k000401_200.jpg"},
        "addressInformation": {"phoneNumber": "(202) 225-2523"},
        "officialWebsiteUrl": "https://kiley.house.gov",
        "partyHistory": _kiley_party_history(party_history_order),
        "terms": [
            {
                "chamber": "House of Representatives",
                "congress": 119,
                "memberType": "Representative",
                "startYear": 2025,
                "stateCode": "CA",
                "district": 3,
            }
        ],
    }


def test_party_history_sorts_by_start_year_regardless_of_input_order():
    reversed_order = etl._party_history(_kiley_party_history("independent_first"))
    chronological = etl._party_history(_kiley_party_history("republican_first"))

    assert reversed_order == chronological
    assert [p["start_year"] for p in reversed_order] == [2023, 2026]


def test_party_history_normalizes_known_and_unknown_parties():
    result = etl._party_history([
        {"partyName": "Republican", "startYear": 2023},
        {"partyName": "Some Third Party", "startYear": 2020},
    ])

    assert result[0]["party"] == "OTHER"
    assert result[0]["source_party_name"] == "Some Third Party"
    assert result[1]["party"] == "REPUBLICAN"
    assert result[1]["source_party_name"] == "Republican"


def test_member_row_source_hash_is_independent_of_party_history_order():
    # Regression test: source_hash must not change just because the
    # upstream API happens to return partyHistory in a different order
    # across two otherwise-identical syncs.
    row_a = etl._member_row(_kiley_member("independent_first"))
    row_b = etl._member_row(_kiley_member("republican_first"))

    source_hash_index = 12
    assert row_a[source_hash_index] == row_b[source_hash_index]


def test_member_row_party_history_column_is_sorted():
    # party_history must stay a plain, JSON-serializable list here (not
    # wrapped in psycopg2.extras.Json) since transform's output crosses
    # an XCom boundary before load() does the actual DB write.
    row = etl._member_row(_kiley_member("independent_first"))

    party_history_index = 11
    stored = row[party_history_index]
    assert isinstance(stored, list)
    assert [p["start_year"] for p in stored] == [2023, 2026]


def test_term_rows_at_large_house_seat_defaults_district_to_zero():
    # Regression test: the item-level API omits "district" entirely for
    # at-large seats, unlike the list endpoint which returns 0 explicitly.
    member = {
        "bioguideId": "M001238",
        "terms": [
            {
                "chamber": "House of Representatives",
                "congress": 119,
                "memberType": "Representative",
                "startYear": 2025,
                "stateCode": "DE",
                # no "district" key
            }
        ],
    }

    rows = etl._term_rows(member, 119)

    assert len(rows) == 1
    assert rows[0][:8] == ("M001238", 119, "HOUSE", "Representative", "DE", 0, 2025, None)


def test_term_rows_senate_seat_has_no_district():
    member = {
        "bioguideId": "M001244",
        "terms": [
            {
                "chamber": "Senate",
                "congress": 119,
                "memberType": "Senator",
                "startYear": 2025,
                "stateCode": "FL",
            }
        ],
    }

    rows = etl._term_rows(member, 119)

    assert rows[0][:8] == ("M001244", 119, "SENATE", "Senator", "FL", None, 2025, None)


def test_members_needing_sync_includes_new_members_not_in_stored_data():
    summaries = [{"bioguideId": "NEW001", "updateDate": "2026-01-01T00:00:00Z"}]

    assert etl._members_needing_sync(summaries, stored_updated_at={}) == ["NEW001"]


def test_members_needing_sync_skips_members_with_unchanged_update_date():
    last_synced = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "SAME001", "updateDate": "2026-01-01T00:00:00Z"}]

    result = etl._members_needing_sync(summaries, {"SAME001": last_synced})

    assert result == []


def test_members_needing_sync_includes_members_with_newer_update_date():
    last_synced = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "CHANGED001", "updateDate": "2026-06-01T00:00:00Z"}]

    result = etl._members_needing_sync(summaries, {"CHANGED001": last_synced})

    assert result == ["CHANGED001"]


def test_members_needing_sync_includes_members_missing_update_date_defensively():
    # If the API ever omits updateDate we can't tell whether the member
    # changed, so err on the side of re-fetching rather than risk
    # silently skipping a real update forever.
    last_synced = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "NOUPDATE001", "updateDate": None}]

    result = etl._members_needing_sync(summaries, {"NOUPDATE001": last_synced})

    assert result == ["NOUPDATE001"]


def test_members_needing_sync_skips_members_with_older_update_date():
    last_synced = datetime(2026, 6, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "OLD001", "updateDate": "2026-01-01T00:00:00Z"}]

    result = etl._members_needing_sync(summaries, {"OLD001": last_synced})

    assert result == []


def test_term_rows_only_includes_the_requested_congress():
    member = {
        "bioguideId": "T000489",
        "terms": [
            {
                "chamber": "House of Representatives",
                "congress": 118,
                "memberType": "Representative",
                "startYear": 2023,
                "endYear": 2025,
                "stateCode": "TX",
                "district": 18,
            },
            {
                "chamber": "House of Representatives",
                "congress": 119,
                "memberType": "Representative",
                "startYear": 2025,
                "endYear": 2025,
                "stateCode": "TX",
                "district": 18,
            },
        ],
    }

    rows = etl._term_rows(member, 119)

    assert len(rows) == 1
    assert rows[0][1] == 119
    assert rows[0][7] == 2025
