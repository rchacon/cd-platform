"""Stores and serves back AI-generated summaries -- general-purpose, not
voting-record-specific (see migrations/versions/0002_ai_summaries.py's
own docstring on `kind`/`subject`). This is the storage half only --
AiSummaryClient (thin, owns the ai_summaries connection pool and raw
SQL, no prompt/Bedrock knowledge -- same role/shape as UsersClient) and
AiSummaryService's history() read path. Prompt composition + the actual
Bedrock call (a kind-specific generate(), e.g. for voting-record
summaries) land in a follow-up once the Bedrock side exists; this PR is
independently testable without either.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from cd.server import settings


@dataclass(frozen=True)
class AiSummaryRecord:
    """One stored row -- what AiSummaryService hands back to schema.py,
    whether freshly generated or read from history. `subject` is
    kind-specific (e.g. {"bioguideId", "topic", "bills": [...]} for
    kind="voting_record") -- callers that care about one kind read it
    out of there, not off this record directly."""

    id: int
    kind: str
    subject: Any  # kind-specific JSON (dict) -- see AiSummaryClient's jsonb codec
    prompt_template: str  # the system-prompt text this row was generated with
    summary: str
    model_id: str
    created_at: datetime


def _record(row: asyncpg.Record) -> AiSummaryRecord:
    return AiSummaryRecord(
        id=row["ai_summary_id"],
        kind=row["kind"],
        # Already a parsed Python object, not raw JSON text -- the pool's
        # jsonb codec (registered in AiSummaryClient.connect()) decodes
        # it automatically.
        subject=row["subject"],
        prompt_template=row["prompt_template"],
        summary=row["summary"],
        model_id=row["model_id"],
        created_at=row["created_at"],
    )


class AiSummaryClient:
    """Thin wrapper around the ai_summaries connection pool -- owns the
    raw insert/select SQL, no prompt/Bedrock/kind-specific knowledge.
    Its own independent asyncpg.Pool against cd_customers, following the
    "each service opens its own pool, no sharing" precedent UsersClient/
    CdApiService/GeocoderService already set. connect()/close() are
    designed to be called from app.py's lifespan (asyncpg.create_pool()
    is a coroutine, can't run at construction time), same as UsersClient
    -- but that wiring isn't added until this service is actually
    instantiated at module scope in a follow-up PR; nothing calls
    connect() yet in this one."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        # Registered per-connection (asyncpg's init= runs once for every
        # new physical connection the pool opens, not once for the pool
        # itself) so `subject` binds/reads as a plain Python object --
        # json.dumps()/loads() + an explicit ::jsonb cast are otherwise
        # needed on every call, since asyncpg has no jsonb codec by
        # default. schema="pg_catalog" matches the built-in jsonb type,
        # not some user-defined type of the same name.
        self._pool = await asyncpg.create_pool(self._dsn, init=self._register_codecs)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    @staticmethod
    async def _register_codecs(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    async def insert_summary(
        self,
        user_id: str,
        kind: str,
        subject: Any,
        prompt_template: str,
        summary: str,
        model_id: str,
    ) -> AiSummaryRecord:
        assert self._pool is not None, "AiSummaryClient.connect() was never called"
        # RETURNING only the two DB-generated columns, not every value
        # the caller already passed in -- subject in particular can be a
        # full bills+votes JSON snapshot (see the migration's own
        # docstring), not worth a round-trip re-select/re-decode of data
        # this call just wrote verbatim.
        row = await self._pool.fetchrow(
            """
            INSERT INTO ai_summaries (
                user_id, kind, subject, prompt_template, summary, model_id
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING ai_summary_id, created_at
            """,
            user_id,
            kind,
            # Bound directly as a Python object -- the pool's jsonb codec
            # (see _register_codecs) encodes it; Postgres infers $3's
            # type as jsonb from the target column, no explicit cast
            # needed.
            subject,
            prompt_template,
            summary,
            model_id,
        )
        return AiSummaryRecord(
            id=row["ai_summary_id"],
            kind=kind,
            subject=subject,
            prompt_template=prompt_template,
            summary=summary,
            model_id=model_id,
            created_at=row["created_at"],
        )

    async def fetch_history(self, user_id: str, limit: int) -> list[AiSummaryRecord]:
        assert self._pool is not None, "AiSummaryClient.connect() was never called"
        rows = await self._pool.fetch(
            """
            SELECT ai_summary_id, kind, subject, prompt_template, summary, model_id, created_at
            FROM ai_summaries
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
        return [_record(row) for row in rows]


class AiSummaryService:
    """What schema.py's resolvers depend on. Storage-only for now --
    generate() (prompt composition + the Bedrock Converse call, for
    whichever kind a follow-up PR adds first) lands alongside the
    Bedrock client itself."""

    def __init__(self, client: AiSummaryClient):
        self._client = client

    async def connect(self) -> None:
        await self._client.connect()

    async def aclose(self) -> None:
        await self._client.close()

    async def history(self, user_id: str, limit: int = 20) -> list[AiSummaryRecord]:
        return await self._client.fetch_history(user_id, limit)


def get_ai_summary_service() -> AiSummaryService:
    return AiSummaryService(AiSummaryClient(settings.PG_DSN))
