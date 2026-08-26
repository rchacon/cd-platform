from __future__ import annotations

import hashlib
import logging
from typing import Any

from airflow.providers.postgres.hooks.postgres import PostgresHook

logger = logging.getLogger(__name__)


class IsolatedTransaction:
    """Runs one unit of work against its own fresh Postgres connection,
    isolating that unit's failure from the rest of a run: commits on a
    clean exit, rolls back + logs + suppresses the exception on failure
    (the caller's `with` block just ends early, no exception propagates),
    and always closes the connection.

    A fresh connection per unit rather than one long-lived connection
    reused across many units means a connection is never left open (and
    vulnerable to an infra-level idle-connection timeout) across an
    unrelated network-bound phase -- e.g. an API fetch -- between units
    of work; it's only ever open for as long as this one unit's actual DB
    work takes. `rollback()` on an already-dead connection is itself
    swallowed rather than allowed to escape uncaught, so one unit's
    connection trouble can't abort whatever's driving the loop this runs
    inside of.

    Check `.failed` after the `with` block to tell whether this unit
    succeeded, e.g. for a running failure count:

        for item in items:
            txn = IsolatedTransaction(hook, f"item {item!r}")
            with txn as conn:
                ...  # do this item's DB work against conn
            if txn.failed:
                failed_count += 1

    Not used by bills_common.sync_bill()/bills_etl.refresh_bills's
    refresh_one(), even though both do a similar isolate-and-continue
    dance -- sync_bill() commits multiple times internally rather than
    once at the end, and refresh_one() re-raises into
    congress_api.fetch_concurrently()'s own per-item isolation instead of
    handling it itself, so neither actually fits this shape without a
    larger, separate refactor of their own control flow (tracked in
    rchacon/cd-platform#87).
    """

    def __init__(self, hook: PostgresHook, description: str):
        self._hook = hook
        self._description = description
        self.failed = False

    def __enter__(self) -> Any:
        self._conn = self._hook.get_conn()
        return self._conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self._conn.commit()
        else:
            try:
                self._conn.rollback()
            except Exception:
                pass  # connection's already dead -- nothing left to roll back
            self.failed = True
            logger.error("Failed to sync %s: %s", self._description, exc)
        self._conn.close()
        return True  # suppress -- caller's loop continues to the next unit


def get_current_congress(postgres_conn_id: str) -> int:
    # Postgres's own current_congress() function is the single place
    # every ETL agrees on "which Congress is current." Shared here since
    # house_votes_etl.py and bills_etl.py both need this exact,
    # no-upstream-dependency lookup as their very first task -- their
    # copies were identical. members_etl.py's own get_current_congress
    # keeps its own copy rather than being forced onto this shape: it
    # takes a dummy upstream-ordering argument (so Airflow sequences it
    # after sync_current_congress) that this shared, zero-arg version
    # has no equivalent for.
    hook = PostgresHook(postgres_conn_id=postgres_conn_id)
    row = hook.get_first("SELECT current_congress()")
    if row is None or row[0] is None:
        raise ValueError("No current congress found in congresses table")
    return row[0]


def source_hash(*parts: Any) -> str:
    normalized = "|".join(
        str(part).strip().lower() if part is not None else "" for part in parts
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def to_pgvector_literal(embedding: list[float]) -> str:
    # Neither writer nor reader in this codebase ever deserializes a
    # vector column back into a float array (only ever writes one, or
    # reads back a computed distance) -- a plain string literal bound as
    # a normal %s param and cast with ::vector in SQL is enough, so
    # there's no need for the pgvector Python package (and the numpy
    # dependency it pulls in) just to register a bidirectional adapter
    # neither service actually needs.
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"
