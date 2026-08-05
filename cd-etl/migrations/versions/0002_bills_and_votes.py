"""bills and roll call votes (legislation only)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-05 00:00:00.000000

Adds bills/bill_subjects and roll_call_votes/roll_call_vote_member_votes,
scoped deliberately to legislation only -- nominations are out of scope
(see rchacon/cd-platform#8): a member's vote on a Presidential Nomination
doesn't reveal a policy-area position the way a legislation vote does, so
nothing here models nomination votes or nominees. Every roll_call_votes row
is either linked to a bills row or unresolvable/procedural (bill_id NULL),
never a nomination.

Also adds members.lis_member_id, a crosswalk needed to resolve the Senate's
own XML vote feed (which keys members by an internal LIS id, not
bioguide_id) back to the existing members table.
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
        -- A small, constitutionally fixed taxonomy (unlike
        -- member_terms.member_type, which stays TEXT because the full
        -- space of upstream memberType values hasn't been reviewed).
        -- Both sources report from this same closed set: the House API's
        -- bare codes (HR, SJRES, ...) and the Senate XML's dotted display
        -- forms (H.R., S.J.Res., ...) both resolve onto it.
        CREATE TYPE bill_type AS ENUM (
            'HR',
            'S',
            'HJRES',
            'SJRES',
            'HCONRES',
            'SCONRES',
            'HRES',
            'SRES'
        )
        """
    )

    op.execute(
        """
        -- One row per piece of legislation, keyed by the same
        -- (congress, bill_type, bill_number) triple as the Congress.gov
        -- API path. A surrogate bill_id is used as the primary key (like
        -- member_terms' member_term_id) rather than that 3-column
        -- composite, so bill_subjects and roll_call_votes get a simple
        -- single-column foreign key instead of a 3-column one.
        --
        -- Deliberately minimal: title, sponsor, introduced_date, and
        -- latest_action are all real upstream fields but aren't stored
        -- here -- none of them are load-bearing for this table's only
        -- current purpose (deriving how a member voted on a policy area).
        -- Add them if/when a consumer actually needs them, following the
        -- same precedent as members.office_address in 0001.
        CREATE TABLE bills (
            bill_id         BIGSERIAL PRIMARY KEY,

            congress        SMALLINT NOT NULL
                REFERENCES congresses (congress),

            bill_type       bill_type NOT NULL,
            bill_number     SMALLINT NOT NULL,

            -- Scalar field directly on the bill (a controlled vocabulary),
            -- not a join table -- e.g. "Government Operations and
            -- Politics". Nullable: not every bill has been assigned one.
            policy_area     TEXT,

            -- SHA-256 hash of the normalized source fields:
            --   congress
            --   bill_type
            --   bill_number
            --   policy_area
            source_hash     TEXT NOT NULL,

            -- Timestamp reported by the upstream source (its own
            -- updateDate), used by the ETL to decide whether a bill needs
            -- re-fetching. See members.source_updated_at in 0001 for why
            -- this isn't gated on source_hash.
            source_updated_at TIMESTAMPTZ,

            synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT bills_unique_bill
                UNIQUE (congress, bill_type, bill_number),

            CONSTRAINT bills_positive_number
                CHECK (bill_number > 0)
        )
        """
    )

    op.execute(
        """
        -- A bill's legislative subject terms (a genuine one-to-many list,
        -- e.g. HR 144 has 4 subject terms), fetched from the bill's
        -- /subjects sub-resource. Distinct from policy_area, which is a
        -- single scalar field directly on the bill.
        --
        -- No source_hash/synced_at here -- unlike bills, this table has no
        -- per-row mutable content to diff. The ETL fully replaces a
        -- bill's subject rows on each sync (delete + reinsert) rather
        -- than tracking individual row changes, the same spirit as how
        -- members.party_history's array is replaced wholesale rather
        -- than diffed entry by entry.
        CREATE TABLE bill_subjects (
            bill_subject_id BIGSERIAL PRIMARY KEY,

            bill_id         BIGINT NOT NULL
                REFERENCES bills (bill_id)
                ON DELETE CASCADE,

            subject_name    TEXT NOT NULL,

            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT bill_subjects_unique_subject
                UNIQUE (bill_id, subject_name)
        )
        """
    )

    op.execute(
        """
        -- Normalized vote-cast vocabulary. House roll calls report
        -- different literal values depending on voteType ("Recorded
        -- Vote" uses Aye/No/Not Voting; "Yea-and-Nay" uses Yea/Nay/Not
        -- Voting), and the Senate's own XML feed uses Yea/Nay/Not Voting.
        -- The ETL normalizes all of these onto this single set
        -- case-insensitively (yea/aye -> YEA, nay/no -> NAY) rather than
        -- storing the source's literal string.
        CREATE TYPE vote_cast_type AS ENUM (
            'YEA',
            'NAY',
            'PRESENT',
            'NOT_VOTING'
        )
        """
    )

    op.execute(
        """
        -- One row per roll call vote, unified across the House API and
        -- the Senate's own XML feed. chamber is part of the natural key
        -- because House and Senate vote numbers are independently
        -- sequenced per chamber, not globally.
        --
        -- bill_id is nullable: a small residue of roll calls (e.g.
        -- "Elected Speaker", "On Motion to Adjourn") have no associated
        -- piece of legislation at all and are stored with bill_id NULL
        -- rather than being excluded outright. It is never NULL because
        -- of a nomination -- those are filtered out entirely before
        -- ingestion (see rchacon/cd-platform#8), never stored here.
        --
        -- result is kept TEXT rather than an enum -- like
        -- member_terms.member_type, the full space of result strings
        -- across both sources ("Passed", "Bill Passed", "Agreed to",
        -- "Rejected", ...) hasn't been exhaustively reviewed yet.
        CREATE TABLE roll_call_votes (
            roll_call_vote_id BIGSERIAL PRIMARY KEY,

            chamber         chamber_type NOT NULL,

            congress        SMALLINT NOT NULL
                REFERENCES congresses (congress),

            session         SMALLINT NOT NULL,
            vote_number     INTEGER NOT NULL,

            bill_id         BIGINT
                REFERENCES bills (bill_id),

            vote_question   TEXT NOT NULL,
            result          TEXT NOT NULL,
            vote_date       DATE NOT NULL,

            -- SHA-256 hash of the normalized source fields:
            --   chamber
            --   congress
            --   session
            --   vote_number
            --   bill_id
            --   vote_question
            --   result
            --   vote_date
            source_hash     TEXT NOT NULL,

            synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT roll_call_votes_unique_vote
                UNIQUE (chamber, congress, session, vote_number),

            CONSTRAINT roll_call_votes_valid_session
                CHECK (session IN (1, 2))
        )
        """
    )

    op.execute(
        """
        -- One row per member's cast in a roll call vote.
        --
        -- No source_hash here -- a closed historical vote's cast is
        -- immutable in practice, so the load's own
        -- ON CONFLICT ... WHERE vote_cast IS DISTINCT FROM EXCLUDED.vote_cast
        -- clause is enough to detect the one column that could ever
        -- change, without a dedicated hash column.
        CREATE TABLE roll_call_vote_member_votes (
            roll_call_vote_member_vote_id BIGSERIAL PRIMARY KEY,

            roll_call_vote_id BIGINT NOT NULL
                REFERENCES roll_call_votes (roll_call_vote_id)
                ON DELETE CASCADE,

            bioguide_id     TEXT NOT NULL
                REFERENCES members (bioguide_id)
                ON DELETE CASCADE,

            vote_cast       vote_cast_type NOT NULL,

            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            CONSTRAINT roll_call_vote_member_votes_unique_cast
                UNIQUE (roll_call_vote_id, bioguide_id)
        )
        """
    )

    op.execute(
        """
        -- Crosswalk to the Senate's own internal member numbering
        -- (Legislative Information System), needed because the Senate's
        -- XML vote feed keys each member by lis_member_id (e.g. "S428"),
        -- not bioguide_id. Populated from unitedstates/congress-legislators'
        -- legislators-current.yaml, which carries both ids per person.
        -- NULL for every House member (the LIS only covers the Senate) and
        -- for any senator not yet synced by that crosswalk. Plain UNIQUE
        -- is sufficient -- Postgres treats multiple NULLs as
        -- non-conflicting by default, and nearly all rows will be NULL.
        ALTER TABLE members
            ADD COLUMN lis_member_id TEXT UNIQUE
        """
    )

    op.execute(
        """
        -- Serves this feature's actual query: for a given member, find
        -- every vote they cast.
        CREATE INDEX idx_roll_call_vote_member_votes_bioguide
        ON roll_call_vote_member_votes (bioguide_id)
        """
    )

    op.execute(
        """
        -- Serves the reverse direction: for a given bill (and, via its
        -- policy_area, a policy area), find every vote cast on it.
        CREATE INDEX idx_roll_call_votes_bill
        ON roll_call_votes (bill_id)
        WHERE bill_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_roll_call_votes_bill")
    op.execute("DROP INDEX idx_roll_call_vote_member_votes_bioguide")
    op.execute("ALTER TABLE members DROP COLUMN lis_member_id")
    op.execute("DROP TABLE roll_call_vote_member_votes")
    op.execute("DROP TABLE roll_call_votes")
    op.execute("DROP TYPE vote_cast_type")
    op.execute("DROP TABLE bill_subjects")
    op.execute("DROP TABLE bills")
    op.execute("DROP TYPE bill_type")
