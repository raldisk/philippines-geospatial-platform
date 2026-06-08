# RUNBOOK.md — PH Geospatial Intelligence Platform v2.2

**Last updated:** 2026-06-06  
**Scope:** 5 failure scenarios + Day 7 addendum. Single-developer deployment.  
**Entry point:** A new engineer can restart from `README.md` → `docker-compose up` → this runbook.  
**Authoritative source:** `master_plan_v2.2_final.md`

---

## Scenario 1 — Quarantine Rate > 5%

**Symptom:** Airflow DAG completes with WARNING. `geo_pipeline_quarantine_rate` > 0.05.

**Diagnose:**
```bash
psql $POSTGRES_DSN -c "
SELECT failure_reason, COUNT(*) FROM geo_quarantine
WHERE pipeline_run_id = '<run_id_from_airflow>' AND resolved = FALSE
GROUP BY 1 ORDER BY 2 DESC;"
```

**Fix by failure_reason:**

| reason | fix |
|--------|-----|
| `PSGC_MISMATCH` | Update `psgc_crosswalk` with new vintage mappings. Re-ingest archive. |
| `CRS_MISSING` | Obtain correct `.prj` file from PSA/NAMRIA directly. Cannot auto-repair. |
| `GEOMETRY_REPAIR_FAILED` | Inspect `geo_quarantine.sample_wkt`. If cluster-wide: exclude batch, report to PSA. |
| `QUALITY_CONTRACT_VIOLATION` | Check `config/quality_contracts/` — threshold too strict, or data degraded. |

---

## Scenario 2 — Martin OOM / Blank Tiles

**Symptom:** `curl http://localhost:3002/health` → connection refused. MapLibre shows blank tiles.

**Fix:**
```bash
docker-compose -f docker-compose.prod.yml restart martin
curl -f http://localhost:3002/health && echo "Martin recovered"
```

**If OOM persists:** PMTiles > 100 MB. Regenerate with zoom cap and increase Martin memory to 2G.

---

## Scenario 3 — DuckDB Lock Contention

**Symptom:** FastAPI `/geo/v1/h3/*` returns 503. Logs: `duckdb.IOException: could not obtain file lock`.

**Fix:** `docker-compose -f docker-compose.prod.yml restart geo-service`  
Container restart releases OS file handle. No data loss.

---

## Scenario 4 — Data Correction Re-Ingestion

Mark original hash SUPERSEDED in `geo_ingest_registry`. Delete Gold + PMTiles from MinIO. Upload corrected archive → MinioSensor triggers within 15 min. See Day 6 README for full SQL sequence.

---

## Scenario 5 — SCD Type 2 Manual Boundary Update

```bash
python scripts/scd_type2_manual_update.py \
  --region-nk "<PSGC>" --new-region-code "<new>" \
  --new-region-name "<name>" --effective-date "2024-01-01" \
  --archive-hash "<sha256>"
```

---

## Full Stack Restart

```bash
docker-compose -f docker-compose.prod.yml down
docker-compose -f docker-compose.prod.yml up -d
curl -f http://localhost:8002/health/ready && echo "FastAPI OK"
```

---

## Scenario 6 — Indicator Registration

Use this workflow when you need to pre-register a new indicator before ingestion,
or when `dim_indicator` is empty on a fresh schema install.

**When to use:**  
The pipeline auto-discovers numeric columns and inserts into `dim_indicator` on
first ingestion. Pre-registration is only required when you want to set custom
metadata (unit, description, domain) before running the pipeline.

**Pre-register an indicator:**
```sql
INSERT INTO dim_indicator (indicator_nk, indicator_name, unit, source, domain, description)
VALUES (
    'poverty_2024',            -- natural key: matches shapefile column name
    'Poverty Incidence 2024',  -- display name
    'percent',                 -- unit of measurement
    'PSA FIES 2024',           -- data source
    'economic',                -- domain: demographic|economic|geographic|infrastructure
    'Official PSA poverty incidence estimate for 2024 reference year'
)
ON CONFLICT (indicator_nk) DO UPDATE SET
    indicator_name = EXCLUDED.indicator_name,
    unit           = EXCLUDED.unit,
    source         = EXCLUDED.source,
    description    = EXCLUDED.description;
```

