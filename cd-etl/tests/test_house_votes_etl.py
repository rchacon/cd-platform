from cd.etl.dags import house_votes_etl as etl


class _FakeHook:
    def __init__(self, known_votes, known_bioguide_ids):
        self._known_votes = known_votes
        self._known_bioguide_ids = known_bioguide_ids

    def get_records(self, sql, parameters=None):
        if "roll_calls" in sql:
            return self._known_votes
        return [(bioguide_id,) for bioguide_id in self._known_bioguide_ids]


class _FakeConn:
    def __init__(self):
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        raise AssertionError("cursor() should not be called on this path")

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        pass

    def close(self):
        self.closed = True


class _FakeConnHook:
    def __init__(self, conn):
        self._conn = conn

    def get_conn(self):
        return self._conn


def _house_vote_summary(
    roll_call_number, session_number=1, result="Passed",
    start_date="2025-09-08T18:56:00-04:00",
    legislation_type=None, legislation_number=None,
    amendment_type=None, amendment_number=None,
):
    summary = {
        "rollCallNumber": roll_call_number,
        "sessionNumber": session_number,
        "result": result,
        "startDate": start_date,
    }
    if legislation_type is not None:
        summary["legislationType"] = legislation_type
        summary["legislationNumber"] = legislation_number
    if amendment_type is not None:
        summary["amendmentType"] = amendment_type
        summary["amendmentNumber"] = amendment_number
    return summary


def test_house_vote_member_vote_maps_bioguideID_alias():
    # Regression test: the member-votes sub-resource uses "bioguideID"
    # (capital ID), which to_camel's automatic alias generation for a
    # bioguide_id field would NOT produce ("bioguideId") -- pins the
    # explicit Field(alias=...) override.
    member_vote = etl.HouseVoteMemberVote.model_validate(
        {"bioguideID": "A000055", "voteCast": "Yea"}
    )

    assert member_vote.bioguide_id == "A000055"
    assert member_vote.vote_cast == "Yea"


def test_resolve_amendment_bill_returns_bill_key_when_resolvable(monkeypatch):
    calls = []

    def fake_api_get(session, url, params=None):
        calls.append(url)
        return {"amendment": {"amendedBill": {"congress": 119, "type": "HR", "number": "3838"}}}

    monkeypatch.setattr(etl.congress_api, "api_get", fake_api_get)

    result = etl.resolve_amendment_bill(
        session=None, congress=119, amendment_type="HAMDT", amendment_number="97",
    )

    assert result == (119, "HR", 3838)
    assert calls[0].endswith("/119/hamdt/97")


def test_resolve_amendment_bill_returns_none_when_unresolvable(monkeypatch):
    monkeypatch.setattr(
        etl.congress_api, "api_get", lambda session, url, params=None: {"amendment": {}},
    )

    result = etl.resolve_amendment_bill(
        session=None, congress=119, amendment_type="HAMDT", amendment_number="97",
    )

    assert result is None


def test_filter_votes_needing_sync_drops_purely_procedural_votes(monkeypatch):
    monkeypatch.setattr(
        etl, "PostgresHook",
        lambda postgres_conn_id: _FakeHook(known_votes=[], known_bioguide_ids=["A000055"]),
    )

    dag = etl.house_votes_etl()
    filter_votes_needing_sync = dag.task_dict["filter_votes_needing_sync"].python_callable

    procedural = _house_vote_summary(2, result="Elected Speaker Name")
    amendment_only = _house_vote_summary(259, amendment_type="HAMDT", amendment_number="97")

    result = filter_votes_needing_sync([procedural, amendment_only], 119)

    assert result["votes"] == [amendment_only]
    assert result["known_bioguide_ids"] == ["A000055"]


def test_filter_votes_needing_sync_skips_malformed_summary(monkeypatch):
    # Regression test: one malformed vote summary (missing a required
    # field) must be logged and skipped, not raise and fail the whole
    # task -- same fault-isolation philosophy as every other per-item
    # loop in this module.
    monkeypatch.setattr(
        etl, "PostgresHook",
        lambda postgres_conn_id: _FakeHook(known_votes=[], known_bioguide_ids=[]),
    )

    dag = etl.house_votes_etl()
    filter_votes_needing_sync = dag.task_dict["filter_votes_needing_sync"].python_callable

    malformed = {"sessionNumber": 1}  # missing rollCallNumber/result/startDate
    good = _house_vote_summary(240, legislation_type="HR", legislation_number="3424")

    result = filter_votes_needing_sync([malformed, good], 119)

    assert result["votes"] == [good]


