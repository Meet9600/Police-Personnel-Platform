-- ============================================================================
-- Gujarat Police Management AI — CLEAN ANALYTICAL STORE (schema: clean)
-- Target: PostgreSQL 17
--
-- Design principles:
--   * NON-DESTRUCTIVE: every normalized column keeps its original in *_raw.
--   * NEVER deletes source data; missing values become explicit NULL + flags.
--   * Namespaced person_id ('O-<id>' / 'E-<id>') so officer & employee id
--     ranges (which overlap) never collide.
--   * Transfers UPDATE current posting + APPEND a history row (no duplicate
--     person). Genuinely-new source ids APPEND a new person.
--   * Separate from the source DB. The source 'public' schema is read-only.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS clean;
SET search_path TO clean, public;

-- ---------------------------------------------------------------------------
-- Controlled vocabularies (reference data, populated by the pipeline)
-- ---------------------------------------------------------------------------

-- Canonical rank vocabulary. rank_order lets us compare seniority numerically.
CREATE TABLE IF NOT EXISTS clean.rank_ref (
    rank_code      TEXT PRIMARY KEY,         -- PI, PSI, UASI, UHC, ...
    rank_band      TEXT NOT NULL,            -- 'officer' | 'employee'
    rank_name_en   TEXT NOT NULL,
    rank_name_gu   TEXT,
    rank_order     INTEGER NOT NULL          -- higher = more senior
);

-- Specialization categories that duties map onto.
CREATE TABLE IF NOT EXISTS clean.specialization_ref (
    spec_code      TEXT PRIMARY KEY,         -- CYBER, TRAFFIC, INVESTIGATION...
    spec_name_en   TEXT NOT NULL,
    category       TEXT NOT NULL             -- broad grouping for the UI/filters
);

-- Maps each raw duty_detail (Gujarati) -> a specialization code.
-- Built once from duty_details; unmapped duties get spec_code = 'UNCLASSIFIED'
-- and are surfaced for human review rather than dropped.
CREATE TABLE IF NOT EXISTS clean.duty_map (
    source_duty_id   INTEGER PRIMARY KEY,
    duty_detail_raw  TEXT NOT NULL,          -- original Gujarati, preserved
    duty_detail_en   TEXT,                   -- transliterated/translated label
    spec_code        TEXT REFERENCES clean.specialization_ref(spec_code),
    needs_review     BOOLEAN NOT NULL DEFAULT FALSE
);

-- ---------------------------------------------------------------------------
-- Org hierarchy
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clean.dim_division (
    division_id    INTEGER PRIMARY KEY,
    name_raw       TEXT,
    name_en        TEXT,
    is_active      BOOLEAN
);

CREATE TABLE IF NOT EXISTS clean.dim_station (
    station_id     INTEGER PRIMARY KEY,      -- = police_station_branch.id
    division_id    INTEGER REFERENCES clean.dim_division(division_id),
    name_raw       TEXT,
    name_en        TEXT,
    is_active      BOOLEAN
);

-- ---------------------------------------------------------------------------
-- PERSON: one canonical row per human (officer or employee)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clean.person (
    person_id          TEXT PRIMARY KEY,     -- 'O-31', 'E-1087'
    source_table       TEXT NOT NULL,        -- 'officer_details'|'employee_details'
    source_id          INTEGER NOT NULL,
    rank_band          TEXT NOT NULL,        -- 'officer' | 'employee'
    rank_code          TEXT REFERENCES clean.rank_ref(rank_code),
    rank_raw           TEXT,                 -- original designation

    full_name_raw      TEXT,                 -- original (Gujarati, with honorific)
    full_name_gu       TEXT,                 -- name with honorific/rank suffix stripped
    honorific          TEXT,                 -- શ્રી / સુશ્રી etc.

    current_station_id INTEGER REFERENCES clean.dim_station(station_id),
    gender             TEXT,
    dob                DATE,
    age_years          INTEGER,              -- derived from dob; NULL if no dob
    appointment_date   DATE,
    years_of_service   NUMERIC(5,2),         -- derived; NULL if no appt date
    study_raw          TEXT,
    study_en           TEXT,                 -- normalized qualification
    batch_raw          TEXT,                 -- original (year/date/guj-numeral)
    batch_year         INTEGER,              -- parsed 4-digit year, else NULL

    missing_flags      TEXT[] NOT NULL DEFAULT '{}',  -- e.g. {dob,appointment_date}
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    first_loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (source_table, source_id)
);

