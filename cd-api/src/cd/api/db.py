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


def _to_pgvector_literal(embedding: list[float]) -> str:
    # Bound as a plain string %s param and cast with ::vector in SQL --
    # no query here ever needs the pgvector Python package (and the
    # numpy dependency it pulls in) to deserialize a vector column back
    # into a float array, matching cd-etl's own db.to_pgvector_literal.
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


def member_exists(bioguide_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM members WHERE bioguide_id = %s", (bioguide_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def fetch_closest_vocab_term(embedding: list[float]) -> dict | None:
    # No index on vocab_term_embeddings (see migration 0005) -- a brute
    # force scan is exact and fast enough at this table's expected scale
    # (a few hundred distinct terms).
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT kind, term, embedding <=> %(embedding)s::vector AS distance
                FROM vocab_term_embeddings
                ORDER BY distance ASC
                LIMIT 1
                """,
                {"embedding": _to_pgvector_literal(embedding)},
            )
            return cur.fetchone()
    finally:
        conn.close()


def fetch_bills_by_policy_area(term: str, limit: int) -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bill_id, bill_key, congress, bill_type, bill_number, title,
                       policy_area, crs_summary
                FROM bills
                WHERE policy_area = %(term)s
                LIMIT %(limit)s
                """,
                {"term": term, "limit": limit},
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def fetch_bills_by_subject(term: str, limit: int) -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.bill_id, b.bill_key, b.congress, b.bill_type, b.bill_number, b.title,
                       b.policy_area, b.crs_summary
                FROM bills b
                JOIN bill_subjects s ON s.bill_id = b.bill_id
                WHERE s.subject_name = %(term)s
                LIMIT %(limit)s
                """,
                {"term": term, "limit": limit},
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def fetch_bills_by_similarity(
    embedding: list[float], exclude_bill_ids: list[int], limit: int, max_distance: float
) -> list[dict]:
    # max_distance is a relevance floor -- without it this always pads
    # out to `limit` with whatever's *least* far, even when nothing in
    # the corpus is genuinely related to the query (confirmed empirically:
    # a query with no genuine match in a small local corpus still
    # returned `limit` bills, all filler). Filtered in SQL rather than
    # in Python so an empty result short-circuits the query itself.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH scored AS (
                    SELECT bill_id, bill_key, congress, bill_type, bill_number, title,
                           policy_area, crs_summary,
                           crs_summary_embedding <=> %(embedding)s::vector AS distance
                    FROM bills
                    WHERE crs_summary_embedding IS NOT NULL
                      AND NOT (bill_id = ANY(%(exclude_bill_ids)s))
                )
                SELECT bill_id, bill_key, congress, bill_type, bill_number, title,
                       policy_area, crs_summary
                FROM scored
                WHERE distance <= %(max_distance)s
                ORDER BY distance ASC
                LIMIT %(limit)s
                """,
                {
                    "embedding": _to_pgvector_literal(embedding),
                    "exclude_bill_ids": exclude_bill_ids,
                    "limit": limit,
                    "max_distance": max_distance,
                },
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def fetch_votes_for_bills(bill_ids: list[int], bioguide_id: str) -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.bill_id, v.vote_cast, r.vote_question, r.result, r.vote_date
                FROM roll_calls r
                JOIN roll_call_member_votes v ON v.roll_call_id = r.roll_call_id
                WHERE r.bill_id = ANY(%(bill_ids)s) AND v.bioguide_id = %(bioguide_id)s
                """,
                {"bill_ids": bill_ids, "bioguide_id": bioguide_id},
            )
            return list(cur.fetchall())
    finally:
        conn.close()
