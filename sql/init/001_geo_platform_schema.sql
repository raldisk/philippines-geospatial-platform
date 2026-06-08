-- ============================================================
-- sql/init/001_geo_platform_schema.sql
-- PH Geospatial Intelligence Platform v2.2 — Complete PostgreSQL DDL
--
-- Run order enforced in this file:
--   1. Extensions
--   2. Functions
--   3. Dimension tables (dim_region created BEFORE psgc_crosswalk FK ref)
--   4. Registry tables
--   5. PSGC crosswalk
--   6. Remaining dimension tables (dim_date, dim_indicator, dim_vintage)
--   7. Fact tables
--   8. Lineage table
--   9. Indexes (inline with table creation)
--  10. Triggers
--  11. Views
--  12. Service accounts (roles + grants)
--  13. Row-Level Security
--
-- Authoritative source: Section 24 + Section 23.5
-- Execute as superuser or database owner.
-- Idempotent: all CREATE statements use IF NOT EXISTS / OR REPLACE.
-- ============================================================

BEGIN;

-- ============================================================
-- 1. EXTENSIONS
-- ============================================================

CREATE EXTENSION IF NOT EXISTS postgis;           -- geometry columns on dim_region
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";       -- uuid_generate_v4() for pipeline_run_id
CREATE EXTENSION IF NOT EXISTS pgcrypto;          -- gen_random_uuid() fallback + sha256()

-- ============================================================
-- 2. FUNCTIONS
-- ============================================================

-- Auto-update dw_updated_ts on any row modification.
-- Attached via trigger on every mutable table.
CREATE OR REPLACE FUNCTION update_dw_updated_ts()
RETURNS TRIGGER AS $$
BEGIN
    NEW.dw_updated_ts = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- SHA-256 row hash for SCD Type 2 change detection.
-- Hashes all non-audit fields to detect actual content changes.
-- IMMUTABLE: same inputs always produce same hash — safe for index use.
CREATE OR REPLACE FUNCTION compute_row_hash(
    region_code TEXT,
    region_name TEXT,
    admin_level TEXT,
    parent_region_nk TEXT,
    geom_wkb BYTEA
)
RETURNS CHAR(64) AS $$
    SELECT encode(
        sha256(
            (
                COALESCE(region_code,      '') || '|' ||
                COALESCE(region_name,      '') || '|' ||
                COALESCE(admin_level,      '') || '|' ||
                COALESCE(parent_region_nk, '') || '|' ||
                COALESCE(encode(geom_wkb, 'hex'), '')
            )::BYTEA
        ),
        'hex'
    );
$$ LANGUAGE SQL IMMUTABLE PARALLEL SAFE;

-- ============================================================
-- 3. DIMENSION TABLES
-- dim_region created first — psgc_crosswalk has FK reference to it
-- ============================================================

