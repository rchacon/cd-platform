import os

# Dedicated test database (cd-platform#16) -- must be set before `cd.api.db` (or
# anything importing it, e.g. `app`) is first imported: db.py's PG_DSN is
# built from this env var at import time and used by the real app code
# under test too (via TestClient), not just this fixture's own direct
# seeding -- setting it only here, after import, would leave the app
# querying the real dev database while tests seed the test one.
os.environ.setdefault("CD_API_PGDATABASE", "congressional_app_test")
# The pool opens connections on demand (min_size=0), and nothing here runs
# queries concurrently, so a small cap is enough -- it keeps the number of
# test connections to the shared Postgres low.
os.environ.setdefault("CD_API_PG_POOL_SIZE", "2")

import uuid

import psycopg2
import pytest

from cd.api.db import PG_DSN


def random_number(low: int, high: int) -> int:
    # Shared by real-Postgres test modules that need a bill/vote number
    # kept well clear of any real value's current range. Matches
    # cd-etl's own tests/conftest.py helper of the same name.
    return low + (uuid.uuid4().int % (high - low))


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


@pytest.fixture
def client(pg_conn):
    # As a context manager, so cd.api's lifespan opens (and then closes) its
    # asyncpg pool on the TestClient's own event loop -- a bare
    # TestClient(app) never runs the lifespan, so any route that queries the
    # database would fail. Depends on pg_conn to skip without Postgres.
    from fastapi.testclient import TestClient

    from cd.api.app import app

    with TestClient(app) as client:
        yield client
