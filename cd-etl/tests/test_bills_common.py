import time

import pytest
from psycopg2.extras import execute_values

from cd.etl import bills_common
from cd.etl.congress_models import BillSummaryItem
from conftest import random_number

# The 119th Congress is seeded by migration 0001, so bill fixtures below
# don't need their own congresses row.
CONGRESS = 119

# pg_conn fixture lives in conftest.py, shared across every real-Postgres
# test module.


@pytest.fixture
def test_bill_number(pg_conn):
    # Kept well above any real bill's current range, and under
    # bill_number's SMALLINT max (32767).
    bill_number = random_number(20000, 29000)
    yield bill_number
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM bills WHERE congress = %s AND bill_type = 'HR' AND bill_number = %s",
            (CONGRESS, bill_number),
        )
    pg_conn.commit()


@pytest.fixture
def test_bill_id(pg_conn, test_bill_number):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            bills_common.BILLS_UPSERT_SQL,
            (CONGRESS, "HR", test_bill_number, "Test Bill Title", "Health", None, "hash-bill", None),
        )
        bill_id = cursor.fetchone()[0]
    pg_conn.commit()
    return bill_id


def _get_bill_row(pg_conn, bill_id):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT title, policy_area, crs_summary, updated_at FROM bills WHERE bill_id = %s",
            (bill_id,),
        )
        return cursor.fetchone()


def _get_bill_subjects(pg_conn, bill_id):
    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT subject_name FROM bill_subjects WHERE bill_id = %s", (bill_id,))
        return [row[0] for row in cursor.fetchall()]


def test_bills_upsert_returning_yields_bill_id_even_on_unchanged_conflict(
    pg_conn, test_bill_number,
):
    # Pins the deliberate difference from MEMBERS_UPSERT_SQL: this
    # ON CONFLICT is NOT WHERE-gated, so RETURNING always yields bill_id
    # even when re-run with an identical row.
    row = (CONGRESS, "HR", test_bill_number, "A Title", "Health", "A summary", "hash-a", None)
    with pg_conn.cursor() as cursor:
        cursor.execute(bills_common.BILLS_UPSERT_SQL, row)
        first_bill_id = cursor.fetchone()[0]
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute(bills_common.BILLS_UPSERT_SQL, row)
        second_bill_id = cursor.fetchone()[0]
    pg_conn.commit()

    assert first_bill_id == second_bill_id


def test_bill_subjects_delete_and_reinsert_replaces_prior_set(pg_conn, test_bill_id):
    # Exercises the delete+reinsert pattern sync_bill uses for a bill's
    # subjects directly.
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, bills_common.BILL_SUBJECTS_INSERT_SQL,
            [(test_bill_id, "Health"), (test_bill_id, "Insurance")],
        )
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute("DELETE FROM bill_subjects WHERE bill_id = %s", (test_bill_id,))
        execute_values(cursor, bills_common.BILL_SUBJECTS_INSERT_SQL, [(test_bill_id, "Tax Policy")])
    pg_conn.commit()

    assert _get_bill_subjects(pg_conn, test_bill_id) == ["Tax Policy"]


def test_latest_crs_summary_prefers_non_empty_text_over_merely_latest_dated():
    # Regression test: the most-recently-dated entry having null/empty
    # text shouldn't discard an earlier entry's real, usable text.
    summaries = [
        BillSummaryItem(action_date="2025-01-01", text="Introduced summary"),
        BillSummaryItem(action_date="2025-06-01", text=None),
    ]

    assert bills_common._latest_crs_summary(summaries) == "Introduced summary"


def test_latest_crs_summary_returns_none_when_no_entry_has_text():
    summaries = [
        BillSummaryItem(action_date="2025-01-01", text=None),
        BillSummaryItem(action_date="2025-06-01", text=""),
    ]

    assert bills_common._latest_crs_summary(summaries) is None


def _fake_api_get(bill_title, policy_area, subject_names, summaries):
    # summaries: list of (action_date, text) tuples, oldest-to-newest
    # order not required -- sync_bill is what's responsible for picking
    # the latest one.
    def fake_api_get(session, url, params=None):
        if url.endswith("/subjects"):
            return {"subjects": {"legislativeSubjects": [{"name": n} for n in subject_names]}}
        if url.endswith("/summaries"):
            return {
                "summaries": [
                    {"actionDate": action_date, "text": text}
                    for action_date, text in summaries
                ]
            }
        return {
            "bill": {
                "congress": CONGRESS, "type": "HR", "number": "1",
                "title": bill_title,
                "policyArea": {"name": policy_area} if policy_area else None,
                "updateDate": "2025-01-01T00:00:00Z",
            }
        }

    return fake_api_get


