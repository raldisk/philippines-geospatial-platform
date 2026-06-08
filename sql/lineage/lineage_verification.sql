-- sql/lineage/lineage_verification.sql
-- Day 5 — Section 30 lineage schema verification
--
-- PURPOSE:
--   Verify geo_lineage_edges table exists from Day 4 DDL.
--   Confirm adjacency-list model is correct.
--   Review recursive CTE query plans (EXPLAIN only — no data population).
--
-- DO NOT POPULATE: population is Phase 3 scope (master_plan_v2.2_final.md Day 7 P2).
-- Population function: geo_service/pipeline/lineage.py::record_lineage_edge()
--
-- Run against: geo_platform database
-- Expected results annotated inline.

-- ─────────────────────────────────────────────────────────────────────────────
-- CHECK 1: Table structure matches Section 24 DDL
-- Expected: 7 columns — id, source_type, source_id, target_type, target_id,
--           pipeline_run_id, created_at
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    column_name,
    data_type,
    is_nullable,
    column_default
FROM information_schema.columns
WHERE table_name = 'geo_lineage_edges'
ORDER BY ordinal_position;

-- ─────────────────────────────────────────────────────────────────────────────
-- CHECK 2: CHECK constraints on source_type / target_type
-- Expected: both constrained to ('archive','bronze','silver','gold','pmtiles')
-- Silver included in type enum even though Silver paths are never inserted
-- (ephemeral Silver has no lineage edge per ADR-011 — only archive→bronze,
--  bronze→gold, gold→pmtiles edges are recorded in Phase 3).
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    conname                                      AS constraint_name,
    contype                                      AS constraint_type,
    pg_get_constraintdef(oid, true)              AS definition
FROM pg_constraint
WHERE conrelid = 'geo_lineage_edges'::regclass
ORDER BY contype, conname;

-- ─────────────────────────────────────────────────────────────────────────────
-- CHECK 3: Indexes present — required for recursive CTE performance
-- Expected:
--   geo_lineage_source  ON (source_id, source_type)  — downstream-impact query
--   geo_lineage_target  ON (target_id, target_type)  — traceback query
--   PRIMARY KEY         ON (id)
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'geo_lineage_edges'
ORDER BY indexname;

-- ─────────────────────────────────────────────────────────────────────────────
-- CHECK 4: UNIQUE constraint on (source_id, target_id, pipeline_run_id)
-- Idempotency guarantee: record_lineage_edge() uses ON CONFLICT DO NOTHING.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    conname,
    contype,
    pg_get_constraintdef(oid, true) AS definition
FROM pg_constraint
WHERE conrelid  = 'geo_lineage_edges'::regclass
  AND contype   = 'u';           -- 'u' = UNIQUE

-- ─────────────────────────────────────────────────────────────────────────────
-- CHECK 5: Table is empty — Phase 3 population gate
-- Expected: edge_count = 0
-- If nonzero: manual insertion occurred outside pipeline — investigate.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
    COUNT(*)                               AS edge_count,
    'EXPECTED: 0 (Phase 3 populates)'      AS note
FROM geo_lineage_edges;

-- ─────────────────────────────────────────────────────────────────────────────
-- QUERY PLAN REVIEW A: Archive → Tile traceback (Section 30.2)
-- "Which source archive produced this PMTiles file?"
--
-- EXPLAIN ONLY — no ANALYZE — table is empty, runtime would be misleading.
-- Key things to verify in plan output:
--   1. CTE Scan on geo_lineage_edges uses geo_lineage_target index (target_id lookup)
--   2. Recursive join uses geo_lineage_target index for e.target_id = lp.source_id
--   3. depth < 10 guard present in plan (prevents unbounded recursion)
--   4. Anti-join on path_ids array (cycle prevention: NOT source_id = ANY(...))
-- ─────────────────────────────────────────────────────────────────────────────
EXPLAIN (FORMAT TEXT, ANALYZE FALSE, COSTS TRUE, VERBOSE FALSE)
WITH RECURSIVE lineage_path AS (
    -- Base case: start from PMTiles node
    SELECT
        source_type,
        source_id,
        target_type,
        target_id,
        pipeline_run_id,
        1                       AS depth,
        ARRAY[target_id]        AS path_ids
    FROM geo_lineage_edges
    WHERE target_id = 'pmtiles/psa_provincial_2023.pmtiles'

    UNION ALL

    -- Recursive step: walk upstream (target → source)
    SELECT
        e.source_type,
        e.source_id,
        e.target_type,
        e.target_id,
        e.pipeline_run_id,
        lp.depth + 1,
        lp.path_ids || e.source_id
    FROM geo_lineage_edges e
    JOIN lineage_path lp
        ON e.target_id = lp.source_id
    WHERE lp.depth < 10                          -- depth guard
      AND NOT e.source_id = ANY(lp.path_ids)     -- cycle prevention
)
SELECT
    lp.source_type,
    lp.source_id,
    lp.depth                    AS hops_from_tile,
    r.extracted_at              AS ingested_at,
    r.archive_filename