def test_filter_votes_needing_sync_skips_already_known_votes(monkeypatch):
    monkeypatch.setattr(
        etl, "PostgresHook",
        lambda postgres_conn_id: _FakeHook(known_votes=[(1, 240)], known_bioguide_ids=[]),
    )

    dag = etl.house_votes_etl()
    filter_votes_needing_sync = dag.task_dict["filter_votes_needing_sync"].python_callable

    known = _house_vote_summary(240, legislation_type="HR", legislation_number="3424")

    result = filter_votes_needing_sync([known], 119)

    assert result["votes"] == []


def test_resolve_bills_skips_vote_with_malformed_legislation_number(monkeypatch):
    fake_conn = _FakeConn()
    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: _FakeConnHook(fake_conn))

    dag = etl.house_votes_etl()
    resolve_bills = dag.task_dict["resolve_bills"].python_callable

    bad_vote = _house_vote_summary(1, legislation_type="HR", legislation_number="not-a-number")

    result = resolve_bills([bad_vote], 119)

    assert result == []
    assert fake_conn.rolled_back
    assert fake_conn.closed


def test_resolve_bills_skips_vote_with_unresolvable_amendment(monkeypatch):
    fake_conn = _FakeConn()
    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: _FakeConnHook(fake_conn))
    monkeypatch.setattr(
        etl.congress_api, "api_get", lambda session, url, params=None: {"amendment": {}},
    )

    dag = etl.house_votes_etl()
    resolve_bills = dag.task_dict["resolve_bills"].python_callable

    amendment_vote = _house_vote_summary(259, amendment_type="HAMDT", amendment_number="97")

    result = resolve_bills([amendment_vote], 119)

    assert result == []
    # Unlike the malformed-number case, an unresolvable amendment is not
    # an exception -- it's a normal early continue, so no DB work was
    # ever attempted and no rollback is needed.
    assert not fake_conn.rolled_back
    assert fake_conn.closed


def test_resolve_bills_skips_vote_when_amendment_bill_congress_mismatches(monkeypatch):
    # Regression test: an amendment's resolved bill should always belong
    # to the Congress currently being synced -- a mismatch must be
    # treated as a resolution failure (skip + rollback), not silently
    # trusted into a roll_calls row pointed at the wrong congress.
    fake_conn = _FakeConn()
    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: _FakeConnHook(fake_conn))
    monkeypatch.setattr(
        etl.congress_api, "api_get",
        lambda session, url, params=None: {
            "amendment": {"amendedBill": {"congress": 118, "type": "HR", "number": "3838"}}
        },
    )

    dag = etl.house_votes_etl()
    resolve_bills = dag.task_dict["resolve_bills"].python_callable

    amendment_vote = _house_vote_summary(259, amendment_type="HAMDT", amendment_number="97")

    result = resolve_bills([amendment_vote], 119)

    assert result == []
    assert fake_conn.rolled_back
    assert fake_conn.closed


def test_extract_house_vote_summaries_covers_both_sessions(monkeypatch):
    calls = []

    def fake_paginate(session, url, params, items_key, page_limit):
        calls.append(url)
        return [{"rollCallNumber": 1}]

    monkeypatch.setattr(etl.congress_api, "paginate", fake_paginate)

    dag = etl.house_votes_etl()
    extract_house_vote_summaries = dag.task_dict["extract_house_vote_summaries"].python_callable

    result = extract_house_vote_summaries(119)

    assert len(result) == 2
    assert any(url.endswith("/119/1") for url in calls)
    assert any(url.endswith("/119/2") for url in calls)


def test_fetch_vote_details_skips_failed_fetch_without_failing_the_batch(monkeypatch):
    # Regression test: fetch_vote_details was split out of resolve_bills's
    # own sequential loop into its own concurrent task -- mirrors
    # sync_member_votes's own per-batch member-vote fetch fault isolation,
    # one vote's failed detail fetch shouldn't discard the others.
    def fake_api_get(session, url, params=None):
        if url.endswith("/1/240"):
            raise RuntimeError("simulated failure")
        return {"houseRollCallVote": {"voteQuestion": "On Passage"}}

    monkeypatch.setattr(etl.congress_api, "api_get", fake_api_get)

    dag = etl.house_votes_etl()
    fetch_vote_details = dag.task_dict["fetch_vote_details"].python_callable

    resolved = [
        {"session": 1, "roll_call_number": 240},
        {"session": 1, "roll_call_number": 241},
    ]

    result = fetch_vote_details(resolved, 119)

    assert len(result) == 1
    assert result[0]["roll_call_number"] == 241
    assert result[0]["vote_question"] == "On Passage"


