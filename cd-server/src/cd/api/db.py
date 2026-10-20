from __future__ import annotations

import logging
import os
from typing import Any

import asyncpg
from cd.lib.sql import named

logger = logging.getLogger(__name__)

# CD_API_-prefixed, not the plain libpq PG* names: this package now runs
# inside cd-server, whose own settings.py already owns PGHOST/PGDATABASE/...
# for its cd_customers database. This one reads congressional_app (cd-etl's
# schema) under its own credentials, so the two can't share variables.
PG_DSN = {
    "host": os.environ.get("CD_API_PGHOST", "localhost"),
    "port": os.environ.get("CD_API_PGPORT", "5432"),
    "database": os.environ.get("CD_API_PGDATABASE", "congressional_app"),
    "user": os.environ.get("CD_API_PGUSER", "postgres"),
    "password": os.environ.get("CD_API_PGPASSWORD", "postgres"),
}

# min_size=0: the pool opens no connections at startup, so an unreachable
# congressional_app fails /v1 requests rather than cd-server's boot (which
# would take GraphQL and /health down with it). connect() probes it once
# and logs a warning instead. asyncpg's acquire() waits for a free connection rather
# than raising, so a burst queues instead of failing.
POOL_SIZE = int(os.environ.get("CD_API_PG_POOL_SIZE") or "5")
# Bounds each connection attempt, so an unreachable host (a security-group
# drop, a failover in progress) fails in seconds rather than after the OS's
# TCP timeout.
CONNECT_TIMEOUT_SECONDS = 10
# Bounds the wait for a free connection under a burst (acquire), and each
# query (command_timeout). Exceeding either raises TimeoutError, which the
# app's catch-all handler turns into a 500 rather than a request hanging
# indefinitely.
ACQUIRE_TIMEOUT_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 15
# Closes connections idle longer than this; with min_size=0 they're only
# reopened on demand. This bounds how long a connection sits unused, but it
# doesn't detect one a failover has already cut -- that still fails the
# request that gets it.
MAX_INACTIVE_CONNECTION_LIFETIME_SECONDS = 300

_pool: asyncpg.Pool | None = None


async def connect() -> None:
    # Called from cd-server's lifespan startup (and cd.api's own lifespan
    # when the app runs standalone, e.g. its tests). An asyncpg pool belongs
    # to the event loop it was created on, so it has to be opened by the
    # loop that serves requests -- never lazily or at import.
    global _pool
    _pool = await asyncpg.create_pool(
        min_size=0,
        max_size=POOL_SIZE,
        timeout=CONNECT_TIMEOUT_SECONDS,
        command_timeout=COMMAND_TIMEOUT_SECONDS,
        max_inactive_connection_lifetime=MAX_INACTIVE_CONNECTION_LIFETIME_SECONDS,
        **PG_DSN,
    )
    try:
        async with _pool.acquire(timeout=CONNECT_TIMEOUT_SECONDS) as conn:
            await conn.execute("SELECT 1")
    except Exception:
        logger.warning(
            "cd.api can't reach its database at startup -- /v1 requests will "
            "fail until it's reachable",
            exc_info=True,
        )


async def close() -> None:
    # Called from the same lifespans' shutdown. Reset to None so a later
    # connect() (the next TestClient lifespan, in tests) starts clean.
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("cd.api.db.connect() was never called")
    return _pool


def _acquire():
    return _get_pool().acquire(timeout=ACQUIRE_TIMEOUT_SECONDS)


async def _fetch(query: str, params: dict[str, Any]) -> list[dict]:
    # dict(), not asyncpg.Record: the shapers in transform.py and the routes
    # (bills.py adds a "matches" key) work with plain mutable dicts.
    async with _acquire() as conn:
        return [dict(row) for row in await conn.fetch(*named(query, params))]


async def _fetchrow(query: str, params: dict[str, Any]) -> dict | None:
    async with _acquire() as conn:
        row = await conn.fetchrow(*named(query, params))
    return dict(row) if row is not None else None


