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
-- party_history stores the member's full party affiliation
-- timeline, independent of Congress or term boundaries -- it
-- mirrors the upstream partyHistory array directly. Party values
-- are not constrained by a database enum; normalization happens
-- entirely in the ETL (see PARTY_MAP in members_etl.py).
--
-- Mutable office attributes such as district are intentionally
-- stored outside this table, in member_terms.
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

    -- office_address (from the upstream addressInformation object) is
    -- deliberately not stored -- not called for by any current
    -- consumer (cd-lookup's UI only needs name/party/phone/website/
    -- photo). Add it if/when a consumer actually needs it, rather
    -- than storing it speculatively.
    phone           TEXT,
    website_url     TEXT,

    -- Full party affiliation history, independent of Congress or
    -- term boundaries. Mirrors the upstream partyHistory array:
    --   [{"party": "REPUBLICAN", "source_party_name": "Republican",
    --     "start_year": 2023, "end_year": 2026}, ...]
    --
    -- party is normalized to a small canonical set of values by
    -- the ETL (e.g. "DEMOCRATIC", "REPUBLICAN"), but is stored
    -- as plain text and not validated at the database level.
    party_history   JSONB NOT NULL DEFAULT '[]'::jsonb,

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
    --   party_history
    source_hash     TEXT NOT NULL,

    -- Timestamp reported by the upstream source indicating when
    -- the source record was last updated. Always reflects the
    -- source's current value on every sync, regardless of
    -- whether source_hash changed -- source_hash covers only a
    -- subset of fields (see above), so gating this column on it
    -- would let it lag behind the source and, since the ETL
    -- compares this value to decide whether a member needs a
    -- detail re-fetch, could cause the same member to be
    -- re-fetched forever.
    --
    -- source_hash remains the authoritative mechanism for
    -- detecting normalized data changes; this column is not a
    -- substitute for it.
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
-- Party is intentionally not stored here -- see
-- members.party_history. current_member_terms derives each
-- member's current party from that history.
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
-- Current Congress
--
-- The single source of truth for "which Congress is current."
-- Both the ETL (which needs a congress number to sync against)
-- and current_member_terms below call this function rather than
-- each independently typing the same start_date/end_date
-- predicate -- two copies of that logic could silently drift if
-- the definition of "current" ever changes (e.g. a grace period).
-- ============================================================

CREATE FUNCTION current_congress() RETURNS SMALLINT AS $$
    SELECT congress FROM congresses
    WHERE start_date <= CURRENT_DATE AND CURRENT_DATE < end_date
    ORDER BY congress DESC
    LIMIT 1
$$ LANGUAGE sql STABLE;


-- ============================================================
-- Current Member Terms
--
-- A term is current when it belongs to the Congress that
-- current_congress() returns.
--
-- Since Congress.gov currently provides only startYear and
-- endYear (not exact service dates), this view determines
-- current membership using the active Congress.
--
-- party/source_party_name reflect each member's most recent
-- members.party_history entry as of now (not as of the term's
-- start_year), so a mid-term party switch is reflected
-- immediately without needing to touch member_terms.
--
-- Joins in the biographical/contact fields a lookup consumer
-- needs (name, photo, phone, website) so this view alone is
-- enough to answer "who currently represents this district."
-- Deliberately excludes any Senior/Senator vs. Junior Senator
-- distinction -- that's based on continuous years of Senate
-- service, which isn't derivable from current-Congress-only
-- term data.
--
-- TODO:
--   This may incorrectly include members who resigned, died,
--   or were otherwise replaced during the current Congress.
--   Investigate whether Congress.gov exposes an authoritative
--   current-member indicator or exact service dates that can
--   be used to make this view precise.
-- ============================================================

CREATE VIEW current_member_terms AS
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
WHERE mt.congress = current_congress();


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
