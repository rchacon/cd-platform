import os
import time
import uuid
from datetime import datetime, timezone

import psycopg2
import pytest
from psycopg2.extras import Json, execute_values

import members_etl as etl

PG_DSN = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": os.environ.get("PGPORT", "5432"),
    # Dedicated test database (cd-platform#16) -- kept as the default even
    # outside `make test-etl` (which sets PGDATABASE explicitly), so a
    # stray bare pytest invocation still lands on the safe, isolated
    # database rather than real dev-seeded data.
    "dbname": os.environ.get("PGDATABASE", "congressional_app_test"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", "postgres"),
}


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


def test_current_members_excludes_prior_year_end_year(
    pg_conn, test_bioguide_id, current_congress_number, current_year
):
    _insert_member_term(pg_conn, test_bioguide_id, current_congress_number, current_year - 1)
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM current_members WHERE bioguide_id = %s", (test_bioguide_id,))
        assert cursor.fetchone() is None


def test_current_members_includes_null_end_year(pg_conn, test_bioguide_id, current_congress_number):
    _insert_member_term(pg_conn, test_bioguide_id, current_congress_number, None)
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM current_members WHERE bioguide_id = %s", (test_bioguide_id,))
        assert cursor.fetchone() is not None


def test_current_members_includes_current_year_end_year(
    pg_conn, test_bioguide_id, current_congress_number, current_year
):
    # Pins the known, tracked limitation (issue #14): year-only precision
    # can't distinguish "departed earlier this year" from "still serving
    # the rest of this year," so a same-year departure is still included.
    # Not a bug -- this test exists so a future accidental tightening of
    # the filter doesn't silently change this documented behavior.
    _insert_member_term(pg_conn, test_bioguide_id, current_congress_number, current_year)
    pg_conn.commit()

    with pg_conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM current_members WHERE bioguide_id = %s", (test_bioguide_id,))
        assert cursor.fetchone() is not None
