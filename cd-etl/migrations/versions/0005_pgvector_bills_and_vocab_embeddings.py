"""pgvector: bills.crs_summary_embedding + vocab_term_embeddings

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-27 00:00:00.000000

Schema half of rchacon/cd-platform#9 (semantic search over bill
subjects) -- embedding generation itself is a separate, later
migration-free change to bills_common.py; this just adds somewhere to
put the vectors.

bills.crs_summary_embedding holds the embedding of title + crs_summary
concatenated (see 0004's own docstring for why both fields, together).
vector(1024): Titan Text Embeddings V2 (the chosen embedding model)
supports 256/512/1024-dim output: 1024 is picked for retrieval quality
since Titan pricing is per-token, not per-dimension, so there's no cost
reason to go smaller, and this project's scale (a few thousand bills)
makes the storage/index cost difference irrelevant either way.

vocab_term_embeddings is a single table (not two) for policy_area and
bill_subjects.subject_name embeddings, distinguished by a `kind`
column: both are short controlled-vocabulary strings needing the
identical shape (term + embedding), and query-time tier-1 matching
wants to check both against one query embedding in a single query, not
two separate ones merged in application code. Deliberately no ANN
index on this table -- at its expected scale (dozens of policy areas,
low thousands of subject terms per the issue's own estimate), a
brute-force cosine scan is both fast enough and exact; an approximate
index here would trade away recall for no real speed benefit.

idx_bills_crs_summary_embedding is a partial HNSW index (WHERE
crs_summary_embedding IS NOT NULL) since most bills won't have an
embedding until cd-etl backfills incrementally -- matches how it's
queried (cd-api's similarity-search tier always filters on the column
being non-null first).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("ALTER TABLE bills ADD COLUMN crs_summary_embedding vector(1024)")

    op.execute(
        """
        CREATE INDEX idx_bills_crs_summary_embedding
        ON bills USING hnsw (crs_summary_embedding vector_cosine_ops)
        WHERE crs_summary_embedding IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE TYPE vocab_term_kind AS ENUM (
            'POLICY_AREA',
            'SUBJECT'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE vocab_term_embeddings (
            vocab_term_embedding_id BIGSERIAL PRIMARY KEY,

            kind        vocab_term_kind NOT NULL,
            term        TEXT NOT NULL,
            embedding   vector(1024) NOT NULL,

            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT vocab_term_embeddings_unique_term UNIQUE (kind, term)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE vocab_term_embeddings")
    op.execute("DROP TYPE vocab_term_kind")
    op.execute("DROP INDEX idx_bills_crs_summary_embedding")
    op.execute("ALTER TABLE bills DROP COLUMN crs_summary_embedding")
    # Extension left in place on downgrade -- no precedent in this repo
    # for a migration's downgrade dropping an extension it created (a
    # shared, instance-wide object, not scoped to this migration's own
    # schema changes).
