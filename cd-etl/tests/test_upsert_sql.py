import time
import uuid
from datetime import datetime, timezone

import pytest
from psycopg2.extras import Json, execute_values

from cd.etl.dags import members_etl as etl

# pg_conn fixture lives in conftest.py, shared across every real-Postgres
# test module.


def _member_row(bioguide_id: str, source_hash: str, source_updated_at=None) -> tuple:
    return (
        bioguide_id, "Test", None, "Member", None, None,
        1970, None, None, None, None, Json([]), source_hash, source_updated_at,
    )


def _get_updated_at(pg_conn, bioguide_id: str):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM members WHERE bioguide_id = %s", (bioguide_id,)
        )
        return cursor.fetchone()[0]


def _get_row(pg_conn, bioguide_id: str):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT source_updated_at, updated_at FROM members WHERE bioguide_id = %s",
            (bioguide_id,),
        )
        return cursor.fetchone()


@pytest.fixture
def test_bioguide_id(pg_conn):
    bioguide_id = f"TEST{uuid.uuid4().hex[:8].upper()}"
    yield bioguide_id
    with pg_conn.cursor() as cursor:
        cursor.execute("DELETE FROM members WHERE bioguide_id = %s", (bioguide_id,))
    pg_conn.commit()


def test_members_upsert_skips_update_when_source_hash_unchanged(pg_conn, test_bioguide_id):
    with pg_conn.cursor() as cursor:
        execute_values(cursor, etl.MEMBERS_UPSERT_SQL, [_member_row(test_bioguide_id, "hash-a")])
    pg_conn.commit()
    first_updated_at = _get_updated_at(pg_conn, test_bioguide_id)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        execute_values(cursor, etl.MEMBERS_UPSERT_SQL, [_member_row(test_bioguide_id, "hash-a")])
    pg_conn.commit()
    second_updated_at = _get_updated_at(pg_conn, test_bioguide_id)

    assert second_updated_at == first_updated_at


def test_members_upsert_bumps_update_when_source_hash_changed(pg_conn, test_bioguide_id):
    with pg_conn.cursor() as cursor:
        execute_values(cursor, etl.MEMBERS_UPSERT_SQL, [_member_row(test_bioguide_id, "hash-a")])
    pg_conn.commit()
    first_updated_at = _get_updated_at(pg_conn, test_bioguide_id)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        execute_values(cursor, etl.MEMBERS_UPSERT_SQL, [_member_row(test_bioguide_id, "hash-b")])
    pg_conn.commit()
    second_updated_at = _get_updated_at(pg_conn, test_bioguide_id)

    assert second_updated_at > first_updated_at


def test_members_upsert_advances_source_updated_at_without_hash_change(pg_conn, test_bioguide_id):
    # Regression test: source_updated_at previously only advanced when
    # source_hash changed too, so it could permanently lag behind the
    # source and cause a member to be re-fetched forever (source_hash
    # doesn't cover every field the source's updateDate can reflect).
    # It should always mirror the source's reported value, while
    # updated_at stays frozen unless real content (source_hash) changed.
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, tzinfo=timezone.utc)

    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.MEMBERS_UPSERT_SQL,
            [_member_row(test_bioguide_id, "hash-a", t1)],
        )
    pg_conn.commit()
    first_source_updated_at, first_updated_at = _get_row(pg_conn, test_bioguide_id)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.MEMBERS_UPSERT_SQL,
            [_member_row(test_bioguide_id, "hash-a", t2)],  # same hash, newer timestamp
        )
    pg_conn.commit()
    second_source_updated_at, second_updated_at = _get_row(pg_conn, test_bioguide_id)

    assert first_source_updated_at == t1
    assert second_source_updated_at == t2
    assert second_updated_at == first_updated_at


def test_members_needing_sync_works_with_real_postgres_driver_datetimes(
    pg_conn, test_bioguide_id
):
    # Regression/coverage test: _members_needing_sync's unit tests only
    # ever pass hand-built, already-timezone-aware datetimes. This
    # exercises the real producer path -- a TIMESTAMPTZ column read
    # back via psycopg2 -- feeding directly into the same comparison,
    # so a future driver/column-type change that started returning
    # naive datetimes (which would raise TypeError comparing against
    # _parse_timestamp's aware output) would fail this test.
    stored_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.MEMBERS_UPSERT_SQL,
            [_member_row(test_bioguide_id, "hash-a", stored_at)],
        )
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT bioguide_id, source_updated_at FROM members")
        stored_updated_at = dict(cursor.fetchall())

    unchanged_summary = [{"bioguideId": test_bioguide_id, "updateDate": "2026-01-01T00:00:00Z"}]
    changed_summary = [{"bioguideId": test_bioguide_id, "updateDate": "2026-06-01T00:00:00Z"}]

    assert etl._members_needing_sync(
        unchanged_summary, stored_updated_at, bioguide_ids_with_current_term={test_bioguide_id},
    ) == []
    assert etl._members_needing_sync(
        changed_summary, stored_updated_at, bioguide_ids_with_current_term={test_bioguide_id},
    ) == [test_bioguide_id]


