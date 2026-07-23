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
-- Canonical biographical identity for a Member of Congress.
--
-- One row per Bioguide ID.
--
-- Populated from the authoritative upstream member data source.
--
-- Mutable office attributes such as district, party, phone,
-- office address, and website are intentionally stored outside
-- this table.
-- ============================================================

CREATE TABLE members (
    bioguide_id     TEXT PRIMARY KEY,

    given_name      TEXT NOT NULL,
    middle_name     TEXT,
    family_name     TEXT NOT NULL,
    nickname        TEXT,
    suffix          TEXT,

    birth_year      SMALLINT,
    death_year      SMALLINT,

    -- Official congressional portrait URI.
    photo_uri       TEXT,

    phone           TEXT,
    website_url     TEXT,

    -- SHA-256 hash of the normalized canonical identity fields:
    --   bioguide_id
    --   given_name
    --   middle_name
    --   family_name
    --   nickname
    --   suffix
    --   birth_year
    --   death_year
    --   photo_uri
    --   phone
    --   website_url
    source_hash     TEXT NOT NULL,

    -- Timestamp reported by the upstream source indicating when
    -- the source record was last updated.
    --
    -- Retained for auditing and diagnostics only.
    -- source_hash remains the authoritative mechanism for
    -- detecting normalized data changes.
    source_updated_at TIMESTAMPTZ,

    -- Most recent successful synchronization with the upstream
    -- source.
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT members_valid_life_years
        CHECK (
            death_year IS NULL
            OR birth_year IS NULL
            OR death_year >= birth_year
        )
);


-- ============================================================
-- Member Terms
--
-- One row per member's service during a numbered Congress.
--
-- Populated from the Congress.gov member detail response.
--
-- Congress.gov provides startYear and endYear rather than exact
-- service dates, so this table preserves those source values.
--
-- district:
--   NULL = Senator
--   0    = At-large House member
--   1+   = Numbered House district
--
-- member_type preserves distinctions such as:
--   Representative
--   Senator
--   Delegate
--   Resident Commissioner
--
-- party stores the normalized application value.
-- source_party_name preserves the original upstream value.
-- ============================================================

CREATE TABLE member_terms (
    member_term_id  BIGSERIAL PRIMARY KEY,

    bioguide_id     TEXT NOT NULL
        REFERENCES members (bioguide_id)
        ON DELETE CASCADE,

    congress        SMALLINT NOT NULL
        REFERENCES congresses (congress),

    chamber         chamber_type NOT NULL,

    -- Kept as TEXT until all upstream memberType values have
    -- been reviewed.
    member_type     TEXT NOT NULL,

    state           CHAR(2) NOT NULL,
    district        SMALLINT,

    party           party_type NOT NULL,

    -- Original party name reported by the upstream source.
    --
    -- Unknown values can be normalized to party = 'OTHER'
    -- without losing the original source value.
    source_party_name TEXT,

    -- Year values supplied by the upstream source.
    start_year      SMALLINT NOT NULL,
    end_year        SMALLINT,

    -- SHA-256 hash of the normalized source fields:
    --   bioguide_id
    --   congress
    --   chamber
    --   member_type
    --   state
    --   district
    --   party
    --   source_party_name
    --   start_year
    --   end_year
    source_hash     TEXT NOT NULL,

    -- Most recent successful synchronization with the upstream
    -- source.
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT member_terms_valid_years
        CHECK (
            end_year IS NULL
            OR end_year >= start_year
        ),

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
            start_year
        )
);


-- ============================================================
-- Current Member Terms
--
-- A term is current when it belongs to the Congress whose date
-- range contains CURRENT_DATE.
--
-- Since Congress.gov currently provides only startYear and
-- endYear (not exact service dates), this view determines
-- current membership using the active Congress.
--
-- TODO:
--   This may incorrectly include members who resigned, died,
--   or were otherwise replaced during the current Congress.
--   Investigate whether Congress.gov exposes an authoritative
--   current-member indicator or exact service dates that can
--   be used to make this view precise.
-- ============================================================

CREATE VIEW current_member_terms AS
SELECT mt.*
FROM member_terms AS mt
JOIN congresses AS c
    ON c.congress = mt.congress
WHERE c.start_date <= CURRENT_DATE
  AND CURRENT_DATE < c.end_date;


-- ============================================================
-- Indexes
-- ============================================================

CREATE INDEX idx_members_family_name
    ON members (family_name);

-- House member lookup by Congress, state, and district.
CREATE INDEX idx_member_terms_house_lookup
ON member_terms (
    congress,
    state,
    district
)
WHERE chamber = 'HOUSE';


-- Senate member lookup by Congress and state.
CREATE INDEX idx_member_terms_senate_lookup
ON member_terms (
    congress,
    state
)
WHERE chamber = 'SENATE';


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
