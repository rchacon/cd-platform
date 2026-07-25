from datetime import date, datetime, timezone

from psycopg2.extras import Json

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


def test_party_history_does_not_crash_on_missing_start_year():
    # Regression test: sorted() previously compared start_year values
    # directly, raising TypeError when one entry's start_year was None
    # (missing/malformed startYear from the API) and another was an int.
    result = etl._party_history([
        {"partyName": "Republican", "startYear": 2023},
        {"partyName": "Independent"},  # no startYear
    ])

    assert [p["start_year"] for p in result] == [None, 2023]


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


def test_wrap_party_history_for_insert_wraps_only_that_column():
    # Regression test: load() reconstructs each row by index to inject
    # Json(...) around party_history -- this pins that reconstruction
    # so a future change to _member_row's tuple shape can't silently
    # shift which column gets wrapped without a test failing.
    row = etl._member_row(_kiley_member("independent_first"))

    wrapped = etl._wrap_party_history_for_insert([row])[0]

    assert isinstance(wrapped[11], Json)
    assert wrapped[11].adapted == row[11]
    assert wrapped[:11] == row[:11]
    assert wrapped[12:] == row[12:]


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


def test_derive_congress_dates_uses_earliest_session_start_and_start_year_plus_two():
    # Mirrors the real /congress/current shape: two sessions already
    # underway, plus a second session pair that hasn't started yet.
    payload = {
        "number": 119,
        "startYear": "2025",
        "endYear": "2026",
        "sessions": [
            {"chamber": "Senate", "startDate": "2025-01-03", "endDate": "2026-01-03", "number": 1},
            {"chamber": "House of Representatives", "startDate": "2025-01-03", "endDate": "2026-01-03", "number": 1},
            {"chamber": "House of Representatives", "startDate": "2026-01-03", "number": 2},
            {"chamber": "Senate", "startDate": "2026-01-03", "number": 2},
        ],
    }

    number, start_date, end_date = etl._derive_congress_dates(payload)

    assert number == 119
    assert start_date == date(2025, 1, 3)
    assert end_date == date(2027, 1, 3)


def test_derive_congress_dates_falls_back_when_no_sessions_have_start_dates():
    payload = {"number": 120, "startYear": "2027", "sessions": []}

    number, start_date, end_date = etl._derive_congress_dates(payload)

    assert number == 120
    assert start_date == date(2027, 1, 3)
    assert end_date == date(2029, 1, 3)


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


def test_transform_skips_malformed_member_without_failing_the_batch():
    # Regression test: _term_rows previously indexed term["chamber"] etc.
    # unguarded, so one member with an unrecognized chamber value raised
    # an uncaught KeyError that aborted transform() for the whole batch.
    dag = etl.congress_members_etl()
    transform = dag.task_dict["transform"].python_callable

    good_member = {
        "bioguideId": "GOOD001",
        "firstName": "Jane",
        "lastName": "Doe",
        "terms": [{
            "chamber": "House of Representatives",
            "congress": 119,
            "memberType": "Representative",
            "startYear": 2025,
            "stateCode": "CA",
            "district": 1,
        }],
    }
    bad_member = {
        "bioguideId": "BAD001",
        "firstName": "Bad",
        "lastName": "Data",
        "terms": [{
            "chamber": "Unrecognized Chamber",
            "congress": 119,
            "memberType": "Representative",
            "startYear": 2025,
            "stateCode": "CA",
            "district": 1,
        }],
    }

    result = transform([good_member, bad_member], 119)

    assert [row[0] for row in result["members"]] == ["GOOD001"]
    assert [row[0] for row in result["terms"]] == ["GOOD001"]


def test_dag_has_expected_tasks_wired_in_the_expected_order():
    # Cheap sanity check that catches typos/wiring mistakes (a renamed
    # task, a dropped dependency) before they ever reach a real
    # Airflow run.
    dag = etl.congress_members_etl()

    assert dag.dag_id == "congress_members_etl"
    assert set(dag.task_dict.keys()) == {
        "sync_current_congress",
        "get_current_congress",
        "extract_member_summaries",
        "filter_members_needing_sync",
        "fetch_member_details",
        "transform",
        "load",
    }

    upstream = {
        task_id: set(task.upstream_task_ids)
        for task_id, task in dag.task_dict.items()
    }
    assert upstream["sync_current_congress"] == set()
    assert upstream["get_current_congress"] == {"sync_current_congress"}
    assert upstream["extract_member_summaries"] == {"get_current_congress"}
    assert upstream["filter_members_needing_sync"] == {"extract_member_summaries"}
    assert upstream["fetch_member_details"] == {"filter_members_needing_sync"}
    assert upstream["transform"] == {"fetch_member_details", "get_current_congress"}
    assert upstream["load"] == {"transform"}
