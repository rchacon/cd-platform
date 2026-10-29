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
            self._dsn, min_size=0, max_size=self._max_size, init=_register_codecs
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("CustomersDb.connect() was never called")
        return self._pool
