# Day 2 — PH Geospatial Intelligence Platform v2.2

**Objective:** Generate consumable tiles and verify in browser.  
**Prerequisite:** Day 1 gate PASSED — `inspector.py`, `bronze/writer.py`, `silver/simplify.py`, `day1_profile_report.json` complete.

---

## Directory Structure

```
day2/
├── geo_service/
│   ├── config.py                        # env-driven settings
│   ├── domain/
│   │   └── exceptions.py               # centralised exception hierarchy (Section 8.2)
│   ├── infra/
│   │   └── logging_config.py           # structlog init (Section 8.1)
│   └── pipeline/
│       └── gold/
│           ├── h3_aggregate.py         # H3 + Jenks (ADR-009, Res 16.5, Sec 7.3)
│           └── pmtiles.py              # tippecanoe wrapper (ADR-008)
├── tests/
│   ├── fixtures/
│   │   └── generate_fixture.py         # synthetic PH GeoDataFrame (82 provinces)
│   ├── test_h3_aggregate.py
│   └── test_pmtiles.py
├── day2_tile_preview.html               # MapLibre GL + PMTiles standalone viewer
├── run_day2.py                          # main runner (all tasks)
├── requirements_day2.txt
└── day-2-README.md                      # this file
```

---

## Prerequisites

### 1. Python dependencies

```bash
pip install -r requirements_day2.txt
```

### 2. tippecanoe binary (ADR-008 — pre-built, SHA-256 pinned)

```bash
# Download felt/tippecanoe v2.67.0 linux-x86_64 pre-built binary
TIPPECANOE_VERSION=2.67.0
curl -fsSL \
  "https://github.com/felt/tippecanoe/releases/download/${TIPPECANOE_VERSION}/tippecanoe-linux-x86_64" \
  -o /usr/local/bin/tippecanoe \
  && chmod +x /usr/local/bin/tippecanoe

# Get the real SHA-256 and update TIPPECANOE_SHA256 in geo_service/config.py
sha256sum /usr/local/bin/tippecanoe

# Verify install
tippecanoe --version   # should print: tippecanoe v2.67.0
```

> **Required:** Copy the `sha256sum` output into `geo_service/config.py` → `TIPPECANOE_SHA256` before running. The placeholder value will fail the integrity check.

### 3. pmtiles CLI (for `pmtiles verify`)

```bash
go install github.com/protomaps/go-pmtiles@latest
# Verify:
pmtiles --version
```

> If Go is not available: set `TIPPECANOE_SKIP_SHA256=1` to bypass SHA-256 check (CI only), and the runner will warn but not halt on missing pmtiles CLI.

### 4. Day 1 curated GeoParquet

Expected at: `data/curated/curated.parquet`  
Override via: `GEO_CURATED_PARQUET=path/to/curated.parquet python run_day2.py`

If Day 1 output is not available yet, use `--fixture` to run against synthetic data.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GEO_DATASET_NAME` | `psa_provincial_2023` | Dataset name / PMTiles layer name |
| `GEO_VALUE_COLUMN` | `poverty_rate` | Numeric column from Day 1 .dbf for aggregation |
| `GEO_CURATED_PARQUET` | `data/curated/curated.parquet` | Day 1 GeoParquet path |
| `GEO_OUTPUT_DIR` | `outputs/day2` | Output directory for H3 parquet + PMTiles |
| `TIPPECANOE_BIN` | `/usr/local/bin/tippecanoe` | tippecanoe binary path |
| `TIPPECANOE_SHA256` | *(placeholder)* | Real SHA-256 from `sha256sum` output |
| `TIPPECANOE_SKIP_SHA256` | `0` | Set `1` for CI smoke-test bypass only |
| `PMTILES_BIN` | `/usr/local/bin/pmtiles` | pmtiles CLI path |
| `GEO_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `GEO_LOG_JSON` | `0` | Set `1` for JSON log output (Airflow/production) |

---

## Execution — Step by Step

### Task A (0–2h): H3 Aggregation + Jenks

