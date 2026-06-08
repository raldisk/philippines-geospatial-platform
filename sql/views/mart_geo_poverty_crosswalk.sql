-- =============================================================
-- sql/views/mart_geo_poverty_crosswalk.sql
-- DuckDB analytical view: spatial boundaries × poverty × GDP
-- ADR-012: all parquet sources written with write_covering_bbox=True
--          → ST_Intersects bbox filter triggers row-group pruning
-- =============================================================

-- Run order: after Gold Parquet lands in /data/gold/
-- Idempotent: CREATE OR REPLACE VIEW

LOAD spatial;

-- ---------------------------------------------------------------
-- View 1: mart_geo_poverty_crosswalk
-- Core cross-domain view for ASOF JOIN proof-of-concept.
-- Grain: one row per (region, poverty_year).
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW mart_geo_poverty_crosswalk AS
SELECT
    b.psgc_code,
    b.region_name,
    b.admin_level,
    b.vintage_year                          AS boundary_year,
    p.poverty_rate,
    p.poverty_threshold,
    p.subsistence_incidence,
    p.data_year                             AS poverty_year,
    g.gdp_growth,
    g.gdp_per_capita,
    g.data_year                             AS gdp_year,
    -- Derived: poverty-GDP tension signal
    ROUND(p.poverty_rate * -1.0 * g.gdp_growth, 4) AS poverty_gdp_tension,
    -- Geometry for spatial queries (ST_AsGeoJSON on demand)
    b.geometry
FROM
    read_parquet('/data/gold/curated/psa_provincial.parquet') b
LEFT JOIN
    read_parquet('/data/gold/indicators/poverty_indicators.parquet') p
    ON b.psgc_code = p.psgc_code
LEFT JOIN
    read_parquet('/data/gold/indicators/gdp_indicators.parquet') g
    ON b.psgc_code = g.psgc_code
       AND g.data_year = (
           -- Latest GDP year at or before poverty year (ASOF semantics)
           SELECT MAX(g2.data_year)
           FROM read_parquet('/data/gold/indicators/gdp_indicators.parquet') g2
           WHERE g2.psgc_code = b.psgc_code
             AND g2.data_year <= p.data_year
       );


-- ---------------------------------------------------------------
-- View 2: mart_geo_bbox_filtered (example for ADR-012 EXPLAIN test)
-- Demonstrates row-group pruning on Visayas subset.
-- Run EXPLAIN on this to confirm "Parquet Filter" in query plan.
-- ---------------------------------------------------------------
CREATE OR REPLACE VIEW mart_geo_bbox_filtered_visayas AS
SELECT
    region_name,
    admin_level,
    psgc_code,
    poverty_rate,
    ST_AsGeoJSON(geometry) AS geom_json
FROM
    read_parquet('/data/gold/curated/psa_provincial.parquet')
WHERE
    -- Visayas bounding box — triggers Hilbert row-group pruning
    ST_Intersects(
        geometry,
        ST_MakeEnvelope(121.0, 9.5, 126.5, 13.5)
    );


-- ---------------------------------------------------------------
-- ADR-012 Validation: EXPLAIN query for bbox pruning confirmation
-- Expected: "Parquet Filter" or "ParquetScan" with filter pushdown
-- in the EXPLAIN output.
-- ---------------------------------------------------------------
EXPLAIN
SELECT region_name, psgc_code, poverty_rate, ST_AsGeoJSON(geometry)
FROM read_parquet('/data/gold/curated/psa_provincial.parquet')
WHERE ST_Intersects(
    geometry,
    ST_MakeEnvelope(121.0, 9.5, 126.5, 13.5)
);