async def fetch_members(
    state: str | None = None,
    chamber: str | None = None,
    district: int | None = None,
    name: str | None = None,
) -> list[dict]:
    # Backs GET /members -- an "honest collection": every filter given is
    # AND'd, and a filter that's omitted simply doesn't constrain. No
    # `chamber = 'SENATE' OR district = :district` union trick: `filter[district]=5`
    # returns *only* House members in district 5, not "district 5 plus the
    # state's senators" (senators have district NULL, so `district = 5` is
    # never true for them -- SQL three-valued logic, no special-casing).
    # `filter[chamber]=house` folds in Delegates / the Resident
    # Commissioner (chamber HOUSE, member_type differs).
    #
    # `name`: every whitespace-separated word must appear (case-insensitive
    # substring) in the given name, nickname or family name, so "bernie
    # sanders" matches given_name Bernard via his nickname, and "pelosi"
    # alone matches too. LIKE's own wildcards are escaped, so a `%` or `_`
    # in the input is matched literally.
    #
    # `AND in_office`: current_members (cd-etl migration 0007) no longer
    # filters departed members out -- it exposes `in_office` instead, so
    # GET /members/{bioguide_id} can still serve them. This roster
    # endpoint is sitting-only, so it re-applies the filter here.
    #
    # ORDER BY: state (only varies for a name search), then senators,
    # then House by district, bioguide_id as a final deterministic
    # tiebreak (two same-state senators, an at-large 0 vs NULL) -- so the
    # flat `data` list has a stable order the old {senators,
    # representatives} split gave for free.
    #
    # Each optional filter adds its clause and its parameter together:
    # named() rejects a parameter the query never uses.
    clauses = ["in_office"]
    params: dict[str, Any] = {}
    if state is not None:
        clauses.append("state = :state")
        params["state"] = state
    if chamber is not None:
        clauses.append("chamber = :chamber")
        params["chamber"] = chamber
    if district is not None:
        clauses.append("district = :district")
        params["district"] = district
    for i, word in enumerate((name or "").split()):
        clauses.append(
            f"(given_name ILIKE :name_{i} OR nickname ILIKE :name_{i}"
            f" OR family_name ILIKE :name_{i})"
        )
        params[f"name_{i}"] = f"%{_escape_like(word)}%"
    return await _fetch(
        f"""
        SELECT * FROM current_members
        WHERE {" AND ".join(clauses)}
        ORDER BY state, (chamber <> 'SENATE'), district NULLS FIRST, bioguide_id
        """,
        params,
    )


def _escape_like(text: str) -> str:
    # Backslash is ILIKE's default escape character.
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def fetch_member(bioguide_id: str) -> dict | None:
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
    return await _fetchrow(
        """
        SELECT * FROM current_members
        WHERE bioguide_id = :bioguide_id
        ORDER BY end_year DESC NULLS FIRST, start_year DESC, member_term_id DESC
        LIMIT 1
        """,
        {"bioguide_id": bioguide_id},
    )


def _to_pgvector_literal(embedding: list[float]) -> str:
    # Bound as a plain string param and cast with ::text::vector in SQL --
    # no query here ever needs the pgvector Python package (and the
    # numpy dependency it pulls in) to deserialize a vector column back
    # into a float array, matching cd-etl's own db.to_pgvector_literal.
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


async def fetch_closest_vocab_term(embedding: list[float]) -> dict | None:
    # No index on vocab_term_embeddings (see migration 0005) -- a brute
    # force scan is exact and fast enough at this table's expected scale
    # (a few hundred distinct terms).
    # ::text first so asyncpg types the parameter as text -- it has no
    # codec for pgvector's own type -- and Postgres does the text -> vector
    # cast.
    return await _fetchrow(
        """
        SELECT kind, term, embedding <=> :embedding::text::vector AS distance
        FROM vocab_term_embeddings
        ORDER BY distance ASC
        LIMIT 1
        """,
        {"embedding": _to_pgvector_literal(embedding)},
    )


