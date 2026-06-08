# Day 1 — Discovery & Bronze/Curated
## PH Geospatial Intelligence Platform v2.2

**Sprint day:** 1 of 4–7  
**Time budget:** 8h total (0–3h inspection, 3–8h pipeline)  
**Gate authority:** Master plan v2.2 (`master_plan_v2_2_final.md`)  
**ADRs in scope:** ADR-006, ADR-011, ADR-012

---

## Objective

Confirm shapefile data exists and is analytical. Build first pipeline segment:
`.7z archive → .dbf inspection → Bronze GeoParquet → Ephemeral Silver → Curated GeoParquet`

---

## Prerequisites

```bash
pip install -r requirements_day1.txt
```

Confirmed available before starting:
- [ ] `.7z` archives in `data/raw/` (PSA, NAMRIA, or COMELEC shapefiles)
- [ ] Python 3.11+
- [ ] `fiona`, `geopandas`, `shapely>=2.0`, `pyarrow>=14`, `py7zr`, `structlog`, `matplotlib`

---

## Phase 1 (0–3h) — Mandatory .dbf Inspection

### What it does

`inspector.py` opens every `.shp` via fiona (schema only, no geometry load), classifies each dataset into one of three operation modes, and writes `day1_profile_report.json`.

**Operation modes (ADR-006, Resolution 16.1):**

| Mode | Condition | Downstream action |
|------|-----------|-------------------|
| `analytical` | numeric columns found | H3 + Jenks + Kimball fact (Day 2+) |
| `boundary_catalog` | PSGC code columns only | dim_region SCD Type 2 load |
| `geometry_only` | no numeric, no PSGC | boundary catalog, no aggregation |

### Run inspection

```bash
# Manual: inspect a single shapefile
python - <<'EOF'
from geo_service.pipeline.extract.inspector import profile_shapefile
p = profile_shapefile("data/staging/dataset/file.shp", "psa_provincial_2023", "data/raw/archive.7z")
print(p.operation_mode, p.recommended_indicator, p.feature_count)
EOF

# Full run against all archives (recommended)
python run_day1.py --archives "data/raw/*.7z" --report-json day1_profile_report.json
# Stops here if halt_condition=true — does NOT proceed to Bronze
```

### Validate gate

```bash
python scripts/validate_analytical.py day1_profile_report.json
# Exit 0 = GO. Exit 1 = HALT.

# pytest variant (CI-compatible)
python -m pytest scripts/validate_analytical.py::test_at_least_one_analytical_dataset -v
```

### Confirmation gate 1 — Inspection (0–3h)

Confirm before proceeding to Phase 2:

- [ ] `day1_profile_report.json` written
- [ ] At least one dataset shows `operation_mode: "analytical"`
- [ ] `recommended_indicator` is non-null for all analytical datasets
- [ ] `validate_analytical.py` exits 0
- [ ] `halt_condition: false` in report

**HALT condition:** If zero analytical datasets after inspecting all archives, do NOT proceed to Bronze. Pivot to geometry-only + external CSV join. Document the pivot in the report and stop Day 1 here.

---

## Phase 2 (3–8h) — Bronze → Ephemeral Silver → Curated

### What it does

For each analytical dataset (highest feature count first):

1. **Bronze** (`bronze/writer.py`) — load `.shp` via geopandas, annotate with SHA-256 hash + lineage metadata, write GeoParquet to `data/bronze/`. SQLite `geo_ingest_registry.db` prevents reprocessing unchanged archives. `write_covering_bbox=True` (ADR-012).

2. **Silver** (`silver/simplify.py`) — CRS normalize → EPSG:4326, `shapely.make_valid()` on invalid geometries (ADR-010 non-regulatory path), simplify at z4/z8/z12 (stored as WKB columns). Write ephemeral temp to `data/silver-tmp/`.

3. **Curated** — write final GeoParquet to `data/curated/` with `write_covering_bbox=True`. Verify. Delete Silver temp (ADR-011).

### Run pipeline (after gate passes)

```bash
# Full Day 1 end-to-end
python run_day1.py \
  --archives "data/raw/*.7z" \
  --staging-dir data/staging \
  --bronze-dir data/bronze \
  --curated-dir data/curated \
  --silver-tmp-dir data/silver-tmp \
  --registry-db geo_ingest_registry.db \
  --report-json day1_profile_report.json \
  --vintage-year 2023    # optional: set to PSA vintage year

# Idempotent: re-running with same archives returns ALREADY_INGESTED, skips pipeline
```

### Run components independently

```python
# Bronze only
from geo_service.pipeline.bronze.writer import write_bronze
import geopandas as gpd
gdf = gpd.read_file("data/staging/psa_provincial/psa_provincial.shp")
result = write_bronze(gdf, archive_path="data/raw/psa_provincial_2023.7z",
                      dataset_name="psa_provincial_2023")
print(result.status, result.output_path, result.feature_count)

# Silver → Curated only (from Bronze Parquet)
from geo_service.pipeline.silver.simplify import run_silver
sr = run_silver(source="data/bronze/psa_provincial_2023_abc123456.parquet",
                dataset_name="psa_provincial_2023")
print(sr.curated_path, sr.quarantined_count, sr.silver_tmp_deleted)
```