FROM lineage_path lp
LEFT JOIN geo_ingest_registry r
    ON r.archive_hash = lp.source_id
WHERE lp.source_type = 'archive'
ORDER BY lp.depth DESC;

-- ─────────────────────────────────────────────────────────────────────────────
-- QUERY PLAN REVIEW B: Targeted re-processing — downstream impact (Section 30.3)
-- "Which PMTiles need regeneration after archive correction?"
--
-- Key things to verify:
--   1. Base case uses geo_lineage_source index (source_id lookup) — forward traversal
--   2. Recursive step joins on source_id = ao.target_id using geo_lineage_source index
--   3. DISTINCT in final SELECT (multiple paths may reach same pmtiles node)
-- ─────────────────────────────────────────────────────────────────────────────
EXPLAIN (FORMAT TEXT, ANALYZE FALSE, COSTS TRUE, VERBOSE FALSE)
WITH RECURSIVE affected_outputs AS (
    -- Base case: start from corrected archive (forward traversal)
    SELECT
        target_type,
        target_id,
        pipeline_run_id,
        1               AS depth
    FROM geo_lineage_edges
    WHERE source_id = 'placeholder_archive_sha256_hash'

    UNION ALL

    -- Recursive step: follow edges downstream (source → target)
    SELECT
        e.target_type,
        e.target_id,
        e.pipeline_run_id,
        ao.depth + 1
    FROM geo_lineage_edges e
    JOIN affected_outputs ao
        ON e.source_id = ao.target_id
    WHERE ao.depth < 10
)
SELECT DISTINCT
    target_id                   AS artifact_to_regenerate,
    target_type
FROM affected_outputs
WHERE target_type = 'pmtiles'
ORDER BY target_id;

-- ─────────────────────────────────────────────────────────────────────────────
-- ADJACENCY-LIST MODEL VALIDATION
-- Confirms that the three edge types Phase 3 will insert satisfy the model:
--   archive    → bronze    (SHA-256 hash → bronze S3 path)
--   bronze     → gold      (bronze path → gold Parquet path)
--   gold       → pmtiles   (gold path → pmtiles S3 key)
-- Silver is deliberately absent: ephemeral Silver has no lineage edge (ADR-011).
--
-- Dry-run INSERT with ON CONFLICT DO NOTHING — verifies idempotency contract.
-- Wrapped in a rolled-back transaction: no persistent state created.
-- ─────────────────────────────────────────────────────────────────────────────
BEGIN;

INSERT INTO geo_lineage_edges
    (source_type, source_id, target_type, target_id, pipeline_run_id)
VALUES
    -- archive → bronze
    ('archive', 'abc123def456', 'bronze',  'bronze/psa_provincial_2023/v2023/', '00000000-0000-0000-0000-000000000001'),
    -- bronze  → gold
    ('bronze',  'bronze/psa_provincial_2023/v2023/', 'gold', 'gold/psa_provincial_2023/features.parquet', '00000000-0000-0000-0000-000000000001'),
    -- gold    → pmtiles
    ('gold',    'gold/psa_provincial_2023/features.parquet', 'pmtiles', 'pmtiles/psa_provincial_2023.pmtiles', '00000000-0000-0000-0000-000000000001')
ON CONFLICT (source_id, target_id, pipeline_run_id) DO NOTHING;

-- Verify 3 rows inserted
SELECT COUNT(*) AS inserted_count, 'EXPECTED: 3' AS note
FROM geo_lineage_edges;

-- Verify idempotency: same INSERT again produces 0 new rows
INSERT INTO geo_lineage_edges
    (source_type, source_id, target_type, target_id, pipeline_run_id)
VALUES
    ('archive', 'abc123def456', 'bronze',  'bronze/psa_provincial_2023/v2023/', '00000000-0000-0000-0000-000000000001'),
    ('bronze',  'bronze/psa_provincial_2023/v2023/', 'gold', 'gold/psa_provincial_2023/features.parquet', '00000000-0000-0000-0000-000000000001'),
    ('gold',    'gold/psa_provincial_2023/features.parquet', 'pmtiles', 'pmtiles/psa_provincial_2023.pmtiles', '00000000-0000-0000-0000-000000000001')
ON CONFLICT (source_id, target_id, pipeline_run_id) DO NOTHING;

-- Still 3 rows — ON CONFLICT DO NOTHING enforced idempotency
SELECT COUNT(*) AS count_after_second_insert, 'EXPECTED: 3 (idempotent)' AS note
FROM geo_lineage_edges;

-- Rollback: table remains empty (Phase 3 gate)
ROLLBACK;

-- Final state: 0 rows (rollback succeeded)
SELECT COUNT(*) AS final_count, 'EXPECTED: 0 (rolled back)' AS note
FROM geo_lineage_edges;
