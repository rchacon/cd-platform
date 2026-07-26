from __future__ import annotations

import os

import psycopg2
import psycopg2.extras

PG_DSN = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": os.environ.get("PGPORT", "5432"),
    "dbname": os.environ.get("PGDATABASE", "congressional_app"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", "postgres"),
}


def get_connection() -> psycopg2.extensions.connection:
    return psycopg2.connect(cursor_factory=psycopg2.extras.RealDictCursor, **PG_DSN)


def fetch_current_member_terms(state: str, district: int) -> list[dict]:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM current_member_terms
            WHERE state = %(state)s
              AND (chamber = 'SENATE' OR (chamber = 'HOUSE' AND district = %(district)s))
            """,
            {"state": state, "district": district},
        )
        return list(cur.fetchall())
