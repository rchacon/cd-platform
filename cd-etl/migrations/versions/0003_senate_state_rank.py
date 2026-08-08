"""senate seniority (Senior/Junior Senator)

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-08 00:00:00.000000

Adds members.senate_state_rank, resolving rchacon/cd-platform#3: Senior/Junior
Senator status, sourced from unitedstates/congress-legislators'
legislators-current.yaml (the same file 0002's members.lis_member_id crosswalk
is populated from) rather than derived from continuous-service history --
that upstream file already carries an editorially-maintained state_rank per
Senate term, resolving tie-break cases (prior chamber/gubernatorial service,
alphabetical order) this project would otherwise have to approximate.
NULL for House members and any senator not yet resolved by that sync, same
1:1-or-NULL shape as lis_member_id.

current_members is replaced (CREATE OR REPLACE VIEW -- safe here, only adding
a trailing column) to expose the new column. 0001's own comment on that view
("Deliberately excludes any Senior/Senator vs. Junior Senator distinction --
... isn't derivable from current-Congress-only term data") is left as-is,
an accurate historical record of that migration's own view -- it's this
migration's CREATE OR REPLACE that supersedes it, not an edit to 0001 itself.
members_etl.py's _term_rows comment, which describes current runtime
behavior rather than a historical migration, is updated in the same commit
as this migration.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TYPE senate_state_rank_type AS ENUM (
            'SENIOR',
            'JUNIOR'
        )
        """
    )

    op.execute(
        """
        ALTER TABLE members
            ADD COLUMN senate_state_rank senate_state_rank_type
        """
    )

    op.execute(
        """
        -- Same view as 0001, plus state_rank -- see this migration's own
        -- docstring for why current_members can now expose it.
        CREATE OR REPLACE VIEW current_members AS
        SELECT
            mt.*,
            m.given_name,
            m.middle_name,
            m.family_name,
            m.nickname,
            m.suffix,
            m.photo_uri,
            m.phone,
            m.website_url,
            cp.party,
            cp.source_party_name,
            m.senate_state_rank AS state_rank
        FROM member_terms AS mt
        JOIN members AS m
            ON m.bioguide_id = mt.bioguide_id
        LEFT JOIN LATERAL (
            SELECT
                elem ->> 'party' AS party,
                elem ->> 'source_party_name' AS source_party_name
            FROM jsonb_array_elements(m.party_history) AS elem
            ORDER BY (elem ->> 'start_year')::int DESC
            LIMIT 1
        ) AS cp ON TRUE
        WHERE mt.congress = current_congress()
          AND (mt.end_year IS NULL OR mt.end_year >= EXTRACT(YEAR FROM CURRENT_DATE)::smallint)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW current_members AS
        SELECT
            mt.*,
            m.given_name,
            m.middle_name,
            m.family_name,
            m.nickname,
            m.suffix,
            m.photo_uri,
            m.phone,
            m.website_url,
            cp.party,
            cp.source_party_name
        FROM member_terms AS mt
        JOIN members AS m
            ON m.bioguide_id = mt.bioguide_id
        LEFT JOIN LATERAL (
            SELECT
                elem ->> 'party' AS party,
                elem ->> 'source_party_name' AS source_party_name
            FROM jsonb_array_elements(m.party_history) AS elem
            ORDER BY (elem ->> 'start_year')::int DESC
            LIMIT 1
        ) AS cp ON TRUE
        WHERE mt.congress = current_congress()
          AND (mt.end_year IS NULL OR mt.end_year >= EXTRACT(YEAR FROM CURRENT_DATE)::smallint)
        """
    )
    op.execute("ALTER TABLE members DROP COLUMN senate_state_rank")
    op.execute("DROP TYPE senate_state_rank_type")