CREATE INDEX IF NOT EXISTS ix_person_rank   ON clean.person(rank_code);
CREATE INDEX IF NOT EXISTS ix_person_station ON clean.person(current_station_id);

-- ---------------------------------------------------------------------------
-- POSTING HISTORY: append-only; fed by service periods + transfer records
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clean.person_posting_history (
    id             BIGSERIAL PRIMARY KEY,
    person_id      TEXT NOT NULL REFERENCES clean.person(person_id),
    place_raw      TEXT,                     -- original Gujarati place/station
    place_en       TEXT,
    start_date     DATE,
    end_date       DATE,
    remarks        TEXT,
    source_table   TEXT NOT NULL,
    source_id      INTEGER NOT NULL,
    UNIQUE (source_table, source_id)         -- idempotent re-runs
);
CREATE INDEX IF NOT EXISTS ix_posting_person ON clean.person_posting_history(person_id);

-- ---------------------------------------------------------------------------
-- SPECIALIZATION: derived duty tags per person (many-to-many)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clean.person_specialization (
    person_id      TEXT NOT NULL REFERENCES clean.person(person_id),
    spec_code      TEXT NOT NULL REFERENCES clean.specialization_ref(spec_code),
    duty_detail_raw TEXT,
    PRIMARY KEY (person_id, spec_code, duty_detail_raw)
);
CREATE INDEX IF NOT EXISTS ix_spec_code ON clean.person_specialization(spec_code);

-- ---------------------------------------------------------------------------
-- PERFORMANCE: aggregated awards / punishments per person
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clean.person_performance (
    person_id        TEXT PRIMARY KEY REFERENCES clean.person(person_id),
    awards_count     INTEGER NOT NULL DEFAULT 0,
    punishments_count INTEGER NOT NULL DEFAULT 0,
    last_award_date  DATE,
    last_punishment_date DATE
);

-- ---------------------------------------------------------------------------
-- STATION CAPACITY: approved vs present strength (availability signal)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clean.station_capacity (
    station_id       INTEGER NOT NULL REFERENCES clean.dim_station(station_id),
    rank_band        TEXT NOT NULL,          -- 'officer' | 'employee'
    approved_total   BIGINT,
    present_total    BIGINT,
    vacancy          BIGINT,                 -- approved - present
    PRIMARY KEY (station_id, rank_band)
);

-- ---------------------------------------------------------------------------
-- FLAT ML FEATURE VIEW  ("one tabular form" deliverable)
-- This is what the recommendation engine and analytics read from.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW clean.vw_ml_features AS
SELECT
    p.person_id,
    p.rank_band,
    p.rank_code,
    r.rank_order,
    p.gender,
    p.age_years,
    p.years_of_service,
    p.batch_year,
    p.current_station_id,
    s.name_en              AS station_en,
    s.division_id,
    COALESCE(perf.awards_count, 0)       AS awards_count,
    COALESCE(perf.punishments_count, 0)  AS punishments_count,
    -- specialization codes as an array, for filtering/matching
    COALESCE(
        (SELECT array_agg(DISTINCT ps.spec_code)
         FROM clean.person_specialization ps WHERE ps.person_id = p.person_id),
        '{}'
    )                       AS specializations,
    -- count of distinct postings = breadth of experience
    COALESCE(
        (SELECT count(DISTINCT pph.place_en)
         FROM clean.person_posting_history pph WHERE pph.person_id = p.person_id),
        0
    )                       AS distinct_postings,
    p.missing_flags,
    p.is_active
FROM clean.person p
LEFT JOIN clean.rank_ref r       ON r.rank_code = p.rank_code
LEFT JOIN clean.dim_station s     ON s.station_id = p.current_station_id
LEFT JOIN clean.person_performance perf ON perf.person_id = p.person_id;
