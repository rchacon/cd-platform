import time
import uuid

import pytest
from psycopg2.extras import execute_values

import bills_common
import house_votes_etl as etl
from conftest import random_number

# The 119th Congress is seeded by migration 0001, so bill/roll_call
# fixtures below don't need their own congresses row.
CONGRESS = 119

# pg_conn fixture lives in conftest.py, shared across every real-Postgres
# test module.


class _NonClosingConnWrapper:
    # load()'s finally block closes the connection it got from
    # hook.get_conn() -- this wrapper lets tests hand load() the shared
    # pg_conn fixture's real connection without that close() call
    # breaking the fixture's own teardown/later assertions in the test.
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        pass


class _RealConnHook:
    def __init__(self, conn):
        self._conn = conn

    def get_conn(self):
        return _NonClosingConnWrapper(self._conn)


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


@pytest.fixture
def test_bioguide_id(pg_conn):
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO members (bioguide_id, given_name, family_name, source_hash) "
            "VALUES (%s, 'Test', 'Member', 'hash-member')",
            (bioguide_id,),
        )
    pg_conn.commit()
    yield bioguide_id
    with pg_conn.cursor() as cursor:
        cursor.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
    pg_conn.commit()


@pytest.fixture
def test_vote_number(pg_conn):
    vote_number = random_number(20000, 29000)
    yield vote_number
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM roll_calls WHERE chamber = 'HOUSE' AND congress = %s AND vote_number = %s",
            (CONGRESS, vote_number),
        )
    pg_conn.commit()


def _roll_call_row(bill_id, vote_number, source_hash, session=1):
    return (
        "HOUSE", CONGRESS, session, vote_number, bill_id,
        "On Passage", "Passed", "2025-09-08", source_hash,
    )


def _get_roll_call_updated_at(pg_conn, vote_number):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM roll_calls "
            "WHERE chamber = 'HOUSE' AND congress = %s AND session = 1 AND vote_number = %s",
            (CONGRESS, vote_number),
        )
        return cursor.fetchone()[0]


def _get_roll_call_id(pg_conn, vote_number):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT roll_call_id FROM roll_calls "
            "WHERE chamber = 'HOUSE' AND congress = %s AND session = 1 AND vote_number = %s",
            (CONGRESS, vote_number),
        )
        return cursor.fetchone()[0]


def _get_member_vote_updated_at(pg_conn, roll_call_id, bioguide_id):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM roll_call_member_votes "
            "WHERE roll_call_id = %s AND bioguide_id = %s",
            (roll_call_id, bioguide_id),
        )
        return cursor.fetchone()[0]


def test_get_or_sync_bill_is_cached_after_first_sync(pg_conn, test_bill_number, monkeypatch):
    # bills_common.sync_bill's own fetch+upsert behavior (including the
    # 3-way detail/subjects/summaries fetch) is covered directly by
    # test_bills_common.py -- this test only pins get_or_sync_bill's own
    # cache-check layer: a hit must short-circuit before ever calling
    # sync_bill (and therefore before any HTTP call at all).
    call_count = {"n": 0}

    def fake_api_get(session, url, params=None):
        call_count["n"] += 1
        if url.endswith("/subjects"):
            return {"subjects": {"legislativeSubjects": [{"name": "Health"}]}}
        if url.endswith("/summaries"):
            return {"summaries": []}
        return {
            "bill": {
                "congress": CONGRESS, "type": "HR", "number": str(test_bill_number),
                "policyArea": {"name": "Health"}, "updateDate": "2025-01-01T00:00:00Z",
            }
        }

    monkeypatch.setattr(etl.congress_api, "api_get", fake_api_get)

    first_bill_id = etl.get_or_sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )
    calls_after_first_sync = call_count["n"]
    assert calls_after_first_sync == 3  # detail, subjects, and summaries calls

    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT subject_name FROM bill_subjects WHERE bill_id = %s", (first_bill_id,))
        assert [row[0] for row in cursor.fetchall()] == ["Health"]

    second_bill_id = etl.get_or_sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )

    assert second_bill_id == first_bill_id
    assert call_count["n"] == calls_after_first_sync  # no new HTTP calls on the cache-hit path


