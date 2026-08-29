"""bills.bill_key -- canonical external id for a bill

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-29 00:00:00.000000

A stable string form of the (congress, bill_type, bill_number) natural key --
"<congress>-<bill_type lowercased>-<bill_number>", e.g. "119-hr-2616",
"119-sjres-14" -- for cd-api's GET /bills/search and (later) GET
/members/{bioguide_id}/voting-record responses to expose as each bill's `id`:
the opaque handle a client reads from one response and passes back verbatim
in the next (rchacon/cd-platform#104).

A Postgres GENERATED column, not a value the ETL writes: the database derives
it once from the three columns it already stores, so cd-etl and cd-api never
assemble the string in code and can't drift on separator or casing (the same
"derived in two places" hazard db.to_pgvector_literal was written to avoid).

The bill_type piece is a spelled-out CASE rather than lower(bill_type::text):
casting an ENUM to text is only STABLE, not IMMUTABLE (labels are renamable
via ALTER TYPE), and a GENERATED expression must be immutable. The CASE covers
every value of the bill_type enum from 0002 -- itself described there as "a
small, constitutionally fixed taxonomy", so it isn't expected to grow; if a
value is ever added, add its arm here too (an unlisted value would make the
whole bill_key NULL for that row).

Deliberately NOT the primary key. bills.bill_id (BIGSERIAL) stays the PK and
roll_calls.bill_id / bill_subjects.bill_id keep their narrow single-column
foreign keys -- surrogate PK plus natural unique key, the same trade 0002 made
in choosing a surrogate bill_id over the 3-column composite. This just adds a
second, externally meaningful unique handle alongside it. bill_key uniqueness
already follows from bills_unique_bill (the lowering is 1:1); the index below
is belt-and-braces and gives cd-api a single-column key to look bills up by.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE bills ADD COLUMN bill_key TEXT
        GENERATED ALWAYS AS (
            congress::text || '-' || CASE bill_type
                WHEN 'HR'      THEN 'hr'
                WHEN 'S'       THEN 's'
                WHEN 'HJRES'   THEN 'hjres'
                WHEN 'SJRES'   THEN 'sjres'
                WHEN 'HCONRES' THEN 'hconres'
                WHEN 'SCONRES' THEN 'sconres'
                WHEN 'HRES'    THEN 'hres'
                WHEN 'SRES'    THEN 'sres'
            END || '-' || bill_number::text
        ) STORED
        """
    )
    op.execute("CREATE UNIQUE INDEX bills_bill_key_key ON bills (bill_key)")


def downgrade() -> None:
    op.execute("DROP INDEX bills_bill_key_key")
    op.execute("ALTER TABLE bills DROP COLUMN bill_key")