def _resolved_vote(roll_call_number=240, session=1, bill_id=1):
    return {
        "session": session, "roll_call_number": roll_call_number, "bill_id": bill_id,
        "result": "Passed", "vote_date": "2025-09-08",
    }


def _vote_detail(roll_call_number=240, session=1, vote_question="On Passage"):
    return {"session": session, "roll_call_number": roll_call_number, "vote_question": vote_question}


def _vote_question_by_key(vote_details):
    return {(vd["session"], vd["roll_call_number"]): vd["vote_question"] for vd in vote_details}


def test_build_batch_rows_normalizes_vote_cast_case_insensitively():
    resolved = [_resolved_vote()]
    vote_details = [_vote_detail()]
    fetched_by_key = {(1, 240): [
        {"bioguideID": "A000055", "voteCast": "yea"},
        {"bioguideID": "A000148", "voteCast": "AYE"},
        {"bioguideID": "A000369", "voteCast": "Nay"},
    ]}

    roll_call_rows, casts_by_key = etl._build_batch_rows(
        resolved, _vote_question_by_key(vote_details), fetched_by_key,
        {"A000055", "A000148", "A000369"}, 119,
    )

    assert roll_call_rows[0][0] == "HOUSE"
    assert roll_call_rows[0][5] == "On Passage"
    casts = dict(casts_by_key[(1, 240)])
    assert casts == {"A000055": "YEA", "A000148": "YEA", "A000369": "NAY"}


def test_build_batch_rows_skips_malformed_vote_cast_without_failing_the_vote():
    # Regression test: an unrecognized vote_cast value shouldn't discard
    # the rest of that roll call's member votes, mirroring
    # members_etl.py's per-item fault isolation in transform().
    resolved = [_resolved_vote()]
    vote_details = [_vote_detail()]
    fetched_by_key = {(1, 240): [
        {"bioguideID": "A000055", "voteCast": "Yea"},
        {"bioguideID": "A000148", "voteCast": "Unrecognized"},
    ]}

    roll_call_rows, casts_by_key = etl._build_batch_rows(
        resolved, _vote_question_by_key(vote_details), fetched_by_key,
        {"A000055", "A000148"}, 119,
    )

    assert len(roll_call_rows) == 1
    casts = dict(casts_by_key[(1, 240)])
    assert casts == {"A000055": "YEA"}


def test_build_batch_rows_drops_member_vote_for_unknown_bioguide_id():
    # Defensive test: roll_call_member_votes.bioguide_id has a hard FK
    # to members -- an unknown id must be dropped in Python before it
    # can fail the whole execute_values batch at insert time.
    resolved = [_resolved_vote()]
    vote_details = [_vote_detail()]
    fetched_by_key = {(1, 240): [
        {"bioguideID": "KNOWN01", "voteCast": "Yea"},
        {"bioguideID": "UNKNOWN01", "voteCast": "Nay"},
    ]}

    roll_call_rows, casts_by_key = etl._build_batch_rows(
        resolved, _vote_question_by_key(vote_details), fetched_by_key, {"KNOWN01"}, 119,
    )

    casts = dict(casts_by_key[(1, 240)])
    assert casts == {"KNOWN01": "YEA"}


def test_build_batch_rows_skips_vote_missing_from_fetched_by_key_without_failing_the_batch():
    # Regression test: mirrors the old fetch_member_votes's fault
    # isolation, now at the pure _build_batch_rows layer so this
    # coverage doesn't need a live Postgres connection -- one vote's
    # failed/absent member-vote fetch (missing from fetched_by_key, same
    # shape congress_api.fetch_concurrently leaves a failed id in) must
    # not discard the rest of the batch.
    resolved = [_resolved_vote(240), _resolved_vote(241)]
    vote_details = [_vote_detail(240), _vote_detail(241)]
    fetched_by_key = {
        # 240 is absent entirely -- its fetch failed.
        (1, 241): [{"bioguideID": "A000055", "voteCast": "Yea"}],
    }

    roll_call_rows, casts_by_key = etl._build_batch_rows(
        resolved, _vote_question_by_key(vote_details), fetched_by_key, {"A000055"}, 119,
    )

    assert len(roll_call_rows) == 1
    assert roll_call_rows[0][3] == 241
    assert casts_by_key == {(1, 241): [("A000055", "YEA")]}