**List all registered indicators:**
```sql
SELECT indicator_nk, indicator_name, unit, domain, dw_created_ts
FROM dim_indicator
ORDER BY domain, indicator_nk;
```

**Mark indicator as regulatory (routes geometry repair to PostGIS ST_MakeValid):**
```sql
UPDATE dim_indicator SET is_regulatory = TRUE
WHERE indicator_nk = 'flood_hazard_2024';
```

**Remove an indicator (CAUTION: cascades to fact tables if rows exist):**
```sql
-- Check for existing observations first:
SELECT COUNT(*) FROM fact_geo_observation o
JOIN dim_indicator i ON i.indicator_sk = o.indicator_sk
WHERE i.indicator_nk = 'indicator_to_remove';

-- Only delete if count = 0:
DELETE FROM dim_indicator WHERE indicator_nk = 'indicator_to_remove';
```

**Quarantined indicators** (unknown columns auto-quarantined by pipeline):
```sql
SELECT dataset_name, failure_reason, feature_count, quarantined_at
FROM geo_quarantine
WHERE failure_reason LIKE '%indicator%' AND resolved = FALSE
ORDER BY quarantined_at DESC;
```

---

## Day 7 Addendum — P0 Fixes Applied

**Reference:** `master_plan_v2.2_final.md` Day 7 contingency block. All fixes generated 2026-06-06.

| Fix | Status | Description |
|-----|--------|-------------|
| FIX-01 | ✅ DONE | `fitness_functions/test_string_contracts.py` — 5 string-based contracts |
| FIX-02 | ✅ DONE | `tests/integration/test_bronze_minio_roundtrip.py` + `test_tippecanoe_tile_count.py` |
| FIX-03 | ✅ DONE | `geo_service/pipeline/deploy_gate.py` — 46+ checks across 5 categories |
| FIX-04 | ✅ DONE | `day4_choropleth_4k.png` — 3840×2160, synthetic data, 137KB |
| FIX-05 | ✅ DONE | `geo_service/` namespace assembled from Day 1–4 artifacts |
| FIX-06 | ✅ DONE | CI yml verified — references fitness_functions/, integration, ruff, mypy, Docker, Trivy |
| FIX-07 | ✅ DONE | DAG import contract verified — deploy_gate.py now importable |
| FIX-08 | ✅ DONE | `day1_profile_report.json` — synthetic fallback, schema-valid |
| FIX-09 | ✅ DONE | `geo_service/config.py` — TIPPECANOE_SHA256 env-injected (not placeholder) |
| FIX-10 | ✅ DONE | docker-compose.prod.yml verified — healthchecks, non-root, secrets-mounted |
| FIX-11 | ✅ DONE | `day6_post_bundle/` — reddit_posts.md + PNG + deckgl_animation_config.js |
| FIX-12 | ✅ DONE | This RUNBOOK.md updated with Day 7 section |

### Known Limitations

**Martin health check:** Martin requires at least one `.pmtiles` or `.mbtiles` file mounted at `/tiles`. Until Day 4 PMTiles are generated from real data, `curl http://localhost:3002/health` may return non-200. Documented in Day 3 README as expected behaviour. Workaround: set `MARTIN_BASE_URL=http://localhost:9999` to isolate DuckDB path only.

**Synthetic data fallback:** `day4_choropleth_4k.png` and `day1_profile_report.json` use synthetic Philippine regional data. Replace with real PSA/NAMRIA archive run before production deployment or Reddit submission. Synthetic PNG uses PlateCarree projection (not Robinson — `cartopy` not installed in render environment). Install `cartopy` and re-run `day4_choropleth_4k_render.py --verify` for r/MapPorn quality output.

**TIPPECANOE_SHA256 injection:** Config reads from `TIPPECANOE_SHA256` env var (defaults to `"UNSET"`). Must be populated via GitHub Actions secret `TIPPECANOE_SHA256` before CI SHA-256 verification passes. Obtain with: `curl -fsSL <url> | sha256sum`. See Day 5 README §Pre-Flight Checklist.
