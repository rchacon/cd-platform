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
    # TODO: opens a plain connection per call -- fine locally, but Lambda's
    # concurrency model can exhaust RDS's max_connections in production.
    # Front this with RDS Proxy (or a pooler) once running on AWS (see #4).
    return psycopg2.connect(cursor_factory=psycopg2.extras.RealDictCursor, **PG_DSN)


def fetch_current_members(state: str, district: int | None) -> list[dict]:
    # district=None omits any district match: `district = NULL` is never
    # true in SQL (three-valued logic), so a NULL parameter here naturally
    # yields senators only, with no special-casing needed. The HOUSE check
    # is redundant given chamber_type is a strict two-value enum -- once
    # chamber != 'SENATE' it's necessarily 'HOUSE'.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM current_members
                WHERE state = %(state)s
                  AND (chamber = 'SENATE' OR district = %(district)s)
                """,
                {"state": state, "district": district},
            )
            return list(cur.fetchall())
    finally:
        conn.close()
