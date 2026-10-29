"""cd-server's one asyncpg pool against its own database, cd_customers.

Shared by every client that reads or writes cd_customers (UsersClient,
AiSummaryClient), rather than each opening its own: asyncpg's defaults
are min_size=10/max_size=10, so every separate pool held 10 idle
connections per task from startup, doubled during a rolling deploy --
on a db.t4g.micro that's shared with cd.api's pool, Airflow and the
RDS Proxy. Mirrors cd.api.db's pool for congressional_app.
"""
from __future__ import annotations

import json

import asyncpg

from cd.server import settings

# Same bounds as cd.api.db's pool. Connections now open on demand, so an
# authenticated request may wait on a fresh connect: without a limit, an
# unreachable cd_customers (a failover, a security-group drop) would hold
# it for asyncpg's 60s default -- the ALB's idle timeout, so the browser
# would get the ALB's CORS-less 504 instead of a fast error.
CONNECT_TIMEOUT_SECONDS = 10
# Bounds each query. It also bounds how long any one caller can hold a
# connection, since every client call here is a single statement, so a
# caller queued for a free connection under a burst waits at most about
# this long per caller ahead of it.
COMMAND_TIMEOUT_SECONDS = 15
# Closes connections idle longer than this; with min_size=0 they're only
# reopened on demand.
MAX_INACTIVE_CONNECTION_LIFETIME_SECONDS = 300


async def _register_codecs(conn: asyncpg.Connection) -> None:
    # Runs once for every new physical connection the pool opens, not once
    # for the pool itself, so jsonb columns (ai_summaries.subject) bind and
    # read as plain Python objects -- otherwise every call needs
    # json.dumps()/loads() and an explicit ::jsonb cast, since asyncpg has
    # no jsonb codec by default. schema="pg_catalog" matches the built-in
    # jsonb type, not some user-defined type of the same name. Harmless for
    # queries that touch no jsonb column.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


class CustomersDb:
    """Owns the cd_customers pool. connect()/close() are called once, from
    app.py's lifespan -- asyncpg.create_pool() is a coroutine, so it can't
    run at import time like the services that use it."""

    def __init__(self, dsn: str, max_size: int = settings.CD_SERVER_PG_POOL_SIZE):
        self._dsn = dsn
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        # min_size=0: connections open on demand, so a task holds only what
        # it's actually using rather than max_size from startup.
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=0,
            max_size=self._max_size,
            timeout=CONNECT_TIMEOUT_SECONDS,
            command_timeout=COMMAND_TIMEOUT_SECONDS,
            max_inactive_connection_lifetime=MAX_INACTIVE_CONNECTION_LIFETIME_SECONDS,
            init=_register_codecs,
        )
        # min_size=0 opens nothing above, so check cd_customers is actually
        # reachable with these credentials. Raises, failing the lifespan and
        # so the task's startup -- unlike cd.api.db's probe, which only
        # warns. A rolling deploy then keeps the old tasks rather than
        # replacing them with ones whose user upserts fail silently and
        # whose summaries 500. (Before the shared pool, create_pool()'s
        # min_size=10 connections did this implicitly.)
        try:
            await self._pool.fetchval("SELECT 1")
        except BaseException:
            pool, self._pool = self._pool, None
            await pool.close()
            raise

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("CustomersDb.connect() was never called")
        return self._pool
