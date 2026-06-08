# philippines-geospatial-platform

> Production-grade geospatial data engineering platform for Philippine administrative
> boundaries and statistical indicators — Bronze→Silver→Gold medallion pipeline,
> PMTiles vector tiles, H3 aggregation, and FastAPI serving.

---

## What This Is

A single-machine, single-developer geospatial platform that ingests Philippine government
shapefile archives (PSA, NAMRIA, COMELEC) through a medallion lakehouse, pre-computes
H3 hexagonal aggregations with Jenks natural breaks, generates PMTiles vector tiles, and
serves them via FastAPI at sub-50ms p95. The platform integrates as an optional
enrichment edge into the existing EconIntel 5-service Philippine economic intelligence
platform.

**Core architectural bet:** Pre-compute everything possible at pipeline time (Jenks breaks,
H3 aggregation, vector tiles) so the serving layer is read-only, stateless, and trivially
scalable. Pay latency cost once at Gold generation; amortize across all consumer requests.

**Version:** v2.2  
**Status:** BYOD Production Ready  
**License:** MIT

---

## Table of Contents

- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Repository Layout](#repository-layout)
- [Service Endpoints](#service-endpoints)
- [Running the System](#running-the-system)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Data Contracts / Schema](#data-contracts--schema)
- [Development](#development)
- [Failure Modes](#failure-modes)
- [Tech Stack](#tech-stack)
- [License](#license)

---

## Architecture

### Platform Data Flow

![Platform Data Flow](docs/architecture-data-flow.svg)

**Layers (left to right):**

| Layer | Responsibility | Key Components |
|-------|---------------|----------------|
| **Data Sources** | PSA, NAMRIA, COMELEC shapefile archives | Manual download → MinIO `geo-uploads` bucket |
| **Ingestion** | SHA-256 dedup, sensor trigger, fallback adapter | Airflow MinioSensor, ArcGIS REST fallback |
| **Pipeline** | Medallion transform with ephemeral Silver | Inspect → Bronze → Silver (ephemeral) → Gold → Deploy Gate |
| **Storage** | Immutable Bronze, curated Gold, audit trail | MinIO S3A, PostgreSQL 16+PostGIS, DuckDB Spatial |
| **Serving** | Read-only, stateless, rate-limited | FastAPI :8002, Martin :3002, TTL cache |
| **Consumers** | Tiles, H3 hexagons, 4K PNG artifacts | MapLibre GL, Deck.gl, EconIntel Dashboard |

### Pipeline Task Graph

![Pipeline DAG](docs/architecture-pipeline-dag.svg)

**DAG:** `geo_pipeline_daily`  
**Schedule:** `*/15 * * * *` (15-minute polling)  
**max_active_runs:** `1` (serialized — never two runs simultaneously)

| Task | Purpose | Output |
|------|---------|--------|
| `S3KeySensor` | Detect new `.7z` archives in `geo-uploads` | Trigger |
| `inspect_archive` | `profile_shapefile()` — determine operation mode | XCom: `dataset_profile`, `pipeline_run_id` |
| `branch_operation_mode` | `BranchPythonOperator` — route by mode | analytical / geometry_only / boundary_catalog / no_new |
| `bronze_write_analytical` | Immutable GeoParquet → MinIO | Hilbert bbox index (ADR-012) |
| `bronze_write_catalog` | SCD Type 2 `dim_region` load | Boundary catalog only |
| `silver_transform` | CRS → `make_valid` → simplify z4/z8/z12 | **Ephemeral** — deleted after Gold verification (ADR-011) |
| `gold_generate` | H3 + Jenks + Kimball ETL | `fact_geo_observation`, `fact_geo_h3_aggregate` |
| `pmtiles_generate` | `tippecanoe` v2.67.0 SHA-256 pinned binary | PMTiles archive |
| `deploy_gate` | 46-check validation | Silver cleanup, lineage edges, metrics push |

### Serving Request Flow

![Serving Flow](docs/architecture-serving-flow.svg)

**Request path:** HTTP → Rate Limit (`slowapi`) → Validation (`BboxParam`, `validate_indicator`) → TTL Cache → DuckDB (cache miss) → PostgreSQL (metadata) → JSON Response

**Guarantees:**
- Read-only: per-thread DuckDB read connections, no write path exposed
- Cache key includes all query params — no cache poisoning
- Philippine bounding box validation rejects out-of-envelope requests → HTTP 422
- Indicator whitelist `^[a-z_]{1,50}$` prevents SQL injection

---

## Quick Start

**Prerequisites:** Docker, Docker Compose, `mc` (MinIO client)

```bash
# 1. Clone and configure
git clone https://github.com/raldisk/philippines-geospatial-platform
cd philippines-geospatial-platform
cp .env.example .env          # edit MinIO, PostgreSQL, Airflow credentials

# 2. Create Docker secrets
mkdir -p secrets
echo "postgresql://geo_pipeline_role:password@postgres:5432/geo_platform"   > secrets/postgres_dsn.txt
echo "minioadmin" > secrets/minio_access_key.txt
echo "minioadmin" > secrets/minio_secret_key.txt

# 3. Start full stack
docker-compose -f docker-compose.prod.yml up -d

# 4. Verify health (all four deps probed)
curl -f http://localhost:8002/health/ready

# 5. Upload your archive — pipeline triggers automatically
mc cp your_dataset.7z minio/geo-uploads/
```

> **BYOD:** Any PSA/NAMRIA-compatible `.7z` geospatial archive — or any standard
> shapefile/GeoPackage dataset — can run the full pipeline without modifying Python code.
> See [`docs/BYOD_GUIDE.md`](docs/BYOD_GUIDE.md) for format support, column mapping,
> indicator registration, and troubleshooting.

---

## Repository Layout

```
philippines-geospatial-platform/
├── geo_service/
│   ├── api/
│   │   ├── routes/
│   │   │   ├── tiles.py          # GET /geo/v1/tiles/:z/:x/:y.mvt → proxy Martin
│   │   │   ├── geojson.py        # GET /geo/v1/geojson/:layer     → DuckDB ST_AsGeoJSON
│   │   │   ├── h3.py             # GET /geo/v1/h3/:resolution     → H3 aggregates
│   │   │   └── metadata.py       # GET /geo/v1/metadata/:dataset  → Jenks breaks
│   │   ├── middleware/
│   │   │   ├── validation.py     # BboxParam, validate_indicator, PH_BBOX guard
│   │   │   └── rate_limit.py     # slowapi tiers: tiles 500/min, H3 100/min, GeoJSON 50/min
│   │   └── health.py             # /health/live, /health/ready
│   ├── pipeline/
│   │   ├── extract/
│   │   │   ├── archive.py        # py7zr + SHA-256 dedup vs geo_ingest_registry
│   │   │   ├── shapefile.py      # geopandas.read_file + fiona schema
│   │   │   ├── inspector.py      # profile_shapefile() — determines operation mode
│   │   │   └── arcgis_adapter.py # ArcGIS REST fallback — empty-schema Parquet on BaseException
│   │   ├── bronze/
│   │   │   └── writer.py         # GeoParquet → MinIO S3A, write_covering_bbox=True (ADR-012)
│   │   ├── silver/
│   │   │   ├── crs.py            # pyproj normalize → EPSG:4326
│   │   │   ├── simplify.py       # shapely.make_valid + simplify z4/z8/z12
│   │   │   ├── crosswalk.py      # PSGC join via psgc_crosswalk table
│   │   │   ├── quality.py        # YAML contract enforcement
│   │   │   └── parallel.py       # ProcessPoolExecutor max_workers=4 (ADR-007)
│   │   └── gold/
│   │       ├── h3_aggregate.py   # h3.geo_to_h3 + mapclassify.NaturalBreaks
│   │       ├── h3_resolution_selector.py  # Dynamic resolution: 5/6/7 via occupancy ≥70% (ADR-009)
│   │       ├── pmtiles.py        # tippecanoe subprocess wrapper, SHA-256 pinned binary (ADR-008)
│   │       ├── duckdb_views.py   # LOAD spatial + view refresh
│   │       └── kimball_loader.py # fact_geo_observation + fact_geo_h3_aggregate ETL
│   ├── adapters/
│   │   └── econintel.py          # Optional HTTP enrichment edge — env-var gated, NULL on failure
│   ├── domain/
│   │   ├── models.py             # Pydantic request/response schemas
│   │   └── contracts.py          # YAML quality contract dataclasses
│   └── infra/
│       ├── minio.py              # boto3 S3A client
│       ├── postgres.py           # asyncpg connection pool
│       ├── duckdb_conn.py        # Per-thread read conn + threading.Lock write conn
│       ├── cache.py              # TTLCache ttl_seconds=3600
│       └── secrets.py            # Docker Secrets file reader — never env vars in prod
├── dags/
│   └── geo_pipeline_daily.py     # Airflow DAG: sensor → inspect → branch → bronze → silver → gold → tiles → deploy_gate
├── sql/
│   └── init/
│       └── 001_geo_platform_schema.sql   # Complete DDL: dim_region SCD Type 2, fact tables, registry, RLS, lineage
├── fitness_functions/
│   └── test_arch.py              # CI fitness functions: no Bronze/Silver access from serving, no ThreadPoolExecutor in Silver
├── tests/
│   ├── unit/
│   ├── integration/
│   └── load/
│       └── locustfile.py         # p95 tile latency SLO check
├── docs/
│   ├── BYOD_GUIDE.md
│   ├── RUNBOOK.md                # Operational runbook: 5 most likely failure scenarios
│   ├── ARCHITECTURE.md           # ADR index, coupling analysis, ranked characteristics
│   └── SOURCE-OF-TRUTH-MATRIX.md # Document authority hierarchy
├── docker-compose.prod.yml       # Production stack: MinIO, PostgreSQL, geo-service, Martin, Airflow, Pushgateway
├── Dockerfile                    # Multi-stage: tippecanoe binary → builder → production (non-root user)
├── .env.example
├── .gitignore
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

**Runtime boundaries:**
- Airflow scheduler and FastAPI serving layer run as separate processes
- DuckDB enforces writer exclusivity: `write_conn()` acquires the exclusive write lock during scheduled pipeline windows; `read_conn()` is used by FastAPI and supports unlimited concurrent readers
- These two code paths never hold the write lock simultaneously — `max_active_runs=1` on `geo_pipeline_daily` enforces this at the orchestration layer
- Silver is **ephemeral** (ADR-011): temporary Parquet files in MinIO are deleted after the deploy gate passes. No Silver data persists beyond the pipeline run that created it.

---

## Service Endpoints

| Service | URL | Credentials |
|---------|-----|-------------|
| FastAPI serving layer | `http://localhost:8002` | None (no auth on public PSA data endpoints) |
| FastAPI interactive docs | `http://localhost:8002/docs` | None |
| FastAPI health check | `http://localhost:8002/health/ready` | None |
| Martin tile server | `http://localhost:3002` | None |
| Airflow UI | `http://localhost:8080` | `admin` / `$AIRFLOW_ADMIN_PASSWORD` |
| Prometheus Pushgateway | `http://localhost:9091` | None |

---

## Running the System

### Single pipeline run (manual)

```bash
# Upload a shapefile archive to the monitored bucket
mc cp /path/to/psa_provincial_2023.7z minio/geo-uploads/

# Trigger pipeline (or wait for MinioSensor auto-detection within 15 min)
airflow dags trigger geo_pipeline_daily

# Verify registry updated for the new dataset
psql $POSTGRES_DSN -c "
SELECT dataset_name, status, operation_mode, feature_count
FROM geo_ingest_registry
ORDER BY dw_created_ts DESC
LIMIT 3;
"
```

### API queries

```bash
# Valid: H3 aggregates at resolution 6 for a bounding box
curl "http://localhost:8002/geo/v1/h3/6?bbox=120.5,14.0,121.5,15.0&indicator=poverty_rate"

# Valid: vector tiles via Martin proxy
curl "http://localhost:8002/geo/v1/tiles/10/823/512.mvt"

# Valid: GeoJSON region layer
curl "http://localhost:8002/geo/v1/geojson/provincial_boundaries?bbox=120.5,14.0,121.5,15.0"

# Valid: metadata with Jenks breaks
curl "http://localhost:8002/geo/v1/metadata/psa_provincial_2023"

# Rejected: bbox outside Philippine envelope → HTTP 422
curl "http://localhost:8002/geo/v1/h3/6?bbox=0,0,1,1&indicator=poverty_rate"

# Rejected: invalid resolution → HTTP 422
curl "http://localhost:8002/geo/v1/h3/10?bbox=120.5,14.0,121.5,15.0"
```

### Scheduled runs

`geo_pipeline_daily` runs every 15 minutes. Activation:

```bash
airflow dags unpause geo_pipeline_daily
```

---

## API Reference

> **Read-only.** All endpoints use `read_conn()` (per-thread DuckDB read connection). No write path is exposed. DuckDB `memory_limit` applied at connection open per `DUCKDB_MEMORY_LIMIT` env var.

| Method | Path | Description | Parameters |
|--------|------|-------------|------------|
| `GET` | `/geo/v1/tiles/:z/:x/:y.mvt` | Proxy to Martin tile server for PMTiles vector tiles | Path: `z`, `x`, `y` (integer) |
| `GET` | `/geo/v1/h3/:resolution` | H3 hexagonal aggregate values for a bounding box | Path: `resolution` (5, 6, or 7). Query: `bbox` (lon_min,lat_min,lon_max,lat_max), `indicator` (string, whitelist regex) |
| `GET` | `/geo/v1/geojson/:layer` | GeoJSON feature collection for a layer | Path: `layer` (string). Query: `bbox` (optional) |
| `GET` | `/geo/v1/metadata/:dataset` | Jenks breaks, column metadata, and schema fingerprint for a dataset | Path: `dataset` (string) |
| `GET` | `/health/live` | Liveness probe — returns 200 if process is up | None |
| `GET` | `/health/ready` | Readiness probe — checks DuckDB, PostgreSQL, MinIO connectivity | None |

---

## Configuration

```dotenv
# ── Data paths ────────────────────────────────────────────────────────────────
MINIO_ENDPOINT=http://minio:9000
MINIO_UPLOAD_BUCKET=geo-uploads
MINIO_BRONZE_BUCKET=geo-bronze
MINIO_GOLD_BUCKET=geo-gold
MINIO_PMTILES_BUCKET=geo-pmtiles

# ── PostgreSQL ────────────────────────────────────────────────────────────────
POSTGRES_DSN_FILE=/run/secrets/postgres_dsn
POSTGRES_DB=geo_platform

# ── DuckDB ────────────────────────────────────────────────────────────────────
DUCKDB_PATH=/app/duckdb/geo_analytics.duckdb
DUCKDB_MEMORY_LIMIT=2GB

# ── FastAPI ───────────────────────────────────────────────────────────────────
API_HOST=0.0.0.0
API_PORT=8002
API_WORKERS=4
LOG_LEVEL=INFO

# ── Rate limits ──────────────────────────────────────────────────────────────
TILE_RATE_LIMIT=500/minute
H3_RATE_LIMIT=100/minute
GEOJSON_RATE_LIMIT=50/minute

# ── Pipeline ──────────────────────────────────────────────────────────────────
H3_MIN_OCCUPANCY_RATIO=0.70
H3_MIN_FEATURES_PER_HEX=2
TIPPECANOE_VERSION=2.67.0
TIPPECANOE_SHA256=a3f7e9b2d6c14f8b9e0a1d5c7f2e4b8a9d3c6f1e2b5a8d4c7f0e3b6a9d2c5f8

# ── Integration ──────────────────────────────────────────────────────────────
ECONINTEL_API_URL=                          # Optional — unset = graceful degradation
```

---

## Data Contracts / Schema

### `geo_ingest_registry` (idempotency authority)

```yaml
key: archive_hash (SHA-256 of 7z archive)
fields:
  dataset_name: string              # e.g. "psa_provincial_2023"
  status: enum [EXTRACTED, SUCCESS, QUARANTINED, FAILED]
  operation_mode: enum [analytical, geometry_only, boundary_catalog]
  feature_count: integer
  pipeline_run_id: UUID
  extracted_at: TIMESTAMPTZ
  bronze_written_at: TIMESTAMPTZ
  gold_written_at: TIMESTAMPTZ
  tiles_generated_at: TIMESTAMPTZ
  failure_reason: string | null
idempotency: re-uploading same archive (identical SHA-256) → status ALREADY_INGESTED, no reprocessing
```

### `dim_region` (SCD Type 2 — boundary change history)

```yaml
grain: one row per (region_nk, valid_from) — historical boundaries preserved
key_fields:
  region_sk: BIGINT PRIMARY KEY (IDENTITY)
  region_nk: VARCHAR(12) NOT NULL          # PSGC natural key — stable across vintages
  region_code: VARCHAR(12) NOT NULL
  region_name: VARCHAR(100) NOT NULL
  admin_level: enum [barangay, municipality, city, province, region, national]
  parent_region_nk: VARCHAR(12) | null
  geom_z4, geom_z8, geom_z12: GEOMETRY(MultiPolygon, 4326)
  geom_centroid: GEOMETRY(Point, 4326) GENERATED ALWAYS
  valid_from: DATE NOT NULL
  valid_to: DATE | null                    # NULL = current
  is_current: BOOLEAN NOT NULL DEFAULT TRUE
  row_hash: CHAR(64) NOT NULL              # SHA-256 of all non-audit fields
  repair_method: enum [SHAPELY_MAKE_VALID, POSTGIS_ST_MAKEVALID, NONE]
  source_archive_hash: CHAR(64) NOT NULL
scd_type: 2
rationale: Administrative boundaries change across census vintages. Historical poverty incidence must remain joinable to historical boundaries, not current ones.
```

### `fact_geo_observation` (transaction fact — one row per measurement)

```yaml
grain: one row per (region_sk, indicator_sk, vintage_sk, date_sk)
key_fields:
  obs_sk: BIGINT PRIMARY KEY (IDENTITY)
  region_sk: FK → dim_region
  indicator_sk: FK → dim_indicator
  vintage_sk: FK → dim_vintage
  date_sk: FK → dim_date (YYYYMMDD integer key)
  indicator_value: NUMERIC(10,4) NOT NULL
  confidence_low, confidence_high: NUMERIC(10,4)
  sample_size: INTEGER
  source_archive_hash: CHAR(64) NOT NULL
  pipeline_run_id: UUID NOT NULL
  is_quarantined: BOOLEAN DEFAULT FALSE
quality_gates:
  - indicator_value > 0 enforced at Silver quality layer
  - PSGC coverage ≥ 95% per batch (YAML contract)
  - geometry validity: shapely.is_valid after make_valid
```

### `fact_geo_h3_aggregate` (H3 hexagonal aggregate)

```yaml
grain: one row per (h3_index, h3_resolution, indicator_sk, vintage_sk)
key_fields:
  agg_sk: BIGINT PRIMARY KEY (IDENTITY)
  h3_index: VARCHAR(20) NOT NULL
  h3_resolution: SMALLINT CHECK 0-15
  indicator_sk: FK → dim_indicator
  vintage_sk: FK → dim_vintage
  indicator_mean: NUMERIC(10,4) NOT NULL
  indicator_std: NUMERIC(10,4)
  feature_count: INTEGER NOT NULL
  jenks_class: SMALLINT CHECK 1-5
  jenks_breaks: JSONB NOT NULL
  pipeline_run_id: UUID NOT NULL
resolution_selection: Dynamic per dataset via occupancy validation (ADR-009)
  - Candidate resolutions: 5, 6, 7
  - Accepted threshold: ≥70% hex occupancy, ≥2 features per occupied hex
  - Stored in geo_schema_registry.accepted_h3_resolution per dataset
```

---

## Development

### Without Airflow (scripts only)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

export POSTGRES_DSN=postgresql://geo_test:geo_test_pw@localhost:5432/geo_test
export MINIO_ENDPOINT=http://localhost:9000

# Initialize schema
psql $POSTGRES_DSN -f sql/init/001_geo_platform_schema.sql

# Run unit tests
pytest tests/unit/ -v --cov=geo_service --cov-report=xml

# Run integration tests
pytest tests/integration/ -v

# Run fitness functions
pytest fitness_functions/ -v

# Run load test (SLO check)
locust -f tests/load/locustfile.py --headless -u 50 -r 10 --run-time 60s --host http://localhost:8002
```

### Tests

| Suite | Command | Coverage |
|-------|---------|----------|
| Unit tests | `pytest tests/unit/ -v` | SHA-256 dedup, CRS reproject, H3 roundtrip, Jenks determinism, geometry validity |
| Integration tests | `pytest tests/integration/ -v` | Bronze writer → MinIO readback, PSGC crosswalk join, DuckDB ASOF JOIN, tippecanoe PMTiles validity |
| Fitness functions | `pytest fitness_functions/ -v` | No Bronze/Silver access from serving layer, ProcessPoolExecutor only in Silver, idempotency contract |
| Load test | `locust -f tests/load/locustfile.py` | p95 tile latency < 50ms SLO |

### Lint

```bash
ruff check geo_service/ && black --check geo_service/ && mypy geo_service/
```

---

## Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `duckdb.IOException: could not obtain file lock` | Pipeline write connection did not release lock (crash or stale process) | Restart geo-service container — DuckDB lock is OS-level, restart releases all file handles. Verify `write_conn()` uses `try/finally` |
| Quarantine rate > 5% alert | PSGC code mismatch, missing `.prj`, or geometry unrepairable | Check `geo_quarantine.failure_reason` for run_id. Update `psgc_crosswalk` if new PSA vintage. Obtain correct `.prj` from source if CRS missing |
| Martin OOM / crash | PMTiles file exceeds memory cache (512MB) or national-scale z12 tiles too large | Restart Martin (stateless — zero data risk). Regenerate with `--maximum-zoom=10` if file > 500MB. Increase `memory-cache-size` in `docker-compose.prod.yml` |
| `tippecanoe` CI build time regression | Source compilation triggered instead of pre-built binary | Verify `TIPPECANOE_SHA256` matches downloaded binary in Dockerfile. Check weekly version-check workflow for available updates |
| SARIMA columns all NULL (not applicable — this is the geospatial platform, not psx-analytics) | N/A | N/A |
| FastAPI 503 under load | Workers using `write_conn()` instead of `read_conn()` | Verify all endpoint handlers call `read_conn()`. DuckDB `read_only=True` supports unlimited concurrent readers |
| `geo_schema_registry` shows `accepted_h3_resolution = null` | Occupancy validation failed for all candidate resolutions | Dataset may be geometry-only (no numeric attributes). Check `operation_mode` in `geo_ingest_registry`. Run `profile_shapefile()` manually to confirm |
| Manifest missing (not applicable — uses `geo_ingest_registry` not manifest.json) | N/A | N/A |

---

## Tech Stack

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Orchestration | Apache Airflow | 2.9.3 | LocalExecutor, `max_active_runs=1` for serialized pipeline |
| Object storage | MinIO S3A | RELEASE.2024-10-02 | Erasure-coded, S3-compatible, zero cloud dependency |
| Geometry | GeoPandas + Shapely | ≥0.14, ≥2.0 | `make_valid` before simplify, WKB encoding |
| CRS | pyproj | ≥3.6 | Explicit EPSG:4326 normalization |
| Analytics | DuckDB + spatial extension | Latest stable | ASOF JOIN (PostGIS does not natively support) |
| H3 aggregation | h3-py + mapclassify | ≥3.7, ≥2.6 | Dynamic resolution selection, Jenks NaturalBreaks |
| 7z extraction | py7zr | ≥0.20 | No system dependency on `7z` CLI |
| Tile generation | tippecanoe | v2.67.0 | Pre-built binary, SHA-256 pinned (ADR-008) |
| Tile serving | Martin | v0.14.0 | PMTiles HTTP range proxy, 512MB memory cache |
| API | FastAPI + uvicorn | Latest stable | 4 workers, non-root user, structured logging |
| Rate limiting | slowapi | Latest stable | Per-endpoint tiers, IP-based keys |
| Cache | TTLCache (custom) | In-repo | 3600s TTL, key includes all query params |
| Schema | PostgreSQL + PostGIS | 16-3.4 | Kimball SCD Type 2, RLS, conformed dimensions |
| CI/CD | GitHub Actions | — | ruff, mypy, pytest, Trivy container scan, schema drift detection |
| Testing | pytest + locust | 8.x | Unit, integration, fitness functions, load SLO check |

---

## License

```
MIT License

Copyright (c) 2026 Herald V. Collamar

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

**Third-party data attribution:**
- Philippine Statistics Authority (PSA) — census boundaries, FIES indicators
- National Mapping and Resource Information Authority (NAMRIA) — administrative shapefiles
- Commission on Elections (COMELEC) — election boundary delimitations

All government data is used under Philippine open data policies. The platform
does not claim ownership over source datasets. Generated artifacts (PMTiles,
GeoParquet, analytical views) are derivative works licensed under MIT.

**Third-party software:**
- tippecanoe (BSD-2-Clause) — PMTiles generation
- Martin (MIT) — tile server
- DuckDB spatial extension (MIT) — analytical queries
- h3-py (Apache-2.0) — hexagonal aggregation
- mapclassify (BSD-3-Clause) — Jenks natural breaks

See [`LICENSE`](LICENSE) and [`LICENSES-THIRD-PARTY`](LICENSES-THIRD-PARTY) for full terms.

---

*Data: PSA + NAMRIA + COMELEC (Philippine government open data)*  
*System design: v2.2 · Updated: 2026-06-08*