@pytest.fixture
def current_congress_number(pg_conn):
    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT current_congress()")
        return cursor.fetchone()[0]


@pytest.fixture
def current_year(pg_conn):
    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT EXTRACT(YEAR FROM CURRENT_DATE)::int")
        return cursor.fetchone()[0]


def _insert_member_term(pg_conn, bioguide_id: str, congress: int, end_year) -> None:
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.MEMBERS_UPSERT_SQL,
            [_member_row(bioguide_id, f"hash-{bioguide_id}")],
        )
        # member_terms rows cascade-delete via members' ON DELETE CASCADE,
        # so the test_bioguide_id fixture's cleanup covers both tables.
        cursor.execute(
            """
            INSERT INTO member_terms (
                bioguide_id, congress, chamber, member_type, state, district,
                start_year, end_year, source_hash
            ) VALUES (%s, %s, 'SENATE', 'Senator', 'ZZ', NULL, 2023, %s, %s)
            """,
            (bioguide_id, congress, end_year, f"hash-term-{bioguide_id}"),
        )


def _get_crosswalk(pg_conn, bioguide_id: str):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT lis_member_id, senate_state_rank, updated_at FROM members WHERE bioguide_id = %s",
            (bioguide_id,),
        )
        return cursor.fetchone()


def test_legislators_crosswalk_update_skips_when_unchanged(pg_conn, test_bioguide_id):
    with pg_conn.cursor() as cursor:
        execute_values(cursor, etl.MEMBERS_UPSERT_SQL, [_member_row(test_bioguide_id, "hash-a")])
        execute_values(
            cursor, etl.LEGISLATORS_CROSSWALK_UPDATE_SQL,
            [(test_bioguide_id, "S275", "JUNIOR")],
        )
    pg_conn.commit()
    _, _, first_updated_at = _get_crosswalk(pg_conn, test_bioguide_id)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.LEGISLATORS_CROSSWALK_UPDATE_SQL,
            [(test_bioguide_id, "S275", "JUNIOR")],
        )
    pg_conn.commit()
    lis_member_id, senate_state_rank, second_updated_at = _get_crosswalk(pg_conn, test_bioguide_id)

    assert (lis_member_id, senate_state_rank) == ("S275", "JUNIOR")
    assert second_updated_at == first_updated_at


def test_legislators_crosswalk_update_bumps_when_changed(pg_conn, test_bioguide_id):
    with pg_conn.cursor() as cursor:
        execute_values(cursor, etl.MEMBERS_UPSERT_SQL, [_member_row(test_bioguide_id, "hash-a")])
        execute_values(
            cursor, etl.LEGISLATORS_CROSSWALK_UPDATE_SQL,
            [(test_bioguide_id, "S275", "JUNIOR")],
        )
    pg_conn.commit()
    _, _, first_updated_at = _get_crosswalk(pg_conn, test_bioguide_id)

    time.sleep(0.01)
    with pg_conn.cursor() as cursor:
        # Re-elected to a senior seat, or corrected data -- state_rank
        # changes for the same bioguide_id/lis_member_id.
        execute_values(
            cursor, etl.LEGISLATORS_CROSSWALK_UPDATE_SQL,
            [(test_bioguide_id, "S275", "SENIOR")],
        )
    pg_conn.commit()
    lis_member_id, senate_state_rank, second_updated_at = _get_crosswalk(pg_conn, test_bioguide_id)

    assert senate_state_rank == "SENIOR"
    assert second_updated_at > first_updated_at


def test_legislators_crosswalk_update_noops_for_unknown_bioguide_id(pg_conn):
    # This is a plain guarded UPDATE, not an upsert -- a crosswalk entry
    # for a bioguide_id not yet in members must match zero rows, not
    # raise or create one (member creation stays MEMBERS_UPSERT_SQL's job).
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.LEGISLATORS_CROSSWALK_UPDATE_SQL,
            [("NOTREAL01", "S001", "JUNIOR")],
        )
    pg_conn.commit()  # no error


class _NonClosingConnWrapper:
    # load()'s finally block closes the connection it got from
    # hook.get_conn() -- this wrapper lets a test hand load() the shared
    # pg_conn fixture's real connection without that close() call
    # breaking the fixture's own teardown/later assertions.
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


