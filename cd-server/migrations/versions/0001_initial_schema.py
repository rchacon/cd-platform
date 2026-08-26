"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-19 00:00:00.000000

cd_customers' only table for now: a plain user record upserted whenever
cd-server verifies a Cognito ID token on an incoming GraphQL request (see
src/cd/server/services/users_service.py). One op.execute() for the single
statement here, matching cd-etl's own raw-SQL migration idiom.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        -- id is Cognito's `sub` claim (a UUID string) -- stored as TEXT,
        -- not Postgres's uuid type, matching cd-etl's own preference for
        -- plain TEXT over exotic types (e.g. members.bioguide_id).
        --
        -- email is kept in sync with the IdP on every request (see the
        -- upsert's ON CONFLICT clause) rather than only set once, in
        -- case a user changes their email at the IdP.
        --
        -- created_at is only ever set on first insert -- ON CONFLICT
        -- deliberately doesn't touch it. last_seen updates on every
        -- upsert, i.e. on every GraphQL request carrying a verified
        -- token, not throttled -- a deliberately simple first pass (see
        -- AGENTS.md).
        CREATE TABLE users (
            id          TEXT PRIMARY KEY,
            email       TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE users")
