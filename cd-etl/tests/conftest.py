import os
import uuid

import psycopg2
import pytest

# members_etl reads this at import time; tests exercise pure data
# transforms and never make real API calls, so a placeholder is fine.
os.environ.setdefault("CONGRESS_API_KEY", "test-key")

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


def random_number(low: int, high: int) -> int:
    # Shared by real-Postgres test modules that need a bill/vote number
    # kept well clear of any real value's current range.
    return low + (uuid.uuid4().int % (high - low))