---

## Verification — Day 1 Success Criteria

All three must pass before declaring Day 1 complete:

```python
# Quick verification script
import geopandas as gpd
import pyarrow.parquet as pq

curated = "data/curated/psa_provincial_2023.parquet"

# 1. gdf.plot() renders
import matplotlib; matplotlib.use("Agg")
gdf = gpd.read_parquet(curated)
ax = gdf.plot(); assert ax is not None
print(f"[PASS] gdf.plot() OK — {len(gdf)} features")

# 2. Feature count matches source
# (Silver verification already asserts this; re-check here if needed)
print(f"[INFO] Feature count: {len(gdf)}")

# 3. GeoParquet 'geo' metadata in footer (write_covering_bbox proof)
raw = pq.read_table(curated)
assert b"geo" in (raw.schema.metadata or {})
print("[PASS] GeoParquet 'geo' metadata present in Parquet footer")
```

---

## Confirmation Gate 2 — Pipeline (3–8h)

Confirm before closing Day 1:

- [ ] `data/curated/<dataset>.parquet` exists for at least one analytical dataset
- [ ] `gdf.plot()` renders without error
- [ ] Feature count matches source shapefile (minus any quarantined geometries)
- [ ] `b"geo"` in Parquet footer metadata (ADR-012 Hilbert bbox)
- [ ] `data/silver-tmp/` is empty (ADR-011 ephemeral Silver deleted)
- [ ] `geo_ingest_registry.db` contains `status='WRITTEN'` row for each processed archive
- [ ] Quarantine rate < 5% (warning if exceeded; review shapefiles)

---

## Output Artifacts

| File | Purpose |
|------|---------|
| `day1_profile_report.json` | Per-dataset inspection output. Go/No-Go gate input. |
| `geo_ingest_registry.db` | SQLite SHA-256 dedup registry (Day 3: replace with PostgreSQL) |
| `data/bronze/<dataset>_<hash[:12]>.parquet` | Immutable raw GeoParquet. Never modified after write. |
| `data/curated/<dataset>.parquet` | Verified Curated GeoParquet with Hilbert bbox index. Day 2 input. |
| `data/silver-tmp/` | Should be empty after successful run (ADR-011). |

---

## Data Directory Layout

```
data/
├── raw/                    ← place .7z archives here before running
│   └── *.7z
├── staging/                ← auto-created: extracted .shp/.dbf/.prj files
│   └── <archive_stem>/
│       ├── *.shp
│       ├── *.dbf
│       └── *.prj
├── bronze/                 ← immutable Bronze GeoParquet
│   └── <dataset>_<hash>.parquet
├── silver-tmp/             ← ephemeral; must be empty after run
├── curated/                ← final Curated GeoParquet (Day 2 input)
│   └── <dataset>.parquet
geo_ingest_registry.db      ← SQLite dedup registry
day1_profile_report.json    ← inspection report
```

---

## Day 1 → Day 2 Handoff

Pass the following to Day 2 session:

```
CONTINUATION CHECKPOINT — PH Geospatial Platform Sprint v2.2

Attached: master_plan_v2_2_final.md
Status: Day 1 completed
Last artifact: data/curated/<highest_priority_dataset>.parquet
Recommended indicator: <value from day1_profile_report.json>
Feature count: <N>
Blocker: None
Resume from: Day 2 — Product Layer (H3 + Jenks) → PMTiles → MapLibre GL

Do not rewrite TECH-DEBT-002 in this sprint.
Do not implement dbt (Section 27) unless Day 7 contingency with >2h slack.
```

---

## Constraints Active This Day

| Constraint | Source |
|------------|--------|
| Silver ephemeral — delete after Gold verification | ADR-011 |
| `write_covering_bbox=True` on all GeoParquet | ADR-012 |
| Single-threaded Silver (no ProcessPoolExecutor) | ADR-007 deferred to Day 4 |
| SQLite registry (not PostgreSQL) | Phase 1 "Make it Work" |
| Local staging (not MinIO S3A) | Phase 1 "Make it Work" |
| Do not rewrite TECH-DEBT-002 | Master plan hard constraint |
| Do not implement dbt | Unless Day 7 + >2h slack |

---

## Known Limitations (Day 1 Scope)

These are *intentional* Day 1 constraints, not bugs:

- No async pipeline — single-threaded geopandas throughout
- No MinIO — files written to local `data/` directories
- No PostgreSQL — SQLite registry only
- No Airflow — `run_day1.py` is a manual CLI script
- No ProcessPoolExecutor GEOS isolation — Day 4 refactor (ADR-007)
- No PSGC crosswalk join — Day 3 (Section 24 DDL prerequisite)
- No Jenks breaks / H3 aggregation — Day 2 Gold layer

*All limitations are tracked in the master plan and will be resolved in their designated days.*