def test_build_batch_rows_drops_vote_missing_its_detail_entirely():
    # Regression test: a vote whose detail fetch (vote_question) failed
    # (absent from vote_question_by_key) must produce NO roll_calls row
    # at all -- vote_question is a NOT NULL column, same
    # transactional-invariant reasoning as the missing-member-votes case
    # below.
    resolved = [_resolved_vote()]
    fetched_by_key = {(1, 240): [{"bioguideID": "A000055", "voteCast": "Yea"}]}

    roll_call_rows, casts_by_key = etl._build_batch_rows(
        resolved, {}, fetched_by_key, {"A000055"}, 119,
    )

    assert roll_call_rows == []
    assert casts_by_key == {}


def test_build_batch_rows_drops_vote_missing_its_member_votes_entirely():
    # Regression test for the transactional invariant: a vote whose
    # member-vote fetch failed (absent from fetched_by_key) must produce
    # NO roll_calls row at all, not a roll call with zero casts --
    # incremental sync has no separate retry path for an already-known
    # roll call, so a partially-loaded one would be permanently stuck.
    resolved = [_resolved_vote()]
    vote_details = [_vote_detail()]

    roll_call_rows, casts_by_key = etl._build_batch_rows(
        resolved, _vote_question_by_key(vote_details), {}, set(), 119,
    )

    assert roll_call_rows == []
    assert casts_by_key == {}


def test_build_batch_rows_drops_vote_when_all_casts_filtered_out():
    # Regression test: a vote whose every cast got filtered out (all
    # unknown bioguide_ids, here) must produce NO roll_calls row --
    # same transactional-invariant reasoning as the missing-member-votes
    # case above. Previously this only checked for raw_casts is None,
    # missing the case where casts is a non-empty list that fully empties
    # out after per-cast filtering.
    resolved = [_resolved_vote()]
    vote_details = [_vote_detail()]
    fetched_by_key = {(1, 240): [
        {"bioguideID": "UNKNOWN01", "voteCast": "Yea"},
        {"bioguideID": "UNKNOWN02", "voteCast": "Nay"},
    ]}

    roll_call_rows, casts_by_key = etl._build_batch_rows(
        resolved, _vote_question_by_key(vote_details), fetched_by_key, {"KNOWN01"}, 119,
    )

    assert roll_call_rows == []
    assert casts_by_key == {}


def test_build_batch_rows_returns_no_rows_when_every_vote_in_batch_fails():
    # Regression test for the new empty-batch case: sync_member_votes()
    # must not call execute_values() with an empty argslist when every
    # vote in a batch fails validation (here, every vote is missing its
    # detail fetch) -- pins that _build_batch_rows() itself returns
    # cleanly empty rather than raising.
    resolved = [_resolved_vote(240), _resolved_vote(241)]

    roll_call_rows, casts_by_key = etl._build_batch_rows(resolved, {}, {}, set(), 119)

    assert roll_call_rows == []
    assert casts_by_key == {}


def test_dag_has_expected_tasks_wired_in_the_expected_order():
    dag = etl.house_votes_etl()

    assert dag.dag_id == "house_votes_etl"
    assert set(dag.task_dict.keys()) == {
        "get_current_congress",
        "extract_house_vote_summaries",
        "filter_votes_needing_sync",
        "resolve_bills",
        "fetch_vote_details",
        "sync_member_votes",
    }

    upstream = {
        task_id: set(task.upstream_task_ids)
        for task_id, task in dag.task_dict.items()
    }
    assert upstream["get_current_congress"] == set()
    assert upstream["extract_house_vote_summaries"] == {"get_current_congress"}
    assert upstream["filter_votes_needing_sync"] == {
        "extract_house_vote_summaries", "get_current_congress",
    }
    assert upstream["resolve_bills"] == {"filter_votes_needing_sync", "get_current_congress"}
    assert upstream["fetch_vote_details"] == {"resolve_bills", "get_current_congress"}
    assert upstream["sync_member_votes"] == {
        "resolve_bills", "fetch_vote_details",
        "filter_votes_needing_sync", "get_current_congress",
    }
