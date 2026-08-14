"""bills title/crs_summary

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-13 00:00:00.000000

Adds bills.title and bills.crs_summary, both TEXT and nullable. 0002's own
comment on the bills table explicitly deferred title ("not load-bearing for
this table's only current purpose") -- this migration is that "add them if/
when a consumer actually needs them" moment: rchacon/cd-platform#9 (semantic
search over bill subjects) needs both as the embedding source (title +
crs_summary concatenated, picking up the colloquial hook a free-text query
like "dreamers" would match that a policy_area/subject_name alone wouldn't),
and rchacon/cd-platform#52 (bills never refresh after first sync) needs
somewhere to write a bill's current CRS summary once bills_etl starts
fetching one.

crs_summary holds the single most recent CRS-authored summary for a bill
(Congress.gov's own /summaries sub-resource returns one per legislative
stage -- e.g. introduced, reported, enrolled -- not one fixed value), picked
by the ETL as the entry with the latest actionDate. Not modeled as its own
child table: unlike bill_subjects, nothing in this project's scope needs
every historical summary version, only the current one.

source_hash's inputs now also cover title and crs_summary, so a change to
either bumps updated_at the same way a policy_area change already does --
no schema change needed for the hashing itself, only the ETL computing it
needs to include the new fields. 0002's own inline SQL comment
documenting source_hash's inputs is left as-is rather than edited (same
precedent 0003 already set for not editing a prior migration's own
comment -- see that file's docstring): this migration instead issues a
live COMMENT ON COLUMN, which actually supersedes it in the schema
Postgres itself reports, rather than just a comment in a file nothing
re-reads at runtime.
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
        ALTER TABLE bills
            ADD COLUMN title TEXT,
            ADD COLUMN crs_summary TEXT
        """
    )

    op.execute(
        """
        COMMENT ON COLUMN bills.source_hash IS
            'SHA-256 hash of the normalized source fields: congress, '
            'bill_type, bill_number, title, policy_area, crs_summary'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        COMMENT ON COLUMN bills.source_hash IS
            'SHA-256 hash of the normalized source fields: congress, '
            'bill_type, bill_number, policy_area'
        """
    )

    op.execute(
        """
        ALTER TABLE bills
            DROP COLUMN title,
            DROP COLUMN crs_summary
        """
    )
