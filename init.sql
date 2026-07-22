-- ============================================================
-- PostgreSQL 15+
--
-- Congressional member schema.
--
-- Design:
--   • congresses defines each numbered Congress.
--   • members stores one canonical identity row per person.
--   • member_terms stores each distinct period of congressional
--     service.
--   • current_member_terms derives current officeholders from
--     service dates.
-- ============================================================


-- ============================================================
-- Types
-- ============================================================

CREATE TYPE party_type AS ENUM (
    'DEMOCRATIC',
    'REPUBLICAN',
    'INDEPENDENT',
    'LIBERTARIAN',
    'GREEN',
    'NONPARTISAN',
    'OTHER'
);

CREATE TYPE chamber_type AS ENUM (
    'HOUSE',
    'SENATE'
);


-- ============================================================
-- Congresses
--
-- Date ranges use an exclusive end date:
--     start_date <= date < end_date
-- ============================================================

CREATE TABLE congresses (
    congress        SMALLINT PRIMARY KEY,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT congresses_positive_number
        CHECK (congress > 0),

    CONSTRAINT congresses_valid_date_range
        CHECK (end_date > start_date)
);


-- ============================================================
-- Members
--
-- One canonical identity row per person.
--
-- Only an authoritative canonical member-profile source should
-- update an existing member row. Historical term imports may
-- insert a missing member, but should not overwrite an existing
-- canonical identity.
-- ============================================================

CREATE TABLE members (
    bioguide_id     TEXT PRIMARY KEY,

    given_name      TEXT NOT NULL,
    family_name     TEXT NOT NULL,

    birth_date      DATE,
    death_date      DATE,

    -- Hash of normalized canonical identity fields:
    --   bioguide_id
    --   given_name
    --   family_name
    --   birth_date
    --   death_date
    source_hash     TEXT NOT NULL,

    -- Most recent successful synchronization with the canonical
    -- member-profile source.
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT members_valid_life_dates
        CHECK (
            death_date IS NULL
            OR birth_date IS NULL
            OR death_date >= birth_date
        )
);


-- ============================================================
-- Member Terms
--
-- One row per distinct period of service during a numbered
-- Congress.
--
-- Including term_start in the unique service key permits a
-- member to have multiple separate service periods for the
-- same seat during the same Congress.
--
-- district:
--   NULL = Senator
--   0    = At-large House representative
--   1+   = Numbered House district
--
-- Photos may be mirrored to S3 and referenced with photo_uri,
-- for example:
--   s3://cd-member-photos/S000033.jpg
-- ============================================================

CREATE TABLE member_terms (
    member_term_id  BIGSERIAL PRIMARY KEY,

    bioguide_id     TEXT NOT NULL
        REFERENCES members (bioguide_id)
        ON DELETE CASCADE,

    congress        SMALLINT NOT NULL
        REFERENCES congresses (congress),

    chamber         chamber_type NOT NULL,

    state           CHAR(2) NOT NULL,
    district        SMALLINT,

    party           party_type NOT NULL,

    term_start      DATE NOT NULL,
    term_end        DATE NOT NULL,

    website_url     TEXT,
    phone           TEXT,
    office_address  TEXT,
    photo_uri       TEXT,

    -- Hash of normalized source fields used to populate this
    -- specific period of service.
    source_hash     TEXT NOT NULL,

    -- Most recent successful synchronization with the source.
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT member_terms_valid_dates
        CHECK (term_end > term_start),

    CONSTRAINT member_terms_valid_district
        CHECK (
            (
                chamber = 'HOUSE'
                AND district IS NOT NULL
                AND district >= 0
            )
            OR
            (
                chamber = 'SENATE'
                AND district IS NULL
            )
        ),

    CONSTRAINT member_terms_unique_service
        UNIQUE NULLS NOT DISTINCT (
            bioguide_id,
            congress,
            chamber,
            state,
            district,
            term_start
        )
);


-- ============================================================
-- Current Member Terms
--
-- A term is current when:
--     term_start <= CURRENT_DATE < term_end
-- ============================================================

CREATE VIEW current_member_terms AS
SELECT *
FROM member_terms
WHERE term_start <= CURRENT_DATE
  AND CURRENT_DATE < term_end;


-- ============================================================
-- Indexes
-- ============================================================

-- Current representative lookup by Congress, state, and district.
CREATE INDEX idx_member_terms_house_lookup
ON member_terms (
    congress,
    state,
    district,
    term_start,
    term_end
)
WHERE chamber = 'HOUSE';


-- Current senator lookup by Congress and state.
CREATE INDEX idx_member_terms_senate_lookup
ON member_terms (
    congress,
    state,
    term_start,
    term_end
)
WHERE chamber = 'SENATE';


-- Synchronization auditing and stale-record detection.
CREATE INDEX idx_members_synced_at
ON members (synced_at);

CREATE INDEX idx_member_terms_synced_at
ON member_terms (synced_at);


-- ============================================================
-- Seed the 119th Congress
--
-- This schema seeds the current Congress for initial deployment.
-- The ETL process is responsible for discovering, inserting,
-- and updating future Congress entries from the authoritative
-- congressional data source.
-- ============================================================

INSERT INTO congresses (
    congress,
    start_date,
    end_date
)
VALUES (
    119,
    DATE '2025-01-03',
    DATE '2027-01-03'
)
ON CONFLICT (congress) DO NOTHING;