async def fetch_bills_by_policy_area(term: str, limit: int) -> list[dict]:
    return await _fetch(
        """
        SELECT bill_id, bill_key, congress, bill_type, bill_number, title,
               policy_area, crs_summary
        FROM bills
        WHERE policy_area = :term
        LIMIT :limit
        """,
        {"term": term, "limit": limit},
    )


async def fetch_bills_by_subject(term: str, limit: int) -> list[dict]:
    return await _fetch(
        """
        SELECT b.bill_id, b.bill_key, b.congress, b.bill_type, b.bill_number, b.title,
               b.policy_area, b.crs_summary
        FROM bills b
        JOIN bill_subjects s ON s.bill_id = b.bill_id
        WHERE s.subject_name = :term
        LIMIT :limit
        """,
        {"term": term, "limit": limit},
    )


async def fetch_bills_by_similarity(
    embedding: list[float], exclude_bill_ids: list[int], limit: int, max_distance: float
) -> list[dict]:
    # max_distance is a relevance floor -- without it this always pads
    # out to `limit` with whatever's *least* far, even when nothing in
    # the corpus is genuinely related to the query (confirmed empirically:
    # a query with no genuine match in a small local corpus still
    # returned `limit` bills, all filler). Filtered in SQL rather than
    # in Python so an empty result short-circuits the query itself.
    return await _fetch(
        """
        WITH scored AS (
            SELECT bill_id, bill_key, congress, bill_type, bill_number, title,
                   policy_area, crs_summary,
                   crs_summary_embedding <=> :embedding::text::vector AS distance
            FROM bills
            WHERE crs_summary_embedding IS NOT NULL
              AND NOT (bill_id = ANY(:exclude_bill_ids))
        )
        SELECT bill_id, bill_key, congress, bill_type, bill_number, title,
               policy_area, crs_summary
        FROM scored
        WHERE distance <= :max_distance
        ORDER BY distance ASC
        LIMIT :limit
        """,
        {
            "embedding": _to_pgvector_literal(embedding),
            "exclude_bill_ids": exclude_bill_ids,
            "limit": limit,
            "max_distance": max_distance,
        },
    )


async def fetch_member_votes(bioguide_id: str, bill_keys: list[str]) -> list[dict] | None:
    # Backs GET /members/{bioguide_id}/votes: every roll call this member
    # cast, across a caller-supplied set of bills keyed by canonical
    # bill_key ("119-hr-2616"). Returns one row per (bill, roll call,
    # member vote), plus the roll call's natural-key parts (chamber /
    # congress / session / vote_number) so the shaper can build the
    # "119-house-1-327" roll_call id.
    #
    # Returns None when the bioguide id has no current-Congress term at
    # all -- the route's 404, the same rule as GET /members/{bioguide_id}.
    # Checked on this same connection rather than via a separate
    # fetch_member() call, saving a pool checkout per request.
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
    async with _acquire() as conn:
        exists = await conn.fetchval(
            *named(
                "SELECT 1 FROM current_members WHERE bioguide_id = :bioguide_id LIMIT 1",
                {"bioguide_id": bioguide_id},
            )
        )
        if exists is None:
            return None
        rows = await conn.fetch(
            *named(
                """
                SELECT b.bill_key,
                       r.chamber, r.congress, r.session, r.vote_number,
                       r.vote_question, r.result, r.vote_date,
                       v.vote_cast
                FROM bills b
                LEFT JOIN roll_calls r ON r.bill_id = b.bill_id
                LEFT JOIN roll_call_member_votes v
                       ON v.roll_call_id = r.roll_call_id
                      AND v.bioguide_id = :bioguide_id
                WHERE b.bill_key = ANY(:bill_keys)
                ORDER BY r.vote_date, r.roll_call_id
                """,
                {"bioguide_id": bioguide_id, "bill_keys": bill_keys},
            )
        )
    return [dict(row) for row in rows]
