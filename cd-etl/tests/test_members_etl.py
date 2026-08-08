from datetime import date, datetime, timezone

from psycopg2.extras import Json

import members_etl as etl


def _kiley_party_history(order: str) -> list[dict]:
    republican = {"partyAbbreviation": "R", "partyName": "Republican", "startYear": 2023, "endYear": 2026}
    independent = {"partyAbbreviation": "I", "partyName": "Independent", "startYear": 2026}
    return [independent, republican] if order == "independent_first" else [republican, independent]


def _party_history_entries(order: str) -> list[etl.PartyHistoryEntry]:
    return [etl.PartyHistoryEntry.model_validate(entry) for entry in _kiley_party_history(order)]


def _kiley_member(party_history_order: str) -> etl.MemberDetail:
    return etl.MemberDetail.model_validate({
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
    })


def test_party_history_sorts_by_start_year_regardless_of_input_order():
    reversed_order = etl._party_history(_party_history_entries("independent_first"))
    chronological = etl._party_history(_party_history_entries("republican_first"))

    assert reversed_order == chronological
    assert [p["start_year"] for p in reversed_order] == [2023, 2026]


def test_party_history_drops_entries_with_missing_start_year():
    # An entry with no start_year can't be placed chronologically, so it
    # can never correctly answer "which is the most recent party" --
    # it's dropped rather than stored (also incidentally means sorted()
    # never has to compare a None start_year against anything).
    result = etl._party_history([
        etl.PartyHistoryEntry(party_name="Republican", start_year=2023),
        etl.PartyHistoryEntry(party_name="Independent"),  # no start_year
    ])

    assert [p["start_year"] for p in result] == [2023]


def test_count_missing_start_year_counts_entries_without_a_start_year():
    party_history = [
        etl.PartyHistoryEntry(party_name="Republican", start_year=2023),
        etl.PartyHistoryEntry(party_name="Independent"),
        etl.PartyHistoryEntry(party_name="Democratic", start_year=None),
    ]

    assert etl._count_missing_start_year(party_history) == 2


def test_party_history_normalizes_known_and_unknown_parties():
    result = etl._party_history([
        etl.PartyHistoryEntry(party_name="Republican", start_year=2023),
        etl.PartyHistoryEntry(party_name="Some Third Party", start_year=2020),
    ])

    assert result[0]["party"] == "OTHER"
    assert result[0]["source_party_name"] == "Some Third Party"
    assert result[1]["party"] == "REPUBLICAN"
    assert result[1]["source_party_name"] == "Republican"


def test_party_history_normalizes_independent_republican_symmetrically_with_democrat():
    # PARTY_MAP had "Independent Democrat" -> DEMOCRATIC but no
    # "Independent Republican" entry, so the latter fell through to
    # OTHER instead of REPUBLICAN.
    result = etl._party_history([
        etl.PartyHistoryEntry(party_name="Independent Republican", start_year=2020),
    ])

    assert result[0]["party"] == "REPUBLICAN"
    assert result[0]["source_party_name"] == "Independent Republican"


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
    member = etl.MemberDetail.model_validate({
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
    })

    rows = etl._term_rows(member, 119)

    assert len(rows) == 1
    assert rows[0][:8] == ("M001238", 119, "HOUSE", "Representative", "DE", 0, 2025, None)


def test_term_rows_senate_seat_has_no_district():
    member = etl.MemberDetail.model_validate({
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
    })

    rows = etl._term_rows(member, 119)

    assert rows[0][:8] == ("M001244", 119, "SENATE", "Senator", "FL", None, 2025, None)


def test_derive_congress_dates_uses_earliest_session_start_and_start_year_plus_two():
    # Mirrors the real /congress/current shape: two sessions already
    # underway, plus a second session pair that hasn't started yet.
    congress = etl.CongressCurrent.model_validate({
        "number": 119,
        "startYear": "2025",
        "endYear": "2026",
        "sessions": [
            {"chamber": "Senate", "startDate": "2025-01-03", "endDate": "2026-01-03", "number": 1},
            {"chamber": "House of Representatives", "startDate": "2025-01-03", "endDate": "2026-01-03", "number": 1},
            {"chamber": "House of Representatives", "startDate": "2026-01-03", "number": 2},
            {"chamber": "Senate", "startDate": "2026-01-03", "number": 2},
        ],
    })

    number, start_date, end_date = etl._derive_congress_dates(congress)

    assert number == 119
    assert start_date == date(2025, 1, 3)
    assert end_date == date(2027, 1, 3)