def test_load_crosswalk_raises_instead_of_swallowing_and_does_not_affect_members(
    pg_conn, test_bioguide_id, monkeypatch,
):
    # Regression test: load_crosswalk() (split out of load() into its own
    # @task specifically so Airflow's own default_args={"retries": 2}
    # actually applies to it) must let a failure propagate rather than
    # catching and just logging it -- a swallowed exception would never
    # get retried the way a genuine task failure does. Still can't affect
    # members/terms: load() (called first here, mirroring the DAG's
    # load(...) >> load_crosswalk(...) wiring) already committed that
    # data in a separate task before load_crosswalk ever runs, so a
    # crosswalk-only failure has nothing of load()'s to roll back.
    monkeypatch.setattr(etl, "PostgresHook", lambda postgres_conn_id: _RealConnHook(pg_conn))

    dag = etl.congress_members_etl()
    load = dag.task_dict["load"].python_callable
    load_crosswalk = dag.task_dict["load_crosswalk"].python_callable

    # Unwrapped shape (plain list for party_history, not Json(...)) --
    # this is transform()'s actual output shape; load() does the Json(...)
    # wrapping itself via _wrap_party_history_for_insert.
    member_row = (
        test_bioguide_id, "Test", None, "Member", None, None,
        1970, None, None, None, None, [], f"hash-{test_bioguide_id}", None,
    )
    rows = {
        "members": [member_row],
        "terms": [],
        # A hand-built bad row, bypassing _crosswalk_row's own
        # SENATE_STATE_RANKS validation -- exercises load_crosswalk's own
        # failure handling directly, regardless of how unlikely that
        # validation makes this in the real pipeline.
        "crosswalk": [(test_bioguide_id, "S999", "NOT_A_VALID_RANK")],
    }

    load(rows)

    with pytest.raises(Exception):
        load_crosswalk(rows)
    # load_crosswalk's own finally block closes its connection on this
    # path in production (hook.get_conn() returns a fresh one every
    # call), which discards the aborted transaction with it -- this test
    # reuses pg_conn via _NonClosingConnWrapper instead, so it needs an
    # explicit rollback here to reach the same clean state before
    # querying again.
    pg_conn.rollback()

    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT given_name, lis_member_id FROM members WHERE bioguide_id = %s",
            (test_bioguide_id,),
        )
        given_name, lis_member_id = cursor.fetchone()

    assert given_name == "Test"  # members upsert committed independently, in a prior task
    assert lis_member_id is None  # crosswalk update itself rolled back, never applied


def test_current_members_exposes_state_rank(
    pg_conn, test_bioguide_id, current_congress_number,
):
    _insert_member_term(pg_conn, test_bioguide_id, current_congress_number, None)
    with pg_conn.cursor() as cursor:
        execute_values(
            cursor, etl.LEGISLATORS_CROSSWALK_UPDATE_SQL,
            [(test_bioguide_id, "S001", "SENIOR")],
        )
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT state_rank FROM current_members WHERE bioguide_id = %s", (test_bioguide_id,),
        )
        assert cursor.fetchone()[0] == "SENIOR"


def _in_office(pg_conn, bioguide_id):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT in_office FROM current_members WHERE bioguide_id = %s", (bioguide_id,)
        )
        row = cursor.fetchone()
    return row and row[0]


def test_current_members_flags_prior_year_end_year_as_not_in_office(
    pg_conn, test_bioguide_id, current_congress_number, current_year
):
    # A member who left the current Congress last year: still in the view
    # (0007 dropped the currency filter), flagged in_office = false.
    _insert_member_term(pg_conn, test_bioguide_id, current_congress_number, current_year - 1)
    pg_conn.commit()

    assert _in_office(pg_conn, test_bioguide_id) is False


def test_current_members_flags_null_end_year_as_in_office(
    pg_conn, test_bioguide_id, current_congress_number
):
    _insert_member_term(pg_conn, test_bioguide_id, current_congress_number, None)
    pg_conn.commit()

    assert _in_office(pg_conn, test_bioguide_id) is True


def test_current_members_flags_current_year_end_year_as_in_office(
    pg_conn, test_bioguide_id, current_congress_number, current_year
):
    # Pins the known, tracked limitation (issue #14): year-only precision
    # can't distinguish "departed earlier this year" from "still serving
    # the rest of this year," so a same-year departure still reads as
    # in_office. Not a bug -- this test exists so a future accidental
    # tightening of the expression doesn't silently change it.
    _insert_member_term(pg_conn, test_bioguide_id, current_congress_number, current_year)
    pg_conn.commit()

    assert _in_office(pg_conn, test_bioguide_id) is True
