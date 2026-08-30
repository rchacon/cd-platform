"""current_members: expose in_office instead of filtering departed members out

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-30 00:00:00.000000

The view was scoped to the current Congress AND to not-yet-departed terms
(`end_year IS NULL OR end_year >= this_year`), so a member who
resigned / died / was expelled mid-Congress simply vanished from it.

cd-api's `GET /members/{bioguide_id}` (rchacon/cd-platform#104) wants to
keep serving such a member -- with a flag -- so a bookmarked detail page
still resolves after a resignation. `GET /members` (the district roster)
still wants sitting-only.

One view serves both: keep only `congress = current_congress()` in the
WHERE, and move the currency test into an `in_office` column (the exact
expression the dropped clause used). `GET /members` adds `AND in_office`;
`GET /members/{bioguide_id}` reads the column. "In office" is defined in
one place.

Still current-Congress only -- a past-Congress member isn't in the view
regardless of `in_office`. `CREATE OR REPLACE` is legal: `in_office` is
appended last, nothing existing is reordered or retyped. `downgrade`
can't drop a view column with REPLACE, so it does DROP + CREATE.

Deploy note: after this lands, the view includes 2025-departed members
until cd-api adds its `AND in_office` filter -- cut and deploy the cd-api
release right after this one. The interim exposure is cosmetic (a
departed rep in a district list), never an error.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VIEW_WITH_IN_OFFICE = """
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
    m.senate_state_rank AS state_rank,
    (mt.end_year IS NULL
     OR mt.end_year >= EXTRACT(YEAR FROM CURRENT_DATE)::smallint) AS in_office
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
"""

# The 0003 shape -- current-Congress AND not-departed in the WHERE, no
# in_office column.
_VIEW_0003 = """
CREATE VIEW current_members AS
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


def upgrade() -> None:
    op.execute(_VIEW_WITH_IN_OFFICE)


def downgrade() -> None:
    op.execute("DROP VIEW current_members")
    op.execute(_VIEW_0003)