def test_derive_congress_dates_falls_back_when_no_sessions_have_start_dates():
    congress = etl.CongressCurrent.model_validate({
        "number": 120, "startYear": "2027", "sessions": [],
    })

    number, start_date, end_date = etl._derive_congress_dates(congress)

    assert number == 120
    assert start_date == date(2027, 1, 3)
    assert end_date == date(2029, 1, 3)


def test_members_needing_sync_includes_new_members_not_in_stored_data():
    summaries = [{"bioguideId": "NEW001", "updateDate": "2026-01-01T00:00:00Z"}]

    result = etl._members_needing_sync(
        summaries, stored_updated_at={}, bioguide_ids_with_current_term=set(),
    )

    assert result == ["NEW001"]


def test_members_needing_sync_skips_members_with_unchanged_update_date():
    last_synced = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "SAME001", "updateDate": "2026-01-01T00:00:00Z"}]

    result = etl._members_needing_sync(
        summaries, {"SAME001": last_synced}, bioguide_ids_with_current_term={"SAME001"},
    )

    assert result == []


def test_members_needing_sync_includes_members_with_newer_update_date():
    last_synced = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "CHANGED001", "updateDate": "2026-06-01T00:00:00Z"}]

    result = etl._members_needing_sync(
        summaries, {"CHANGED001": last_synced}, bioguide_ids_with_current_term={"CHANGED001"},
    )

    assert result == ["CHANGED001"]


def test_members_needing_sync_includes_members_missing_update_date_defensively():
    # If the API ever omits updateDate we can't tell whether the member
    # changed, so err on the side of re-fetching rather than risk
    # silently skipping a real update forever.
    last_synced = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "NOUPDATE001", "updateDate": None}]

    result = etl._members_needing_sync(
        summaries, {"NOUPDATE001": last_synced}, bioguide_ids_with_current_term={"NOUPDATE001"},
    )

    assert result == ["NOUPDATE001"]


def test_members_needing_sync_skips_members_with_older_update_date():
    last_synced = datetime(2026, 6, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "OLD001", "updateDate": "2026-01-01T00:00:00Z"}]

    result = etl._members_needing_sync(
        summaries, {"OLD001": last_synced}, bioguide_ids_with_current_term={"OLD001"},
    )

    assert result == []


def test_members_needing_sync_includes_returning_member_missing_current_congress_term():
    # Regression test (Congress rollover): a returning incumbent whose
    # bio-level updateDate is unchanged must still be re-synced if they
    # don't yet have a member_terms row for the current Congress --
    # otherwise they'd silently never get one, since skipping the
    # detail fetch also skips _term_rows for the new Congress.
    last_synced = datetime(2026, 1, 1, tzinfo=timezone.utc)
    summaries = [{"bioguideId": "RETURNING001", "updateDate": "2026-01-01T00:00:00Z"}]

    result = etl._members_needing_sync(
        summaries, {"RETURNING001": last_synced}, bioguide_ids_with_current_term=set(),
    )

    assert result == ["RETURNING001"]


def test_term_rows_only_includes_the_requested_congress():
    member = etl.MemberDetail.model_validate({
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
    })

    rows = etl._term_rows(member, 119)

    assert len(rows) == 1
    assert rows[0][1] == 119
    assert rows[0][7] == 2025


def test_fetch_member_details_skips_failed_fetches_without_failing_the_batch(monkeypatch):
    # Regression test: ThreadPoolExecutor.map + list(...) previously
    # re-raised the first exception, discarding every other
    # already-fetched member's details when even one fetch failed.
    def fake_api_get(session, url, params=None):
        bioguide_id = url.rsplit("/", 1)[-1]
        if bioguide_id == "BAD001":
            raise RuntimeError("simulated API failure")
        return {"member": {"bioguideId": bioguide_id}}

    monkeypatch.setattr(etl.congress_api, "api_get", fake_api_get)

    dag = etl.congress_members_etl()
    fetch_member_details = dag.task_dict["fetch_member_details"].python_callable

    result = fetch_member_details(["GOOD001", "BAD001", "GOOD002"])

    assert sorted(m["bioguideId"] for m in result) == ["GOOD001", "GOOD002"]


