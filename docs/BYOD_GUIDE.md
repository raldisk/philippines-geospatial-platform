# Bring Your Own Data (BYOD) Guide
## PH Geospatial Intelligence Platform v2.2

> **What this guide covers:** how to ingest your own Philippine geospatial
> dataset — PSA, NAMRIA, COMELEC, GADM, or any compatible source — without
> modifying platform code.

---

## What You Need

1. **Geospatial archive**: `.7z` or `.zip` containing `.shp` files (or flat GeoJSON/GeoParquet)
2. **PostgreSQL 14+** with PostGIS extension
3. **MinIO or S3** bucket for archive storage
4. **Optional**: Airflow 3.x for scheduled orchestration

---

## Supported Data Formats

| Format | Notes |
|--------|-------|
| Shapefile (`.shp`) | Standard; must include `.shx` and `.dbf`. `.prj` optional — defaults to `TARGET_CRS` env var if missing |
| GeoJSON | Direct ingestion supported |
| GeoParquet | Silver/Gold output format; can also be ingested directly |

---

## Expected Shapefile Attributes

The platform looks for these columns by default (configurable via `REGION_COLUMN_MAP`):

| Role | Default column | Example value |
|------|---------------|--------------|
| Region | `ADM1_EN` | `"Region IV-A CALABARZON"` |
| Province | `ADM2_EN` | `"Laguna"` |
| City/Municipality | `ADM3_EN` | `"Santa Rosa"` |
| Barangay | `ADM4_EN` | `"Balibago"` |

If your shapefile uses different column names (e.g. GADM `NAME_1`, `NAME_2`), override with:

```bash
export REGION_COLUMN_MAP='{"region":"NAME_1","province":"NAME_2","city":"NAME_3","barangay":"NAME_4"}'
```

---

## CRS / Projection Handling

The platform defaults to **EPSG:4326** (WGS84). If your data uses a different CRS:

```bash
# Philippine engineering surveys (UTM Zone 51N)
export TARGET_CRS=EPSG:32651

# Or any EPSG code
export TARGET_CRS=EPSG:3123
```

If your shapefile has no `.prj` file, the pipeline will log a warning and assume `TARGET_CRS`. No crash.

---

## Multi-Shapefile Archives

Real PSA `.7z` archives often bundle multiple shapefiles (provinces + municipalities + barangays).
The extractor finds all `.shp` files recursively — nested folders, metadata PDFs, and README files
are automatically skipped.

To ingest only specific layers from a multi-shapefile archive:

```bash
export LAYER_FILTER=Provinces,Municipalities
```

Leave empty to ingest all shapefiles found.

---

## Adding Custom Indicators

If your shapefile has numeric columns beyond the defaults (e.g. `poverty_2024`, `pop_density`):

1. The pipeline auto-detects them and creates entries in `dim_indicator`.
2. Unknown indicators are quarantined with a log entry — the pipeline continues.
3. To pre-register indicators before ingestion:

```sql
INSERT INTO dim_indicator (indicator_nk, indicator_name, unit, source, domain)
VALUES ('poverty_2024', 'Poverty Incidence 2024', 'percent', 'PSA FIES 2024', 'economic')
ON CONFLICT (indicator_nk) DO NOTHING;
```

See `docs/RUNBOOK.md §Indicator Registration` for the full registration workflow.

---

## Memory Safety for Large Archives (>500MB)

```bash
# Single worker — lowest memory footprint
export SILVER_MAX_WORKERS=1
export SILVER_MEMORY_LIMIT_MB=4096
```

Default is 4 workers. On a machine with <8GB RAM and a large archive, set `SILVER_MAX_WORKERS=1`
to prevent OOM in `ProcessPoolExecutor`.

---

## First Run Checklist

```bash
# 1. Copy and fill env config
cp .env.example .env
# Edit .env with your MinIO endpoint, credentials, and CRS

# 2. Initialize schema (idempotent — safe to re-run)
psql $POSTGRES_DSN -f geo_service/infra/postgres_schema.sql

# 3. Upload your .7z to MinIO
mc cp your_data.7z myminio/geo-uploads/

# 4. Trigger pipeline (Airflow)
airflow dags trigger geo_pipeline_daily

# 5. Verify health
curl http://localhost:8002/health/ready

# 6. Query your data
curl "http://localhost:8002/geo/v1/h3/7?indicator=poverty_rate"
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ImportError: cannot import name 'find_new_archives'` | Stale install | Pull latest; `geo_service/pipeline/extract/archive.py` must be present |
| `TypeError: DAG.__init__() got unexpected keyword argument 'schedule_interval'` | Airflow 2 vs 3 | Upgrade to Airflow 3.x; DAG uses `schedule=` parameter |
| `No CRS detected` | Missing `.prj` file | Set `TARGET_CRS=EPSG:32651` (or your CRS) in `.env` |
| `Unknown indicator column: X` | Column not in `dim_indicator` | Pre-register via SQL, or ignore if not needed |
| `ProcessPoolExecutor OOM` | Archive too large | Set `SILVER_MAX_WORKERS=1` and `SILVER_MEMORY_LIMIT_MB=4096` |
| `Layer 'X' not found` | Layer not in Gold parquet | Re-run pipeline or check `LAYER_FILTER` setting |
| `Resolution N not in accepted set` | `VALID_H3_RESOLUTIONS` restricts range | Set `VALID_H3_RESOLUTIONS=` (empty) to allow all 0–15 |

---

## Environment Variable Reference

All variables documented in `.env.example`. Key BYOD variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `TARGET_CRS` | `EPSG:4326` | Fallback CRS when shapefile has no `.prj` |
| `REGION_COLUMN_MAP` | PSA ADM names | Column name mapping for non-PSA shapefiles |
| `LAYER_FILTER` | *(empty — all)* | Shapefile stems to ingest from multi-shp archives |
| `SILVER_MAX_WORKERS` | `4` | ProcessPoolExecutor workers — reduce for low-RAM hosts |
| `SILVER_MEMORY_LIMIT_MB` | `4096` | Memory budget hint for Silver processing |
| `VALID_H3_RESOLUTIONS` | *(empty — 0–15)* | Restrict H3 API to specific resolutions |
| `TARGET_BBOX_LON_MIN/MAX` | `116.0` / `127.0` | Override bounding box for sub-national datasets |
| `TARGET_BBOX_LAT_MIN/MAX` | `4.5` / `22.0` | Override bounding box for sub-national datasets |
| `MINIO_ARCHIVE_PREFIX` | `uploads/` | MinIO prefix scanned for new archives |
| `GEO_BRONZE_BUCKET` | `geo-bronze` | Bronze layer storage bucket |
| `GEO_SILVER_TEMP_BUCKET` | `geo-silver-tmp` | Ephemeral Silver temp bucket |
| `GEO_GOLD_BUCKET` | `geo-gold` | Gold layer storage bucket |

---

*Last updated: BYOD hardening pass — v2.2-byod. See `remediation_manifest.json` for fix history.*
