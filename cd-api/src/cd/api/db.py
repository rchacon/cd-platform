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


def fetch_members(
    state: str, chamber: str | None = None, district: int | None = None
) -> list[dict]:
    # Backs GET /members -- an "honest collection": every filter given is
    # AND'd, and a filter that's omitted simply doesn't constrain. No
    # `chamber = 'SENATE' OR district = %s` union trick: `filter[district]=5`
    # returns *only* House members in district 5, not "district 5 plus the
    # state's senators" (senators have district NULL, so `district = 5` is
    # never true for them -- SQL three-valued logic, no special-casing).
    # `filter[chamber]=house` folds in Delegates / the Resident
    # Commissioner (chamber HOUSE, member_type differs).
    #
    # `AND in_office`: current_members (cd-etl migration 0007) no longer
    # filters departed members out -- it exposes `in_office` instead, so
    # GET /members/{bioguide_id} can still serve them. This roster
    # endpoint is sitting-only, so it re-applies the filter here.
    #
    # ORDER BY: senators first, then House by district, bioguide_id as a
    # final deterministic tiebreak (two same-state senators, an at-large
    # 0 vs NULL) -- so the flat `data` list has a stable order the old
    # {senators, representatives} split gave for free.
    clauses = ["state = %(state)s", "in_office"]
    if chamber is not None:
        clauses.append("chamber = %(chamber)s")
    if district is not None:
        clauses.append("district = %(district)s")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM current_members
                WHERE {" AND ".join(clauses)}
                ORDER BY (chamber <> 'SENATE'), district NULLS FIRST, bioguide_id
                """,
                {"state": state, "chamber": chamber, "district": district},
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def fetch_member(bioguide_id: str) -> dict | None:
    # current_members (cd-etl migration 0007) is scoped to the current
    # Congress but NOT to still-seated members -- it carries `in_office`
    # instead. So a member who left mid-term is served here (with
    # in_office false) rather than 404'd, keeping a bookmarked
    # /members/{id} page resolving after a resignation. 404 is kept only
    # for an id with no current-Congress term at all.
    #
    # ORDER BY picks the term the member currently holds when a
    # mid-Congress chamber switch left two current-Congress rows:
    # `end_year DESC NULLS FIRST` prefers a still-open term; `start_year
    # DESC` breaks the tie in the window before Congress.gov reports an
    # endYear for the vacated seat (both rows end_year IS NULL); the PK
    # is a final deterministic tiebreaker.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM current_members
                WHERE bioguide_id = %(bioguide_id)s
                ORDER BY end_year DESC NULLS FIRST, start_year DESC, member_term_id DESC
                LIMIT 1
                """,
                {"bioguide_id": bioguide_id},
            )
            return cur.fetchone()
    finally:
        conn.close()


def _to_pgvector_literal(embedding: list[float]) -> str:
    # Bound as a plain string %s param and cast with ::vector in SQL --
    # no query here ever needs the pgvector Python package (and the
    # numpy dependency it pulls in) to deserialize a vector column back
    # into a float array, matching cd-etl's own db.to_pgvector_literal.
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


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


def fetch_member_votes(bioguide_id: str, bill_keys: list[str]) -> list[dict] | None:
    # Backs GET /members/{bioguide_id}/votes: every roll call this member
    # cast, across a caller-supplied set of bills keyed by canonical
    # bill_key ("119-hr-2616"). Returns one row per (bill, roll call,
    # member vote), plus the roll call's natural-key parts (chamber /
    # congress / session / vote_number) so the shaper can build the
    # "119-house-1-327" roll_call id.
    #
    # Returns None when the bioguide id has no current-Congress term at
    # all -- the route's 404, the same rule as GET /members/{bioguide_id}.
    # Checked in this same connection rather than via a separate
    # fetch_member() round trip (this path already opens an unpooled
    # connection per call -- see get_connection's TODO).
    #
    # Both joins are LEFT so a requested bill that exists but has no roll
    # call this member voted on still comes back -- one row with NULL
    # vote columns. The shaper turns that into a `meta.bills_without_votes`
    # entry rather than a resource, so the caller can tell "no recorded
    # vote" from "bill not synced" (the latter matches no row at all and
    # is simply absent). The bioguide filter sits in the join's ON
    # clause, not WHERE, so it doesn't collapse the outer join back to an
    # inner one for members who never voted.
    #
    # ORDER BY gives a stable oldest-first order within each bill (a bill
    # can have both a procedural motion and final passage on different
    # dates); cross-bill ordering is irrelevant -- the route regroups by
    # the caller's requested order.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM current_members WHERE bioguide_id = %(bioguide_id)s LIMIT 1",
                {"bioguide_id": bioguide_id},
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                """
                SELECT b.bill_key,
                       r.chamber, r.congress, r.session, r.vote_number,
                       r.vote_question, r.result, r.vote_date,
                       v.vote_cast
                FROM bills b
                LEFT JOIN roll_calls r ON r.bill_id = b.bill_id
                LEFT JOIN roll_call_member_votes v
                       ON v.roll_call_id = r.roll_call_id
                      AND v.bioguide_id = %(bioguide_id)s
                WHERE b.bill_key = ANY(%(bill_keys)s)
                ORDER BY r.vote_date, r.roll_call_id
                """,
                {"bioguide_id": bioguide_id, "bill_keys": bill_keys},
            )
            return list(cur.fetchall())
    finally:
        conn.close()