def test_sync_bill_stores_title_policy_area_subjects_and_latest_crs_summary(
    pg_conn, test_bill_number, monkeypatch,
):
    monkeypatch.setattr(
        bills_common.congress_api, "api_get",
        _fake_api_get(
            "Dream Act", "Immigration", ["Immigration status and procedures"],
            [("2025-01-01", "Introduced summary"), ("2025-06-01", "Latest summary")],
        ),
    )

    bill_id = bills_common.sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )

    title, policy_area, crs_summary, _ = _get_bill_row(pg_conn, bill_id)
    assert title == "Dream Act"
    assert policy_area == "Immigration"
    assert crs_summary == "Latest summary"  # the later actionDate wins, not list order
    assert _get_bill_subjects(pg_conn, bill_id) == ["Immigration status and procedures"]


def test_sync_bill_refreshes_an_already_synced_bill_when_source_data_changed(
    pg_conn, test_bill_number, monkeypatch,
):
    # Regression test for rchacon/cd-platform#52: get_or_sync_bill() never
    # refreshed an already-synced bill. sync_bill is the refresh path
    # bills_etl now calls on a schedule -- a changed policy_area/subject
    # list on a bill already in the table must actually get picked up.
    monkeypatch.setattr(
        bills_common.congress_api, "api_get",
        _fake_api_get("Original Title", "Health", ["Health"], [("2025-01-01", "Original summary")]),
    )
    first_bill_id = bills_common.sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )
    _, _, _, first_updated_at = _get_bill_row(pg_conn, first_bill_id)

    time.sleep(0.01)
    monkeypatch.setattr(
        bills_common.congress_api, "api_get",
        _fake_api_get(
            "Reclassified Title", "Immigration", ["Immigration status and procedures"],
            [("2025-01-01", "Original summary"), ("2025-06-01", "Updated summary")],
        ),
    )
    second_bill_id = bills_common.sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )
    title, policy_area, crs_summary, second_updated_at = _get_bill_row(pg_conn, second_bill_id)

    assert second_bill_id == first_bill_id  # same row, refreshed in place -- not a duplicate
    assert title == "Reclassified Title"
    assert policy_area == "Immigration"
    assert crs_summary == "Updated summary"
    assert _get_bill_subjects(pg_conn, second_bill_id) == ["Immigration status and procedures"]
    assert second_updated_at > first_updated_at


def test_sync_bill_degrades_gracefully_when_summaries_fetch_fails(
    pg_conn, test_bill_number, monkeypatch,
):
    # Regression test: /summaries is metadata enrichment, not load-bearing
    # for bill_id the way detail/subjects are -- its failure must not sink
    # the whole sync (it did, when all three futures' .result() calls were
    # unguarded).
    def fake_api_get(session, url, params=None):
        if url.endswith("/subjects"):
            return {"subjects": {"legislativeSubjects": [{"name": "Health"}]}}
        if url.endswith("/summaries"):
            raise RuntimeError("simulated /summaries failure")
        return {
            "bill": {
                "congress": CONGRESS, "type": "HR", "number": str(test_bill_number),
                "title": "A Title", "policyArea": {"name": "Health"},
                "updateDate": "2025-01-01T00:00:00Z",
            }
        }

    monkeypatch.setattr(bills_common.congress_api, "api_get", fake_api_get)

    bill_id = bills_common.sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )

    title, policy_area, crs_summary, _ = _get_bill_row(pg_conn, bill_id)
    assert title == "A Title"
    assert policy_area == "Health"
    assert crs_summary is None
    assert _get_bill_subjects(pg_conn, bill_id) == ["Health"]


def test_sync_bill_preserves_prior_subjects_when_subjects_response_is_empty(
    pg_conn, test_bill_number, monkeypatch,
):
    # Regression test: an empty/degraded /subjects response on a refresh
    # must not wipe a bill's real, previously-synced subjects.
    monkeypatch.setattr(
        bills_common.congress_api, "api_get",
        _fake_api_get("A Title", "Health", ["Health", "Insurance"], []),
    )
    bill_id = bills_common.sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )
    assert _get_bill_subjects(pg_conn, bill_id) == ["Health", "Insurance"]

    monkeypatch.setattr(
        bills_common.congress_api, "api_get",
        _fake_api_get("A Title", "Health", [], []),
    )
    bills_common.sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )

    assert _get_bill_subjects(pg_conn, bill_id) == ["Health", "Insurance"]


def test_sync_bill_preserves_prior_values_when_refresh_returns_nulls(
    pg_conn, test_bill_number, monkeypatch,
):
    # Regression test: a refresh where title/policy_area/crs_summary all
    # come back null (a degraded fetch, not a real upstream change) must
    # not blank out a previously-synced bill's real values.
    monkeypatch.setattr(
        bills_common.congress_api, "api_get",
        _fake_api_get("A Title", "Health", ["Health"], [("2025-01-01", "A summary")]),
    )
    bill_id = bills_common.sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )

    monkeypatch.setattr(
        bills_common.congress_api, "api_get",
        _fake_api_get(None, None, ["Health"], []),
    )
    bills_common.sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )

    title, policy_area, crs_summary, _ = _get_bill_row(pg_conn, bill_id)
    assert title == "A Title"
    assert policy_area == "Health"
    assert crs_summary == "A summary"
