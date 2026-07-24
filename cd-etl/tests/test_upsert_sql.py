import os
import time
import uuid

import psycopg2
import pytest
from psycopg2.extras import Json, execute_values

import members_etl as etl

PG_DSN = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": os.environ.get("PGPORT", "5432"),
    "dbname": os.environ.get("PGDATABASE", "congressional_app"),
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


def _member_row(bioguide_id: str, source_hash: str) -> tuple:
    return (
        bioguide_id, "Test", None, "Member", None, None,
        1970, None, None, None, None, Json([]), source_hash, None,
    )


def _get_updated_at(pg_conn, bioguide_id: str):
    with pg_conn.cursor() as cursor:
        cursor.execute(
            "SELECT updated_at FROM members WHERE bioguide_id = %s", (bioguide_id,)
        )
        return cursor.fetchone()[0]


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