def test_roll_calls_upsert_skips_update_when_source_hash_unchanged(
    pg_conn, test_bill_id, test_vote_number,
):
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALLS_UPSERT_SQL,
            [_roll_call_row(test_bill_id, test_vote_number, "hash-a")],
        )
    pg_conn.commit()
    first_updated_at = _get_roll_call_updated_at(pg_conn, test_vote_number)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALLS_UPSERT_SQL,
            [_roll_call_row(test_bill_id, test_vote_number, "hash-a")],
        )
    pg_conn.commit()
    second_updated_at = _get_roll_call_updated_at(pg_conn, test_vote_number)

    assert second_updated_at == first_updated_at


def test_roll_calls_upsert_bumps_update_when_source_hash_changed(
    pg_conn, test_bill_id, test_vote_number,
):
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALLS_UPSERT_SQL,
            [_roll_call_row(test_bill_id, test_vote_number, "hash-a")],
        )
    pg_conn.commit()
    first_updated_at = _get_roll_call_updated_at(pg_conn, test_vote_number)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALLS_UPSERT_SQL,
            [_roll_call_row(test_bill_id, test_vote_number, "hash-b")],
        )
    pg_conn.commit()
    second_updated_at = _get_roll_call_updated_at(pg_conn, test_vote_number)

    assert second_updated_at > first_updated_at


def test_roll_call_member_votes_upsert_skips_update_when_vote_cast_unchanged(
    pg_conn, test_bill_id, test_vote_number, test_bioguide_id,
):
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALLS_UPSERT_SQL,
            [_roll_call_row(test_bill_id, test_vote_number, "hash-a")],
        )
    pg_conn.commit()
    roll_call_id = _get_roll_call_id(pg_conn, test_vote_number)

    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALL_MEMBER_VOTES_UPSERT_SQL, [(roll_call_id, test_bioguide_id, "YEA")],
        )
    pg_conn.commit()
    first_updated_at = _get_member_vote_updated_at(pg_conn, roll_call_id, test_bioguide_id)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALL_MEMBER_VOTES_UPSERT_SQL, [(roll_call_id, test_bioguide_id, "YEA")],
        )
    pg_conn.commit()
    second_updated_at = _get_member_vote_updated_at(pg_conn, roll_call_id, test_bioguide_id)

    assert second_updated_at == first_updated_at


def test_roll_call_member_votes_upsert_bumps_update_when_vote_cast_changed(
    pg_conn, test_bill_id, test_vote_number, test_bioguide_id,
):
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALLS_UPSERT_SQL,
            [_roll_call_row(test_bill_id, test_vote_number, "hash-a")],
        )
    pg_conn.commit()
    roll_call_id = _get_roll_call_id(pg_conn, test_vote_number)

    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALL_MEMBER_VOTES_UPSERT_SQL, [(roll_call_id, test_bioguide_id, "YEA")],
        )
    pg_conn.commit()
    first_updated_at = _get_member_vote_updated_at(pg_conn, roll_call_id, test_bioguide_id)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.ROLL_CALL_MEMBER_VOTES_UPSERT_SQL, [(roll_call_id, test_bioguide_id, "NAY")],
        )
    pg_conn.commit()
    second_updated_at = _get_member_vote_updated_at(pg_conn, roll_call_id, test_bioguide_id)

    assert second_updated_at > first_updated_at