```bash
# Run Task A only
python run_day2.py --task h3

# With real column name (check day1_profile_report.json for exact field):
GEO_VALUE_COLUMN=poverty_incidence python run_day2.py --task h3

# With synthetic fixture (no real data):
python run_day2.py --task h3 --fixture
```

**Expected output:**
```
[Task A 0–2h] H3 aggregation + Jenks classification...
  ✓ H3 aggregation complete in 4.3s
    resolution selected : 6
    hexes output        : 347
    jenks_class range   : 1–5
    jenks_breaks sample : [2.4, 12.1, 22.7, 35.0, 55.1]
    saved → outputs/day2/psa_provincial_2023_h3_r6.parquet
```

**Gate A criteria:**
- `jenks_class` column present in output DataFrame
- `jenks_class` values in range 1–5 (0 = no data)
- `jenks_breaks` is valid JSON list
- No `H3ResolutionError` raised

**If HALT — H3ResolutionError:**
1. Check `day1_profile_report.json` → `operation_mode`. If `geometry_only`, H3 aggregation is not applicable.
2. Confirm `GEO_VALUE_COLUMN` matches an actual numeric column in the .dbf.
3. Resolution 5 covers hexagons ~2,200 km² — even Batanes should pass. If all three fail, dataset has no valid centroids.

---

### Task B (2–5h): tippecanoe PMTiles

```bash
# Run Task B only (uses curated.parquet from settings)
python run_day2.py --task pmtiles

# From existing GeoJSON (skip parquet step):
python run_day2.py --task pmtiles --geojson outputs/day2/psa_provincial_2023.geojson

# With fixture:
python run_day2.py --task pmtiles --fixture
```

**Expected output:**
```
[Task B 2–5h] tippecanoe PMTiles generation...
  ✓ PMTiles generated in 187.4s
    output   : outputs/day2/psa_provincial_2023.pmtiles
    size     : 4.31 MB  (limit: 50 MB)
    verified : True
    zoom     : z4→z12
```

**Gate B criteria:**
- `outputs/day2/<dataset>.pmtiles` exists
- File size < 50 MB
- `pmtiles verify` exits 0

**If HALT — TileGenerationError (tippecanoe non-zero exit):**
```bash
# Run tippecanoe directly to see full stderr:
tippecanoe \
  --output outputs/day2/psa_provincial_2023.pmtiles \
  --layer psa_provincial_2023 \
  --minimum-zoom 4 --maximum-zoom 12 \
  --maximum-tile-bytes 500000 \
  --drop-densest-as-needed --force \
  outputs/day2/psa_provincial_2023.geojson
```

**If HALT — FileSizeError (≥ 50 MB):**
1. Cap maximum zoom: set `TILE_MAX_ZOOM=10` in config.py (Section 29 runbook).
2. Provincial subset: reduce features to one island group.
3. Last resort: increase `TILE_MAX_BYTES=750000` (lossy).

**If HALT — PMTilesVerificationError:**
```bash
pmtiles verify outputs/day2/psa_provincial_2023.pmtiles
pmtiles show   outputs/day2/psa_provincial_2023.pmtiles
```

---

### Task C (5–8h): MapLibre GL Render (Manual Gate)

```bash
# Run all tasks (Task C auto-opens browser)
python run_day2.py

# Skip browser auto-open (open manually):
python run_day2.py --no-browser
```

**Manual steps:**
1. Open `day2_tile_preview.html` in Chrome or Firefox.
2. Click **Choose File** → select `outputs/day2/<dataset>.pmtiles`.
3. Click **Load PMTiles →**.
4. Verify checklist:

| Check | Expected |
|---|---|
| Status badge colour | Green |
| Zoom 4 (national) | Tile boundaries visible |
| Zoom 8 (provincial) | Detail visible |
| Zoom 12 (municipality) | Fine boundaries visible |
| Zoom ≥ 7 | Jenks colour classes visible |
| DevTools console | Zero errors |