def _senator_entry(bioguide="C000127", lis="S275", state_rank="junior"):
    # Wide, safely-bounded date ranges (not tied to any particular real
    # "today") so these tests stay valid regardless of when they run --
    # a historical term that's definitely over, and a current term that's
    # open-ended (no "end", matching a still-serving senator).
    return {
        "id": {"bioguide": bioguide, "lis": lis},
        "terms": [
            {"type": "sen", "start": "2000-01-03", "end": "2010-01-03", "state_rank": "senior"},
            {"type": "sen", "start": "2010-01-04", "state_rank": state_rank},
        ],
    }


def _house_entry(bioguide="A000055"):
    return {
        "id": {"bioguide": bioguide},  # no "lis" key at all -- House members never get one
        "terms": [
            {"type": "rep", "start": "2000-01-03", "end": "2010-01-03", "state": "AL"},
            {"type": "rep", "start": "2010-01-04", "state": "AL"},
        ],
    }


def test_crosswalk_row_resolves_current_senate_term():
    result = etl._crosswalk_row(_senator_entry())

    assert result == ("C000127", "S275", "JUNIOR")


def test_crosswalk_row_house_member_has_no_lis_or_rank():
    # House members never carry id.lis or terms[].state_rank in this
    # source -- both Senate-only concepts.
    result = etl._crosswalk_row(_house_entry())

    assert result == ("A000055", None, None)


def test_crosswalk_row_ignores_expired_terms():
    entry = {
        "id": {"bioguide": "GONE001", "lis": "S001"},
        "terms": [{"type": "sen", "start": "2000-01-03", "end": "2010-01-03", "state_rank": "junior"}],
    }

    result = etl._crosswalk_row(entry)

    assert result == ("GONE001", None, None)


def test_crosswalk_row_treats_missing_end_as_open_ended():
    entry = {
        "id": {"bioguide": "NEW001", "lis": "S999"},
        "terms": [{"type": "sen", "start": "2010-01-03", "state_rank": "senior"}],  # no "end"
    }

    result = etl._crosswalk_row(entry)

    assert result == ("NEW001", "S999", "SENIOR")


def test_crosswalk_row_returns_none_for_missing_bioguide_id():
    assert etl._crosswalk_row({"id": {}, "terms": []}) is None


def test_crosswalk_row_skips_term_with_malformed_end_instead_of_raising():
    # Regression test: a malformed "end" value previously raised
    # ValueError uncaught (only "start" was inside the try/except) --
    # that would propagate out of _crosswalk_row and fail transform()'s
    # whole crosswalk loop, including the member/term transform sharing
    # its task. Now just skips this one term, same as a malformed start.
    entry = {
        "id": {"bioguide": "BADEND001", "lis": "S001"},
        "terms": [{"type": "sen", "start": "2010-01-03", "end": "not-a-date", "state_rank": "junior"}],
    }

    result = etl._crosswalk_row(entry)

    assert result == ("BADEND001", None, None)


def test_crosswalk_row_picks_latest_start_among_overlapping_matches():
    # Defensive tie-break: terms' upstream ordering isn't trusted (same
    # reasoning as _party_history's explicit sort) -- the candidate with
    # the latest start wins even when it isn't listed last.
    entry = {
        "id": {"bioguide": "TIE001", "lis": "S001"},
        "terms": [
            {"type": "sen", "start": "2015-01-03", "state_rank": "junior"},
            {"type": "sen", "start": "2010-01-03", "end": "2099-01-01", "state_rank": "senior"},
        ],
    }

    result = etl._crosswalk_row(entry)

    assert result == ("TIE001", "S001", "JUNIOR")


class _FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"simulated HTTP {self._status}")