def test_load_attributes_member_votes_to_the_correct_roll_call(
    pg_conn, test_bill_id, test_bioguide_id, monkeypatch,
):
    # Guards against a key-mixup bug in load()'s natural-key ->
    # roll_call_id join: two roll calls upserted in one batch must each
    # get their own, correctly-attributed member votes.
    vote_number_a = random_number(20000, 24000)
    vote_number_b = random_number(24000, 28000)

    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: _RealConnHook(pg_conn))

    dag = etl.house_votes_etl()
    load = dag.task_dict["load"].python_callable

    rows = {
        "roll_calls": [
            _roll_call_row(test_bill_id, vote_number_a, "hash-a"),
            _roll_call_row(test_bill_id, vote_number_b, "hash-b"),
        ],
        "member_votes": [
            {"key": [1, vote_number_a], "casts": [(test_bioguide_id, "YEA")]},
            {"key": [1, vote_number_b], "casts": [(test_bioguide_id, "NAY")]},
        ],
    }

    try:
        load(rows)

        with pg_conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT rc.vote_number, rcmv.vote_cast
                FROM roll_call_member_votes rcmv
                JOIN roll_calls rc ON rc.roll_call_id = rcmv.roll_call_id
                WHERE rc.chamber = 'HOUSE' AND rc.congress = %s
                  AND rc.vote_number IN (%s, %s)
                """,
                (CONGRESS, vote_number_a, vote_number_b),
            )
            results = dict(cursor.fetchall())

        assert results[vote_number_a] == "YEA"
        assert results[vote_number_b] == "NAY"
    finally:
        with pg_conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM roll_calls WHERE chamber = 'HOUSE' AND congress = %s "
                "AND vote_number IN (%s, %s)",
                (CONGRESS, vote_number_a, vote_number_b),
            )
        pg_conn.commit()


def test_load_chunk_failure_does_not_block_other_chunks(
    pg_conn, test_bill_id, test_bioguide_id, monkeypatch,
):
    # Regression test for the chunked-commit design: a bad row in one
    # chunk must not roll back roll calls already committed in an
    # earlier chunk, and must not prevent other chunks from loading.
    monkeypatch.setattr(etl, "LOAD_CHUNK_SIZE", 2)
    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: _RealConnHook(pg_conn))

    good_vote_number_1 = random_number(20000, 22000)
    good_vote_number_2 = random_number(22000, 24000)
    bad_vote_number = random_number(24000, 26000)
    nonexistent_bill_id = 999_999_999  # violates roll_calls_bill_congress_fk

    dag = etl.house_votes_etl()
    load = dag.task_dict["load"].python_callable

    rows = {
        "roll_calls": [
            _roll_call_row(test_bill_id, good_vote_number_1, "hash-good-1"),   # chunk 1
            _roll_call_row(test_bill_id, good_vote_number_2, "hash-good-2"),   # chunk 1
            _roll_call_row(nonexistent_bill_id, bad_vote_number, "hash-bad"),  # chunk 2, fails
        ],
        "member_votes": [
            {"key": [1, good_vote_number_1], "casts": [(test_bioguide_id, "YEA")]},
            {"key": [1, good_vote_number_2], "casts": [(test_bioguide_id, "NAY")]},
            {"key": [1, bad_vote_number], "casts": [(test_bioguide_id, "YEA")]},
        ],
    }

    try:
        load(rows)

        with pg_conn.cursor() as cursor:
            cursor.execute(
                "SELECT vote_number FROM roll_calls WHERE chamber = 'HOUSE' AND congress = %s "
                "AND vote_number IN (%s, %s, %s)",
                (CONGRESS, good_vote_number_1, good_vote_number_2, bad_vote_number),
            )
            landed_vote_numbers = {row[0] for row in cursor.fetchall()}

        # Chunk 1's two good roll calls landed; chunk 2's bad one didn't.
        assert landed_vote_numbers == {good_vote_number_1, good_vote_number_2}

        with pg_conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) FROM roll_call_member_votes rcmv
                JOIN roll_calls rc ON rc.roll_call_id = rcmv.roll_call_id
                WHERE rc.chamber = 'HOUSE' AND rc.congress = %s
                  AND rc.vote_number IN (%s, %s)
                """,
                (CONGRESS, good_vote_number_1, good_vote_number_2),
            )
            assert cursor.fetchone()[0] == 2
    finally:
        with pg_conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM roll_calls WHERE chamber = 'HOUSE' AND congress = %s "
                "AND vote_number IN (%s, %s, %s)",
                (CONGRESS, good_vote_number_1, good_vote_number_2, bad_vote_number),
            )
        pg_conn.commit()