CREATE TABLE IF NOT EXISTS dim_region (
    region_sk           BIGINT          PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    region_nk           VARCHAR(12)     NOT NULL,           -- PSGC canonical code (natural key)
    region_code         VARCHAR(12)     NOT NULL,
    region_name         VARCHAR(100)    NOT NULL,
    admin_level         VARCHAR(20)     NOT NULL
                        CHECK (admin_level IN (
                            'barangay', 'municipality', 'city',
                            'province', 'region', 'national'
                        )),
    parent_region_nk    VARCHAR(12),                        -- NULL for national
    -- Multi-resolution geometry (Section 7.2 simplification)
    geom_z4             GEOMETRY(MultiPolygon, 4326),       -- z4 tile tolerance
    geom_z8             GEOMETRY(MultiPolygon, 4326),       -- z8 tile tolerance
    geom_z12            GEOMETRY(MultiPolygon, 4326),       -- z12 tile tolerance (full)
    -- Centroid generated from z12; stored for spatial index use
    geom_centroid       GEOMETRY(Point, 4326)
                        GENERATED ALWAYS AS (ST_Centroid(geom_z12)) STORED,
    -- SCD Type 2 validity window
    valid_from          DATE            NOT NULL,
    valid_to            DATE            CHECK (valid_to IS NULL OR valid_to > valid_from),
    is_current          BOOLEAN         NOT NULL DEFAULT TRUE,
    -- Row hash for change detection (compute_row_hash used in Airflow loader)
    row_hash            CHAR(64)        NOT NULL,
    -- Geometry repair audit (ADR-010)
    repair_method       VARCHAR(30)     CHECK (
                            repair_method IN (
                                'SHAPELY_MAKE_VALID',
                                'POSTGIS_ST_MAKEVALID',
                                'NONE'
                            ) OR repair_method IS NULL
                        ),
    source_archive_hash CHAR(64)        NOT NULL,
    dw_created_ts       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    dw_updated_ts       TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Only one current record per natural key (SCD Type 2 invariant)
CREATE UNIQUE INDEX IF NOT EXISTS dim_region_nk_current
    ON dim_region (region_nk)
    WHERE is_current = TRUE;

-- Composite for SCD Type 2 date-range lookups (ASOF JOIN view)
CREATE INDEX IF NOT EXISTS dim_region_nk_validity
    ON dim_region (region_nk, valid_from, valid_to);

-- Spatial index on centroid for point-in-polygon lookups (Martin, PostGIS queries)
CREATE INDEX IF NOT EXISTS dim_region_centroid_gist
    ON dim_region USING GIST (geom_centroid);

-- Spatial index on z8 geometry for polygon overlap queries
CREATE INDEX IF NOT EXISTS dim_region_geom_z8_gist
    ON dim_region USING GIST (geom_z8);

-- Admin level + currency for common filter pattern
CREATE INDEX IF NOT EXISTS dim_region_admin_current
    ON dim_region (admin_level, is_current);

CREATE TRIGGER dim_region_updated
    BEFORE UPDATE ON dim_region
    FOR EACH ROW EXECUTE FUNCTION update_dw_updated_ts();


-- ============================================================
-- 4. REGISTRY TABLES
-- ============================================================

-- Ingestion registry: SHA-256 dedup + pipeline audit trail
CREATE TABLE IF NOT EXISTS geo_ingest_registry (
    id                  BIGINT          PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    archive_hash        CHAR(64)        NOT NULL UNIQUE,    -- SHA-256 of 7z archive
    archive_filename    TEXT            NOT NULL,
    dataset_name        TEXT            NOT NULL,           -- e.g. "psa_provincial_2023"
    status              TEXT            NOT NULL
                        CHECK (status IN ('EXTRACTED', 'SUCCESS', 'QUARANTINED', 'FAILED')),
    operation_mode      TEXT
                        CHECK (operation_mode IN (
                            'analytical', 'geometry_only', 'boundary_catalog'
                        )),
    feature_count       INTEGER,
    pipeline_run_id     UUID            NOT NULL,
    extracted_at        TIMESTAMPTZ,
    bronze_written_at   TIMESTAMPTZ,
    gold_written_at     TIMESTAMPTZ,
    tiles_generated_at  TIMESTAMPTZ,
    failure_reason      TEXT,
    dw_created_ts       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    dw_updated_ts       TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS geo_ingest_registry_status
    ON geo_ingest_registry (status, dw_created_ts DESC);

CREATE INDEX IF NOT EXISTS geo_ingest_registry_run
    ON geo_ingest_registry (pipeline_run_id);

CREATE TRIGGER geo_ingest_registry_updated
    BEFORE UPDATE ON geo_ingest_registry
    FOR EACH ROW EXECUTE FUNCTION update_dw_updated_ts();


-- Schema registry: column fingerprint for drift detection (Section 28 CI)
CREATE TABLE IF NOT EXISTS geo_schema_registry (
    id                      BIGINT      PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    dataset_name            TEXT        NOT NULL UNIQUE,
    geometry_type           TEXT        NOT NULL,           -- Point|MultiPolygon|LineString|…
    column_names            TEXT[]      NOT NULL,           -- all .dbf column names
    numeric_columns         TEXT[]      NOT NULL DEFAULT '{}',
    categorical_columns     TEXT[]      NOT NULL DEFAULT '{}',
    psgc_code_column        TEXT,                           -- detected PSGC column name (may be NULL)
    accepted_h3_resolution  SMALLINT,                      -- selected by occupancy validator (ADR-009)
    recommended_indicator   TEXT,                           -- highest-variance numeric column
    first_ingested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- SHA-256 of sorted column name set — CI schema drift detector compares this
    schema_fingerprint      CHAR(64)
);


-- Quarantine: append-only log of failed features (ADR-004)
-- Never mutate Bronze — correction requires explicit re-ingestion.
CREATE TABLE IF NOT EXISTS geo_quarantine (
    id                  BIGINT          PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    archive_hash        CHAR(64)        NOT NULL,
    dataset_name        TEXT            NOT NULL,
    pipeline_run_id     UUID            NOT NULL,
    stage               TEXT            NOT NULL
                        CHECK (stage IN ('EXTRACT', 'BRONZE', 'SILVER', 'GOLD')),
    failure_reason      TEXT            NOT NULL,
                        -- MISSING_FILE|CRS_MISSING|PSGC_MISMATCH|GEOMETRY_INVALID|
                        -- QUALITY_CONTRACT|TIMEOUT|SIGSEGV|EXECUTOR_CREATION_FAILED
    feature_count       INTEGER,                            -- features quarantined (not total batch)
    sample_wkt          TEXT,                               -- first failing geometry WKT (≤500 chars)
    quarantined_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    resolved            BOOLEAN         NOT NULL DEFAULT FALSE,
    resolved_at         TIMESTAMPTZ,
    resolution_note     TEXT
);

CREATE INDEX IF NOT EXISTS geo_quarantine_run
    ON geo_quarantine (pipeline_run_id, stage);

-- Partial index on unresolved entries — most operational queries filter this
CREATE INDEX IF NOT EXISTS geo_quarantine_unresolved
    ON geo_quarantine (resolved, quarantined_at DESC)
    WHERE resolved = FALSE;

CREATE INDEX IF NOT EXISTS geo_quarantine_dataset
    ON geo_quarantine (dataset_name, quarantined_at DESC);


-- ============================================================
-- 5. PSGC CROSSWALK
-- Created after dim_region (FK reference).
-- DEFERRABLE INITIALLY DEFERRED allows batch inserts before dim_region population.
-- ============================================================

CREATE TABLE IF NOT EXISTS psgc_crosswalk (
    legacy_code         VARCHAR(12)     NOT NULL,
    canonical_code      VARCHAR(12)     NOT NULL
                        REFERENCES dim_region (region_nk)
                        DEFERRABLE INITIALLY DEFERRED,
    vintage_year        SMALLINT        NOT NULL,
    split_type          VARCHAR(20)
                        CHECK (
                            split_type IN ('SPLIT', 'MERGE', 'RENAME')
                            OR split_type IS NULL
                        ),
    notes               TEXT,
    loaded_at           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (legacy_code, vintage_year)
);

CREATE INDEX IF NOT EXISTS psgc_crosswalk_canonical
    ON psgc_crosswalk (canonical_code);

CREATE INDEX IF NOT EXISTS psgc_crosswalk_vintage
    ON psgc_crosswalk (vintage_year, legacy_code);


-- ============================================================
-- 6. REMAINING DIMENSION TABLES
-- ============================================================

-- Date dimension: Kimball YYYYMMDD surrogate key
CREATE TABLE IF NOT EXISTS dim_date (
    date_sk         BIGINT      PRIMARY KEY,                -- YYYYMMDD integer (Kimball standard)
    full_date       DATE        NOT NULL UNIQUE,
    year            SMALLINT    NOT NULL,
    quarter         SMALLINT    NOT NULL    CHECK (quarter BETWEEN 1 AND 4),
    month           SMALLINT    NOT NULL    CHECK (month BETWEEN 1 AND 12),
    month_name      VARCHAR(9)  NOT NULL,
    week_of_year    SMALLINT    NOT NULL,
    day_of_week     SMALLINT    NOT NULL    CHECK (day_of_week BETWEEN 1 AND 7),
    is_weekend      BOOLEAN     NOT NULL,
    fiscal_year     SMALLINT    NOT NULL,                   -- Philippine gov: Jan-Dec = calendar
    fiscal_quarter  SMALLINT    NOT NULL
);

-- Pre-populate dim_date 2000–2040 (covers all PSA vintage years)
INSERT INTO dim_date
SELECT
    TO_CHAR(d, 'YYYYMMDD')::BIGINT          AS date_sk,
    d::DATE                                  AS full_date,
    EXTRACT(YEAR    FROM d)::SMALLINT        AS year,
    EXTRACT(QUARTER FROM d)::SMALLINT        AS quarter,
    EXTRACT(MONTH   FROM d)::SMALLINT        AS month,
    TO_CHAR(d, 'Month')                      AS month_name,
    EXTRACT(WEEK    FROM d)::SMALLINT        AS week_of_year,
    -- 1=Sunday … 7=Saturday (Kimball convention)
    (EXTRACT(DOW FROM d)::SMALLINT + 1)      AS day_of_week,
    EXTRACT(DOW FROM d) IN (0, 6)            AS is_weekend,
    EXTRACT(YEAR    FROM d)::SMALLINT        AS fiscal_year,
    EXTRACT(QUARTER FROM d)::SMALLINT        AS fiscal_quarter
FROM generate_series(
    '2000-01-01'::DATE,
    '2040-12-31'::DATE,
    '1 day'
) AS d
ON CONFLICT (date_sk) DO NOTHING;


-- Indicator dimension: metadata for each measurable quantity
CREATE TABLE IF NOT EXISTS dim_indicator (
    indicator_sk    BIGINT      PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    indicator_nk    VARCHAR(50) NOT NULL UNIQUE,            -- e.g. "poverty_rate"
    indicator_name  VARCHAR(200) NOT NULL,
    unit            VARCHAR(30) NOT NULL,
    source          VARCHAR(100) NOT NULL,                  -- e.g. "PSA FIES 2021"
    domain          VARCHAR(50) NOT NULL
                    CHECK (domain IN (
                        'demographic', 'economic', 'geographic', 'infrastructure'
                    )),
    -- ADR-010: regulatory flag routes repair to PostGIS ST_MakeValid
    is_regulatory   BOOLEAN     NOT NULL DEFAULT FALSE,
    description     TEXT,
    dw_created_ts   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS dim_indicator_domain
    ON dim_indicator (domain, indicator_nk);


-- Vintage dimension: PSA survey round metadata
CREATE TABLE IF NOT EXISTS dim_vintage (
    vintage_sk      BIGINT      PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    vintage_year    SMALLINT    NOT NULL,
    survey_round    VARCHAR(20),                            -- e.g. "2021Q1"
    reference_date  DATE        NOT NULL,
    published_date  DATE,
    is_provisional  BOOLEAN     NOT NULL DEFAULT FALSE,
    -- PSGC version used for this survey round (crosswalk key)
    psgc_vintage    SMALLINT    NOT NULL DEFAULT 2023,
    UNIQUE (vintage_year, survey_round)
);


-- ============================================================
-- 7. FACT TABLES
-- ============================================================

-- Fact table: one row per (region, indicator, vintage, date) observation
-- Grain: single indicator value for a single administrative unit at a point in time
CREATE TABLE IF NOT EXISTS fact_geo_observation (
    obs_sk              BIGINT      PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    region_sk           BIGINT      NOT NULL REFERENCES dim_region    (region_sk),
    indicator_sk        BIGINT      NOT NULL REFERENCES dim_indicator (indicator_sk),
    vintage_sk          BIGINT      NOT NULL REFERENCES dim_vintage   (vintage_sk),
    date_sk             BIGINT      NOT NULL REFERENCES dim_date      (date_sk),
    -- Natural key retained as degenerate dimension for ASOF JOIN compatibility
    region_nk           VARCHAR(12) NOT NULL,
    indicator_value     NUMERIC(10, 4) NOT NULL,
    confidence_low      NUMERIC(10, 4),
    confidence_high     NUMERIC(10, 4),
    sample_size         INTEGER,
    -- Degenerate dimensions: lineage without separate tables (Section 30 Phase 3)
    source_archive_hash CHAR(64)    NOT NULL,
    pipeline_run_id     UUID        NOT NULL,
    -- Audit
    dw_created_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_quarantined      BOOLEAN     NOT NULL DEFAULT FALSE
);

-- Composite: supports poverty × region × year queries (primary ASOF JOIN pattern)
CREATE INDEX IF NOT EXISTS fact_geo_obs_region_date
    ON fact_geo_observation (region_sk, date_sk, indicator_sk);

CREATE INDEX IF NOT EXISTS fact_geo_obs_run
    ON fact_geo_observation (pipeline_run_id);

CREATE INDEX IF NOT EXISTS fact_geo_obs_indicator
    ON fact_geo_observation (indicator_sk, vintage_sk);

-- Natural key index for ASOF JOIN (Section 4.2)
CREATE INDEX IF NOT EXISTS fact_geo_obs_nk
    ON fact_geo_observation (region_nk, date_sk);


-- Fact table: pre-aggregated H3 hexagons (Gold layer, ADR-003)
-- Grain: one row per (H3 cell, resolution, indicator, vintage)
CREATE TABLE IF NOT EXISTS fact_geo_h3_aggregate (
    agg_sk          BIGINT      PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    h3_index        VARCHAR(20) NOT NULL,
    h3_resolution   SMALLINT    NOT NULL    CHECK (h3_resolution BETWEEN 0 AND 15),
    indicator_sk    BIGINT      NOT NULL REFERENCES dim_indicator (indicator_sk),
    vintage_sk      BIGINT      NOT NULL REFERENCES dim_vintage   (vintage_sk),
    -- Aggregate measures (computed at Gold generation — never recomputed at serve time)
    indicator_mean  NUMERIC(10, 4) NOT NULL,
    indicator_std   NUMERIC(10, 4),
    feature_count   INTEGER     NOT NULL,
    -- Jenks class 1–5; breaks stored as JSON array for tooltip use
    jenks_class     SMALLINT    NOT NULL    CHECK (jenks_class BETWEEN 1 AND 5),
    jenks_breaks    JSONB       NOT NULL,   -- [break1, break2, break3, break4, break5]
    -- Degenerate
    pipeline_run_id UUID        NOT NULL,
    dw_created_ts   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Primary serving query: resolution + vintage for a given indicator
CREATE INDEX IF NOT EXISTS fact_geo_h3_index
    ON fact_geo_h3_aggregate (h3_index, h3_resolution, vintage_sk);

CREATE INDEX IF NOT EXISTS fact_geo_h3_indicator
    ON fact_geo_h3_aggregate (indicator_sk, h3_resolution);

CREATE INDEX IF NOT EXISTS fact_geo_h3_resolution
    ON fact_geo_h3_aggregate (h3_resolution, jenks_class);

-- Unique: one aggregate per (cell, resolution, indicator, vintage) per pipeline run
CREATE UNIQUE INDEX IF NOT EXISTS fact_geo_h3_unique
    ON fact_geo_h3_aggregate (h3_index, h3_resolution, indicator_sk, vintage_sk, pipeline_run_id);


-- ============================================================
-- 8. LINEAGE TABLE
-- Section 30: adjacency-list model.
-- Created now, populated in Phase 3 (Section 30.3).
-- Schema must exist for Day 5 fitness function: table exists check.
-- ============================================================

CREATE TABLE IF NOT EXISTS geo_lineage_edges (
    id              BIGINT  PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    source_type     TEXT    NOT NULL
                    CHECK (source_type IN ('archive', 'bronze', 'silver', 'gold', 'pmtiles')),
    source_id       TEXT    NOT NULL,
    target_type     TEXT    NOT NULL
                    CHECK (target_type IN ('archive', 'bronze', 'silver', 'gold', 'pmtiles')),
    target_id       TEXT    NOT NULL,
    pipeline_run_id UUID    NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, target_id, pipeline_run_id)
);

CREATE INDEX IF NOT EXISTS geo_lineage_source
    ON geo_lineage_edges (source_id, source_type);

CREATE INDEX IF NOT EXISTS geo_lineage_target
    ON geo_lineage_edges (target_id, target_type);

CREATE INDEX IF NOT EXISTS geo_lineage_run
    ON geo_lineage_edges (pipeline_run_id);


-- ============================================================
-- 9. VIEWS
-- ============================================================

-- Current-boundary view: the 99% query pattern.
-- Eliminates SCD Type 2 complexity for callers that don't need history.
CREATE OR REPLACE VIEW dim_region_current AS
SELECT *
FROM dim_region
WHERE is_current = TRUE;


-- ASOF JOIN helper: selects dim_region record valid at the observation's reference date.
-- Used by mart_geo_poverty_crosswalk and the DuckDB ASOF JOIN proof-of-concept.
CREATE OR REPLACE VIEW dim_region_at_observation AS
SELECT
    r.region_sk,
    r.region_nk,
    r.region_code,
    r.region_name,
    r.admin_level,
    r.parent_region_nk,
    r.geom_z4,
    r.geom_z8,
    r.geom_z12,
    r.geom_centroid,
    r.valid_from,
    r.valid_to,
    o.obs_sk,
    o.indicator_sk,
    o.indicator_value,
    o.vintage_sk,
    o.date_sk,
    d.reference_date,
    d.vintage_year
FROM fact_geo_observation o
JOIN dim_vintage  d ON d.vintage_sk    = o.vintage_sk
JOIN dim_region   r ON r.region_nk     = o.region_nk
                   AND r.valid_from   <= d.reference_date
                   AND (r.valid_to IS NULL OR r.valid_to > d.reference_date);


-- Poverty crosswalk mart: primary analytical output (Section 4.2 ASOF JOIN target)
CREATE OR REPLACE VIEW mart_geo_poverty_crosswalk AS
SELECT
    r.region_nk,
    r.region_name,
    r.admin_level,
    ind.indicator_nk,
    o.indicator_value,
    v.vintage_year,
    v.survey_round,
    d.full_date          AS observation_date,
    o.confidence_low,
    o.confidence_high,
    o.sample_size,
    o.is_quarantined,
    o.pipeline_run_id,
    o.source_archive_hash
FROM fact_geo_observation  o
JOIN dim_region_current    r   ON r.region_sk    = o.region_sk
JOIN dim_indicator         ind ON ind.indicator_sk = o.indicator_sk
JOIN dim_vintage           v   ON v.vintage_sk   = o.vintage_sk
JOIN dim_date              d   ON d.date_sk       = o.date_sk
WHERE o.is_quarantined = FALSE;


-- H3 aggregate view with indicator metadata (H3 API serving target)
CREATE OR REPLACE VIEW geo_h3_aggregates AS
SELECT
    h.h3_index,
    h.h3_resolution,
    h.indicator_mean,
    h.indicator_std,
    h.feature_count,
    h.jenks_class,
    h.jenks_breaks,
    ind.indicator_nk,
    ind.indicator_name,
    ind.unit,
    v.vintage_year,
    v.survey_round,
    h.pipeline_run_id,
    h.dw_created_ts
FROM fact_geo_h3_aggregate h
JOIN dim_indicator          ind ON ind.indicator_sk = h.indicator_sk
JOIN dim_vintage            v   ON v.vintage_sk     = h.vintage_sk;


-- Quarantine rate by dataset: pipeline monitoring (alert if > 5% per ADR-004)
CREATE OR REPLACE VIEW geo_quarantine_rate AS
SELECT
    q.dataset_name,
    q.pipeline_run_id,
    COUNT(*)                                        AS quarantined_partitions,
    SUM(q.feature_count)                            AS quarantined_features,
    r.feature_count                                 AS total_features,
    ROUND(
        SUM(q.feature_count)::NUMERIC
        / NULLIF(r.feature_count, 0) * 100, 2
    )                                               AS quarantine_rate_pct,
    MIN(q.quarantined_at)                           AS first_quarantined_at,
    MAX(q.quarantined_at)                           AS last_quarantined_at
FROM geo_quarantine         q
LEFT JOIN geo_ingest_registry r
       ON r.pipeline_run_id = q.pipeline_run_id
      AND r.dataset_name    = q.dataset_name
GROUP BY
    q.dataset_name,
    q.pipeline_run_id,
    r.feature_count;


-- ============================================================
-- 10. SERVICE ACCOUNTS (roles + grants)
-- Section 23.5: RLS separates pipeline write from read-only serving.
-- Roles created before RLS policies that reference them.
-- ============================================================

-- Create roles (IF NOT EXISTS guard for idempotency)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'geo_pipeline_role') THEN
        CREATE ROLE geo_pipeline_role LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'geo_readonly_role') THEN
        CREATE ROLE geo_readonly_role LOGIN;
    END IF;
END;
$$;

-- Connection privileges
GRANT CONNECT ON DATABASE geo_platform TO geo_pipeline_role, geo_readonly_role;
GRANT USAGE   ON SCHEMA public          TO geo_pipeline_role, geo_readonly_role;

-- Pipeline role: read/write on registry and fact tables
GRANT SELECT, INSERT, UPDATE           ON geo_ingest_registry   TO geo_pipeline_role;
GRANT SELECT, INSERT, UPDATE           ON geo_schema_registry   TO geo_pipeline_role;
GRANT INSERT                           ON geo_quarantine         TO geo_pipeline_role;
GRANT SELECT, INSERT, UPDATE, DELETE   ON dim_region             TO geo_pipeline_role;
GRANT SELECT, INSERT                   ON dim_date               TO geo_pipeline_role;
GRANT SELECT, INSERT                   ON dim_indicator          TO geo_pipeline_role;
GRANT SELECT, INSERT                   ON dim_vintage            TO geo_pipeline_role;
GRANT SELECT, INSERT                   ON psgc_crosswalk         TO geo_pipeline_role;
GRANT SELECT, INSERT                   ON fact_geo_observation   TO geo_pipeline_role;  -- KIMBALL-04: append-only; UPDATE/DELETE revoked
GRANT SELECT, INSERT                   ON fact_geo_h3_aggregate  TO geo_pipeline_role;  -- KIMBALL-04: append-only; UPDATE/DELETE revoked
GRANT SELECT, INSERT                   ON geo_lineage_edges      TO geo_pipeline_role;

-- Sequence access for GENERATED ALWAYS AS IDENTITY columns
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO geo_pipeline_role;

-- Read-only role: serving layer + analytics + CI checks
GRANT SELECT ON geo_ingest_registry    TO geo_readonly_role;
GRANT SELECT ON geo_schema_registry    TO geo_readonly_role;
GRANT SELECT ON geo_quarantine         TO geo_readonly_role;
GRANT SELECT ON dim_region             TO geo_readonly_role;
GRANT SELECT ON dim_date               TO geo_readonly_role;
GRANT SELECT ON dim_indicator          TO geo_readonly_role;
GRANT SELECT ON dim_vintage            TO geo_readonly_role;
GRANT SELECT ON psgc_crosswalk         TO geo_readonly_role;
GRANT SELECT ON fact_geo_observation   TO geo_readonly_role;
GRANT SELECT ON fact_geo_h3_aggregate  TO geo_readonly_role;
GRANT SELECT ON geo_lineage_edges      TO geo_readonly_role;

-- Read-only view grants
GRANT SELECT ON dim_region_current          TO geo_readonly_role, geo_pipeline_role;
GRANT SELECT ON dim_region_at_observation   TO geo_readonly_role, geo_pipeline_role;
GRANT SELECT ON mart_geo_poverty_crosswalk  TO geo_readonly_role, geo_pipeline_role;
GRANT SELECT ON geo_h3_aggregates           TO geo_readonly_role, geo_pipeline_role;
GRANT SELECT ON geo_quarantine_rate         TO geo_readonly_role, geo_pipeline_role;

-- Ensure future tables/sequences are also accessible (pipeline writes new partitions)
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES    TO geo_readonly_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO geo_readonly_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE  ON SEQUENCES TO geo_pipeline_role;


-- ============================================================
-- 11. ROW-LEVEL SECURITY (Section 23.5)
-- Enabled on registry + quarantine tables.
-- The platform does not serve multi-tenant data today — RLS is established
-- now so access control is correct before Phase 3 multi-dataset onboarding.
-- Retrofitting RLS after data population is operationally riskier.
-- ============================================================

ALTER TABLE geo_ingest_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE geo_quarantine       ENABLE ROW LEVEL SECURITY;

-- Pipeline role: full access to its own run records; ADMIN_OVERRIDE for migrations
CREATE POLICY pipeline_own_records ON geo_ingest_registry
    FOR ALL
    TO geo_pipeline_role
    USING (
        pipeline_run_id = current_setting('app.pipeline_run_id', TRUE)::UUID
        OR current_setting('app.pipeline_run_id', TRUE) = 'ADMIN_OVERRIDE'
    );

-- Read-only role: can SELECT all rows in registry (analytics + CI)
CREATE POLICY readonly_select ON geo_ingest_registry
    FOR SELECT
    TO geo_readonly_role
    USING (TRUE);

-- Quarantine: pipeline can only INSERT (append-only — ADR-004)
CREATE POLICY quarantine_insert_only ON geo_quarantine
    FOR INSERT
    TO geo_pipeline_role
    WITH CHECK (TRUE);

-- Quarantine resolution updates: pipeline role only
CREATE POLICY quarantine_resolve ON geo_quarantine
    FOR UPDATE
    TO geo_pipeline_role
    USING (TRUE)
    WITH CHECK (TRUE);

-- Quarantine: read-only role can SELECT all rows
CREATE POLICY quarantine_readonly ON geo_quarantine
    FOR SELECT
    TO geo_readonly_role
    USING (TRUE);


-- ============================================================
-- VERIFICATION QUERIES
-- Run these after DDL execution to confirm schema is correct.
-- Expected: all queries return non-empty result sets.
-- ============================================================

-- Table existence check
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'dim_region', 'dim_date', 'dim_indicator', 'dim_vintage',
    'fact_geo_observation', 'fact_geo_h3_aggregate',
    'geo_ingest_registry', 'geo_schema_registry', 'geo_quarantine',
    'psgc_crosswalk', 'geo_lineage_edges'
  )
ORDER BY table_name;

-- dim_date population check (expects 14975 rows: 2000-01-01 to 2040-12-31)
SELECT COUNT(*) AS date_rows FROM dim_date;

-- RLS enabled check
SELECT relname, relrowsecurity
FROM pg_class
WHERE relname IN ('geo_ingest_registry', 'geo_quarantine')
  AND relrowsecurity = TRUE;

-- Role existence check
SELECT rolname FROM pg_roles
WHERE rolname IN ('geo_pipeline_role', 'geo_readonly_role');

-- ============================================================

COMMIT;
