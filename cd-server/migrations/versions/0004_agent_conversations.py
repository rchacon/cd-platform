"""agent_conversations / agent_turns: the research agent's threads

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20 00:00:00.000000

The research agent (cd-platform#164) is multi-turn, which doesn't fit
ai_summaries' one-flat-row-per-generation shape, so it gets two tables of
its own: a conversation per thread, and its turns in order. Each assistant
turn stores its full tool-call trace, so a History view can replay what
the agent did without re-running it against data that keeps syncing --
the same point-in-time reasoning as ai_summaries.subject.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        -- title: the opening question, so a History list has something to
        -- show without loading every turn.
        --
        -- updated_at: bumped whenever a turn is added -- History lists
        -- conversations by most recent activity, not by when they began.
        CREATE TABLE agent_conversations (
            conversation_id BIGSERIAL PRIMARY KEY,
            user_id         TEXT NOT NULL
                REFERENCES users (id),
            title           TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        -- Backs myAgentConversations: this caller's threads, most recently
        -- active first.
        CREATE INDEX idx_agent_conversations_user_updated
        ON agent_conversations (user_id, updated_at DESC)
        """
    )
    op.execute(
        """
        -- One row per message. A 'user' row is the question (trace '[]',
        -- the model/usage columns NULL); an 'assistant' row is the answer.
        --
        -- trace: the ordered steps the agent took to produce this answer
        -- -- each model call and tool call, with its inputs, result,
        -- latency and token usage. JSONB so it stays queryable.
        --
        -- stop_reason: how the turn ended -- 'answered', 'step_limit'
        -- (the step budget ran out first) or 'citations_flagged' (the
        -- answer cited something no tool returned, even after one retry).
        --
        -- input_tokens / output_tokens: summed over every model call in
        -- the turn, for cost reporting without unpacking the trace.
        CREATE TABLE agent_turns (
            turn_id         BIGSERIAL PRIMARY KEY,
            conversation_id BIGINT NOT NULL
                REFERENCES agent_conversations (conversation_id) ON DELETE CASCADE,
            role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content         TEXT NOT NULL,
            trace           JSONB NOT NULL DEFAULT '[]',
            model_id        TEXT,
            stop_reason     TEXT,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        -- Loads a conversation's turns in order.
        CREATE INDEX idx_agent_turns_conversation
        ON agent_turns (conversation_id, turn_id)
        """
    )
    op.execute(
        """
        -- Backs the agent's daily caps, which count assistant turns since
        -- the start of the UTC day (globally, or joined to one user).
        CREATE INDEX idx_agent_turns_created_at ON agent_turns (created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_turns")
    op.execute("DROP TABLE agent_conversations")
