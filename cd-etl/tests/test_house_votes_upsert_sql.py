import os
import time
import uuid

import psycopg2
import pytest
from psycopg2.extras import execute_values

import house_votes_etl as etl

PG_DSN = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": os.environ.get("PGPORT", "5432"),
    "dbname": os.environ.get("PGDATABASE", "congressional_app_test"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", "postgres"),
}

# The 119th Congress is seeded by migration 0001, so bill/roll_call
# fixtures below don't need their own congresses row.
CONGRESS = 119


@pytest.fixture
def pg_conn():
    try:
        conn = psycopg2.connect(connect_timeout=3, **PG_DSN)
    except psycopg2.OperationalError as exc:
        pytest.skip(
            f"Postgres not reachable at {PG_DSN['host']}:{PG_DSN['port']} "
            f"(run `docker compose up -d postgres` to enable this test): {exc}"
        )
    yield conn
    conn.close()


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


def _random_number(low: int, high: int) -> int:
    return low + (uuid.uuid4().int % (high - low))


@pytest.fixture
def test_bill_number(pg_conn):
    # Kept well above any real bill's current range, and under
    # bill_number's SMALLINT max (32767).
    bill_number = _random_number(20000, 29000)
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
            etl.BILLS_UPSERT_SQL,
            (CONGRESS, "HR", test_bill_number, "Health", "hash-bill", None),
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
    vote_number = _random_number(20000, 29000)
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


def test_bills_upsert_returning_yields_bill_id_even_on_unchanged_conflict(
    pg_conn, test_bill_number,
):
    # Pins the deliberate difference from MEMBERS_UPSERT_SQL: this
    # ON CONFLICT is NOT WHERE-gated, so RETURNING always yields bill_id
    # even when re-run with an identical row.
    row = (CONGRESS, "HR", test_bill_number, "Health", "hash-a", None)
    with pg_conn.cursor() as cursor:
        cursor.execute(etl.BILLS_UPSERT_SQL, row)
        first_bill_id = cursor.fetchone()[0]
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute(etl.BILLS_UPSERT_SQL, row)
        second_bill_id = cursor.fetchone()[0]
    pg_conn.commit()

    assert first_bill_id == second_bill_id


def test_get_or_sync_bill_is_cached_after_first_sync(pg_conn, test_bill_number, monkeypatch):
    call_count = {"n": 0}

    def fake_api_get(session, url, params=None):
        call_count["n"] += 1
        if url.endswith("/subjects"):
            return {"subjects": {"legislativeSubjects": [{"name": "Health"}]}}
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
    assert calls_after_first_sync == 2  # one detail call, one subjects call

    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT subject_name FROM bill_subjects WHERE bill_id = %s", (first_bill_id,))
        assert [row[0] for row in cursor.fetchall()] == ["Health"]

    second_bill_id = etl.get_or_sync_bill(
        session=None, conn=pg_conn, congress=CONGRESS, bill_type="HR", bill_number=test_bill_number,
    )

    assert second_bill_id == first_bill_id
    assert call_count["n"] == calls_after_first_sync  # no new HTTP calls on the cache-hit path


def test_bill_subjects_delete_and_reinsert_replaces_prior_set(pg_conn, test_bill_id):
    # Exercises the delete+reinsert pattern get_or_sync_bill uses for a
    # bill's subjects directly, since get_or_sync_bill itself only ever
    # syncs a bill once (no resync path) and so never re-triggers this
    # through its own normal flow.
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.BILL_SUBJECTS_INSERT_SQL,
            [(test_bill_id, "Health"), (test_bill_id, "Insurance")],
        )
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute("DELETE FROM bill_subjects WHERE bill_id = %s", (test_bill_id,))
        execute_values(cursor, etl.BILL_SUBJECTS_INSERT_SQL, [(test_bill_id, "Tax Policy")])
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT subject_name FROM bill_subjects WHERE bill_id = %s", (test_bill_id,))
        assert [row[0] for row in cursor.fetchall()] == ["Tax Policy"]


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
    vote_number_a = _random_number(20000, 24000)
    vote_number_b = _random_number(24000, 28000)

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
