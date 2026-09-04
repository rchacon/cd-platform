"""ai_summaries: stored AI-generated summaries

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05 00:00:00.000000

General-purpose, not voting-record-specific -- kind + subject are what
let this table hold any future summary type (e.g. a bill's legislative
evolution) alongside voting-record summaries without a schema change.
One row per invocation, no dedup/caching by design (every real call is
a real row -- this table also doubles as the product owner's
usage-insight source: who's calling it, on what, how often -- queried
directly, no dedicated reporting endpoint in v1).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        -- user_id: the caller who requested this summary -- FK to
        -- users.id (the Cognito sub), NOT NULL: every kind of summary
        -- this table holds is authenticated-only, so unlike
        -- users.last_seen (touched for every verified caller regardless
        -- of what they did) there's no anonymous-caller case here.
        --
        -- kind: which summary type this row is, e.g. "voting_record"
        -- (a member's votes on a topic) or a future "bill_evolution" (a
        -- bill's legislative history) -- discriminates how to interpret
        -- `subject` below and lets history be filtered/reported on by
        -- type. Plain TEXT, not an enum: a new kind must not need a
        -- schema migration to add, only application code that writes
        -- and reads it.
        --
        -- subject: everything that was fed into the prompt as data,
        -- self-describing per kind -- for "voting_record" this is
        -- {"bioguideId", "topic", "bills": [...]} (the bills+votes JSON,
        -- the same shape searchBills returns). Deliberately holds the
        -- identifying fields too (bioguideId, topic) rather than
        -- breaking them out into their own columns: a future kind's
        -- identifying fields will look nothing like a member+topic pair
        -- (e.g. bill_evolution's would be a bill key), so per-kind
        -- columns would multiply forever. Dual purpose, both requiring
        -- the full searchBills-shaped bill data (not a trimmed-down
        -- prompt-only projection): (1) grounds the generated text and
        -- (2) is a point-in-time snapshot a read-only "History" view can
        -- re-render the same search-results UI from, without re-running
        -- a (nondeterministic) search against data that keeps syncing.
        -- Member profile fields (name/party/photo) are deliberately NOT
        -- included -- unlike bills/votes they don't need point-in-time
        -- pinning, so History re-fetches those live via getMember(bioguideId)
        -- instead of duplicating them into every row. Stored as JSONB
        -- (not the raw prompt text) so it stays diffable/queryable, and
        -- so a later
        -- prompt or search-data change can be checked against exactly
        -- what produced this row, not a re-fetched approximation.
        --
        -- prompt_template: the system-prompt text this row was
        -- generated with, snapshotted verbatim rather than referenced
        -- by version -- there's no prompt_templates table yet (a future
        -- migration), so this is the plain text for now. Lets a later
        -- prompt-wording change be correlated with a change in output
        -- quality across rows, since each row still shows exactly what
        -- instructions produced it. Does NOT include the small
        -- "Summarize X's voting record on Y using this data:"-style
        -- user-turn wrapper -- that's reconstructable from kind+subject
        -- and not worth its own column.
        --
        -- summary: the full generated text returned to the caller,
        -- stored so a later read (myAiSummaries) can serve it back
        -- without re-generating -- every *generation* is fresh, but a
        -- *read* of a past one must be free.
        --
        -- model_id: the Bedrock model actually used for this row, not
        -- just whatever the current setting happens to be -- if the
        -- model is ever changed, older rows stay correctly attributed
        -- to what actually produced them.
        --
        -- created_at: the only timestamp -- this table is insert-only,
        -- no update path exists.
        CREATE TABLE ai_summaries (
            ai_summary_id   BIGSERIAL PRIMARY KEY,
            user_id         TEXT NOT NULL
                REFERENCES users (id),
            kind            TEXT NOT NULL,
            subject         JSONB NOT NULL,
            prompt_template TEXT NOT NULL,
            summary         TEXT NOT NULL,
            model_id        TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        -- Backs myAiSummaries' one access pattern: this caller's own
        -- rows (of any kind, mixed together -- one History page across
        -- every summary type), newest first, LIMIT-capped (no cursor
        -- pagination in v1, matching every other list resolver in this
        -- schema). Composite with created_at DESC so the index is
        -- already in the query's required order.
        CREATE INDEX idx_ai_summaries_user_created
        ON ai_summaries (user_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ai_summaries")
