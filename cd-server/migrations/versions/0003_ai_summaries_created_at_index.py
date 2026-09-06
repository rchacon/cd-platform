"""ai_summaries: index created_at for the daily-cap counts

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06 00:00:00.000000

cd-platform#169's per-user and global daily caps on summarizeVotingRecord
count ai_summaries rows on the current UTC calendar day. The per-user
count (WHERE user_id = $1 AND created_at >= <day start>) is already served
by idx_ai_summaries_user_created (0002), but the *global* count (WHERE
created_at >= <day start>, no user filter) isn't -- that index leads with
user_id. This adds the standalone created_at index for it.

Plain CREATE INDEX, not CONCURRENTLY: the table is tiny (one row per
summary ever generated) so the lock is sub-millisecond, and it's safe
under both the current on-boot migrate and cd-infra#67's one-shot
migrate task. Additive -- old code never needs it.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE INDEX idx_ai_summaries_created_at ON ai_summaries (created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX idx_ai_summaries_created_at")