**Gate C criteria:**
- Tile visible at all zoom levels z4→z12
- No console errors in DevTools
- Status badge shows green "✓ Tiles loaded"

**If HALT — blank tiles:**
1. Source-layer name mismatch — check layer name in preview HTML matches tippecanoe `--layer` value (default: dataset name).
2. Run `pmtiles verify` again — Task B gate must have passed first.
3. Open DevTools → Network → filter `pmtiles` → check 206 Partial Content responses.
4. If loading via MinIO URL: add CORS header `Access-Control-Allow-Origin: *` to bucket policy.

---

## Full Run (All Tasks)

```bash
# With real data (Day 1 curated parquet present):
python run_day2.py

# With synthetic fixture:
python run_day2.py --fixture
```

---

## Unit Tests

```bash
# All unit tests (no binary required):
pytest tests/ -v

# H3 only:
pytest tests/test_h3_aggregate.py -v

# PMTiles unit (mocked tippecanoe):
pytest tests/test_pmtiles.py -v

# PMTiles integration (requires tippecanoe binary):
pytest tests/test_pmtiles.py -v -m integration

# Coverage:
pytest tests/ --cov=geo_service --cov-report=term-missing
```

---

## Day 2 Gate Report

After `run_day2.py`, a gate report is saved to `outputs/day2/day2_gate_report.json`:

```json
{
  "task_a_h3_jenks": true,
  "task_b_pmtiles_file": true,
  "task_b_pmtiles_verify": true,
  "task_b_size_under_50mb": true,
  "task_c_tile_visible": true
}
```

**Day 2 Go/No-Go gate (from master plan):** All five gates must be `true` before proceeding to Day 3.

**If PMTiles fails on national scale:** Reduce to provincial subset per master plan fallback. Example — Luzon only:

```python
# In run_day2.py or notebook:
gdf = gpd.read_parquet("data/curated/curated.parquet")
luzon_only = gdf[gdf["region"].str.contains("Luzon|NCR|Region I|Region II|Region III|Region IV|Region V|CAR|Cordillera")]
luzon_only.to_parquet("data/curated/curated_luzon.parquet")
```

Then: `GEO_CURATED_PARQUET=data/curated/curated_luzon.parquet python run_day2.py --task pmtiles`

---

## HALT Conditions (from master_plan_v2.2_final.md)

| Condition | Action |
|---|---|
| PMTiles fails on national scale | Reduce to provincial subset. Do not proceed to Day 3 with unverified tiles. |
| `pmtiles verify` fails | Fix tippecanoe arguments or GeoJSON validity. HALT. |
| MapLibre GL shows no tile or console errors | Debug source-layer name, CORS, verify output. HALT. |
| Do not rewrite TECH-DEBT-002 | — |
| Do not implement dbt (Section 27) | Unless Day 7 contingency with >2h slack |

---

## Key Architecture References

| ADR / Section | Decision | Relevance |
|---|---|---|
| ADR-001 | PMTiles over MBTiles | HTTP range requests, CDN-cacheable, no SQLite lock |
| ADR-003 | H3 + administrative boundaries both precomputed | H3 for Deck.gl, admin for choropleth |
| ADR-008 | Pre-built tippecanoe binary, SHA-256 pinned | 8–12 min CI → ~25 s |
| ADR-009 | Dynamic H3 resolution, occupancy thresholds | ≥70% occupancy, ≥2 features/hex |
| Resolution 16.4 | tippecanoe binary pinning | felt/tippecanoe GitHub Releases |
| Resolution 16.5 | H3 resolution validation | Candidates (7,6,5) descending |
| Section 7.3 | Jenks computed once at Gold | Never recomputed at serve time |
| Section 6.2 | C4 Container — MapLibre GL frontend | Tile source configuration |

---

*Day 2 of PH Geospatial Intelligence Platform v2.2 (4–7 day compressed sprint)*  
*Author: Herald V. Collamar*  
*Proceed to Day 3 (FastAPI /geo/v1 + DuckDB spatial + ASOF JOIN + Security baseline) only after Day 2 gate PASSED.*
