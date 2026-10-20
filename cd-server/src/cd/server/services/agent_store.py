"""Storage for the research agent's conversations (cd-platform#164) --
agent_conversations / agent_turns (migration 0004).

AgentStoreClient is thin, same role as AiSummaryClient: it owns its own
asyncpg pool against cd_customers and the raw SQL, and knows nothing
about the model or the tools. A question and its answer are written
together, in one transaction, only once the agent has finished the turn
-- so a failed or abandoned turn leaves no half-written exchange behind.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg
from cd.lib.sql import named

# Longer opening questions are cut to this for the stored title; the full
# question is still the conversation's first turn.
_TITLE_MAX_LEN = 120


@dataclass(frozen=True)
class AssistantAnswer:
    """What the agent hands back for one turn, ready to store."""

    content: str
    trace: list[dict[str, Any]]
    model_id: str
    stop_reason: str  # 'answered' | 'step_limit' | 'citations_flagged'
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class AgentTurnRecord:
    id: int
    role: str  # 'user' | 'assistant'
    content: str
    trace: list[dict[str, Any]]
    model_id: str | None
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime


@dataclass(frozen=True)
class AgentConversationRecord:
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    # Oldest first. Empty for a History list entry (list_conversations
    # doesn't load them); full for get_conversation.
    turns: list[AgentTurnRecord] = field(default_factory=list)


def _title(question: str) -> str:
    question = " ".join(question.split())
    if len(question) <= _TITLE_MAX_LEN:
        return question
    return question[: _TITLE_MAX_LEN - 1].rstrip() + "…"


def _turn(row: asyncpg.Record) -> AgentTurnRecord:
    return AgentTurnRecord(
        id=row["turn_id"],
        role=row["role"],
        content=row["content"],
        trace=row["trace"],
        model_id=row["model_id"],
        stop_reason=row["stop_reason"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        created_at=row["created_at"],
    )


def _conversation(row: asyncpg.Record, turns: list[AgentTurnRecord]) -> AgentConversationRecord:
    return AgentConversationRecord(
        id=row["conversation_id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        turns=turns,
    )


_INSERT_TURNS = """
    INSERT INTO agent_turns (
        conversation_id, role, content, trace,
        model_id, stop_reason, input_tokens, output_tokens
    )
    VALUES
        (:conversation_id, 'user', :question, '[]', NULL, NULL, NULL, NULL),
        (:conversation_id, 'assistant', :content, :trace,
         :model_id, :stop_reason, :input_tokens, :output_tokens)
"""


def _turn_params(conversation_id: int, question: str, answer: AssistantAnswer) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "question": question,
        "content": answer.content,
        "trace": answer.trace,
        "model_id": answer.model_id,
        "stop_reason": answer.stop_reason,
        "input_tokens": answer.input_tokens,
        "output_tokens": answer.output_tokens,
    }


class AgentStoreClient:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        # Same per-connection jsonb codec as AiSummaryClient, so `trace`
        # binds and reads as a plain Python list.
        self._pool = await asyncpg.create_pool(self._dsn, init=self._register_codecs)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @staticmethod
    async def _register_codecs(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    def _get_pool(self) -> asyncpg.Pool:
        assert self._pool is not None, "AgentStoreClient.connect() was never called"
        return self._pool

    async def start_conversation(
        self, user_id: str, question: str, answer: AssistantAnswer
    ) -> int:
        """Creates the conversation and its first exchange; returns its id."""
        async with self._get_pool().acquire() as conn, conn.transaction():
            conversation_id = await conn.fetchval(
                *named(
                    """
                    INSERT INTO agent_conversations (user_id, title)
                    VALUES (:user_id, :title)
                    RETURNING conversation_id
                    """,
                    {"user_id": user_id, "title": _title(question)},
                )
            )
            await conn.execute(
                *named(_INSERT_TURNS, _turn_params(conversation_id, question, answer))
            )
        return conversation_id

    async def add_exchange(
        self, user_id: str, conversation_id: int, question: str, answer: AssistantAnswer
    ) -> bool:
        """Appends a question and its answer. Returns False, writing
        nothing, when the conversation isn't this caller's (or doesn't
        exist) -- the two cases look the same, so a caller can't probe
        for other users' conversation ids."""
        async with self._get_pool().acquire() as conn, conn.transaction():
            owned = await conn.fetchval(
                *named(
                    """
                    UPDATE agent_conversations
                    SET updated_at = now()
                    WHERE conversation_id = :conversation_id AND user_id = :user_id
                    RETURNING true
                    """,
                    {"conversation_id": conversation_id, "user_id": user_id},
                )
            )
            if not owned:
                return False
            await conn.execute(
                *named(_INSERT_TURNS, _turn_params(conversation_id, question, answer))
            )
        return True

    async def get_conversation(
        self, user_id: str, conversation_id: int
    ) -> AgentConversationRecord | None:
        """The conversation with every turn, oldest first -- None when it
        isn't this caller's (or doesn't exist)."""
        async with self._get_pool().acquire() as conn:
            row = await conn.fetchrow(
                *named(
                    """
                    SELECT conversation_id, title, created_at, updated_at
                    FROM agent_conversations
                    WHERE conversation_id = :conversation_id AND user_id = :user_id
                    """,
                    {"conversation_id": conversation_id, "user_id": user_id},
                )
            )
            if row is None:
                return None
            turns = await conn.fetch(
                *named(
                    """
                    SELECT turn_id, role, content, trace, model_id, stop_reason,
                           input_tokens, output_tokens, created_at
                    FROM agent_turns
                    WHERE conversation_id = :conversation_id
                    ORDER BY turn_id
                    """,
                    {"conversation_id": conversation_id},
                )
            )
        return _conversation(row, [_turn(t) for t in turns])

    async def list_conversations(
        self, user_id: str, limit: int
    ) -> list[AgentConversationRecord]:
        """This caller's conversations, most recently active first, without turns."""
        rows = await self._get_pool().fetch(
            *named(
                """
                SELECT conversation_id, title, created_at, updated_at
                FROM agent_conversations
                WHERE user_id = :user_id
                ORDER BY updated_at DESC, conversation_id DESC
                LIMIT :limit
                """,
                {"user_id": user_id, "limit": limit},
            )
        )
        return [_conversation(row, []) for row in rows]

    async def count_answers_since(self, since: datetime, user_id: str | None = None) -> int:
        """Assistant turns created at or after `since` -- across all
        callers when `user_id` is None, else just that caller's. Backs the
        agent's daily caps; each answer is one or more Bedrock calls."""
        return await self._get_pool().fetchval(
            *named(
                """
                SELECT count(*)
                FROM agent_turns AS t
                JOIN agent_conversations AS c USING (conversation_id)
                WHERE t.role = 'assistant'
                  AND t.created_at >= :since
                  AND (:user_id::text IS NULL OR c.user_id = :user_id)
                """,
                {"since": since, "user_id": user_id},
            )
        )