def test_extract_legislators_crosswalk_returns_parsed_yaml(monkeypatch):
    yaml_text = "- id:\n    bioguide: C000127\n    lis: S275\n  terms:\n  - type: sen\n    start: '2010-01-04'\n    state_rank: junior\n"
    monkeypatch.setattr(etl._API_SESSION, "get", lambda url, timeout=None: _FakeResponse(yaml_text))

    dag = etl.congress_members_etl()
    extract_legislators_crosswalk = dag.task_dict["extract_legislators_crosswalk"].python_callable

    result = extract_legislators_crosswalk()

    assert result == [{"id": {"bioguide": "C000127", "lis": "S275"}, "terms": [
        {"type": "sen", "start": "2010-01-04", "state_rank": "junior"},
    ]}]


def test_extract_legislators_crosswalk_returns_empty_list_on_fetch_failure(monkeypatch):
    # Regression test: a broken/unreachable crosswalk source must never
    # fail this task -- that would fail/retry the whole DAG run over data
    # that's best-effort by design, blocking the member sync cd-lookup
    # actually depends on.
    def fake_get(url, timeout=None):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(etl._API_SESSION, "get", fake_get)

    dag = etl.congress_members_etl()
    extract_legislators_crosswalk = dag.task_dict["extract_legislators_crosswalk"].python_callable

    assert extract_legislators_crosswalk() == []


def test_transform_builds_crosswalk_rows():
    dag = etl.congress_members_etl()
    transform = dag.task_dict["transform"].python_callable

    result = transform([], 119, [_senator_entry(), _house_entry()])

    assert set(result["crosswalk"]) == {
        ("C000127", "S275", "JUNIOR"),
        ("A000055", None, None),
    }


def test_transform_skips_malformed_member_without_failing_the_batch():
    # Regression test: _term_rows previously indexed term["chamber"] etc.
    # unguarded, so one member with an unrecognized chamber value raised
    # an uncaught KeyError that aborted transform() for the whole batch.
    # Now the chamber lookup happens via CHAMBER_MAP after the member
    # has already parsed cleanly as a MemberDetail (chamber is a plain
    # str field, not validated against a closed set at parse time), so
    # this still fails at the same place -- CHAMBER_MAP[term.chamber] --
    # just one layer further in.
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

    result = transform([good_member, bad_member], 119, [])

    assert [row[0] for row in result["members"]] == ["GOOD001"]
    assert [row[0] for row in result["terms"]] == ["GOOD001"]


def test_transform_skips_member_with_missing_required_field():
    # Regression test: a member missing bioguideId entirely (required on
    # MemberDetail) now raises a pydantic ValidationError at parse time
    # instead of a KeyError deep inside _member_row -- still caught by
    # the same per-member try/except, so one bad record still can't
    # abort the whole batch.
    dag = etl.congress_members_etl()
    transform = dag.task_dict["transform"].python_callable

    good_member = {"bioguideId": "GOOD001", "firstName": "Jane", "lastName": "Doe", "terms": []}
    bad_member = {"firstName": "No", "lastName": "BioguideId", "terms": []}

    result = transform([good_member, bad_member], 119, [])

    assert [row[0] for row in result["members"]] == ["GOOD001"]


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
        "extract_legislators_crosswalk",
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
    assert upstream["filter_members_needing_sync"] == {
        "extract_member_summaries", "get_current_congress",
    }
    assert upstream["fetch_member_details"] == {"filter_members_needing_sync"}
    # No upstream at all -- independent of current_congress/member
    # details, so Airflow runs it concurrently with the rest of the chain.
    assert upstream["extract_legislators_crosswalk"] == set()
    assert upstream["transform"] == {
        "fetch_member_details", "get_current_congress", "extract_legislators_crosswalk",
    }
    assert upstream["load"] == {"transform"}


def test_api_session_reuses_connections_and_retries_transient_failures():
    # Regression test: members_etl previously built its own bare
    # requests.Session() with no retry/backoff -- across ~500+
    # per-member detail calls that meant no connection reuse and a
    # single transient 5xx/timeout failing the whole task. Now built via
    # congress_api.build_session(), shared by any future DAG.
    adapter = etl._API_SESSION.get_adapter("https://api.congress.gov")

    assert adapter.max_retries.total == 3
    assert "GET" in adapter.max_retries.allowed_methods
    assert 500 in adapter.max_retries.status_forcelist
