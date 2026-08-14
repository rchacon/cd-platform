import os

# Dedicated test database (cd-platform#16) -- must be set before `db` (or
# anything importing it, e.g. `app`) is first imported: db.py's PG_DSN is
# built from this env var at import time and used by the real app code
# under test too (via TestClient), not just this fixture's own direct
# seeding -- setting it only here, after import, would leave the app
# querying the real dev database while tests seed the test one.
os.environ.setdefault("PGDATABASE", "congressional_app_test")

import psycopg2
import pytest

from cd.api.db import PG_DSN


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
