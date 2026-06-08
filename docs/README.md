# PH Geospatial Intelligence Platform

> Production-grade geospatial data engineering platform ingesting Philippine government
> shapefile archives through a Bronze→Silver→Gold medallion lakehouse, generating PMTiles
> vector tiles, and publishing analytical outputs to Reddit.

**Author:** Herald V. Collamar  
**Status:** Phase 1 Complete — External Validation (Day 6)  
**Sprint:** v2.2 Compressed 4–7 Day Execution

---

## Architecture Overview

```
PSA / NAMRIA / COMELEC
        │
        ▼ (7z archive → SHA-256 dedup → MinioSensor trigger)
┌─────────────────────────────────────────────────────────────┐
│                    BRONZE LAYER                             │
│  Raw immutable GeoParquet copy. write_covering_bbox=True.   │
│  Audit trail: geo_ingest_registry (PostgreSQL).             │
└─────────────────────────┬───────────────────────────────────┘
                          │ (ephemeral Silver: CRS normalize →
                          │  shapely.make_valid() → simplify z4/z8/z12
                          │  → ProcessPoolExecutor (ADR-007)
                          │  → deleted after Gold verification)
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                     GOLD LAYER                              │
│  H3 hexagonal aggregation (dynamic resolution, ADR-009).    │
│  Jenks natural breaks classification.                       │
│  GeoParquet with Hilbert bbox indexing (ADR-012).           │
│  DuckDB spatial ASOF JOIN → poverty × GDP cross-domain.     │
└─────────────────────────┬───────────────────────────────────┘
                          │
             ┌────────────┴────────────┐
             ▼                         ▼
┌────────────────────┐     ┌───────────────────────┐
│  tippecanoe        │     │  FastAPI /geo/v1       │
│  PMTiles archive   │     │  - /tiles/{z}/{x}/{y}  │
│  (HTTP range req.) │     │  - /h3/{resolution}    │
│  Martin tile srv.  │     │  - /geojson            │
│  CDN-compatible    │     │  - /metadata           │
└────────────────────┘     └───────────────────────┘
             │                         │
             └────────────┬────────────┘
                          ▼
             ┌─────────────────────────┐
             │  MapLibre GL / Deck.gl  │
             │  4K Matplotlib PNG      │
             │  r/MapPorn  ·  r/dataisbeautiful │
             └─────────────────────────┘
```

### Architecture Decision Records (ADRs)

| ADR | Decision | Rationale |
|-----|----------|-----------|
| ADR-001 | PMTiles over MBTiles | HTTP range requests → serve from MinIO S3A without tile server for static datasets |
| ADR-002 | DuckDB spatial over PostGIS for analytics | Only engine supporting ASOF JOIN for cross-domain temporal correlation |
| ADR-007 | ProcessPoolExecutor for Silver GEOS ops | Isolates GEOS SIGSEGV from serving process |
| ADR-008 | Pre-built tippecanoe binary | SHA-256 pinned; avoids 8-min CI compile per run |
| ADR-009 | Dynamic H3 resolution per dataset | Avoids mostly-ocean H3 cells in island province contexts |
| ADR-011 | Silver is ephemeral | Annual-batch immutable shapefiles don't need three materialized layers |
| ADR-012 | GeoParquet Hilbert bbox indexing | ~10× DuckDB regional query speedup via row-group pruning |

---

## Stack

| Layer | Technology |
|-------|------------|
| Pipeline orchestration | Apache Airflow 2.9.3 (LocalExecutor) |
| Object storage | MinIO S3A (Bronze + Gold + PMTiles) |
| Geometry processing | Python · GeoPandas · Shapely · GDAL |
| Spatial analytics | DuckDB spatial extension |
| H3 aggregation | Uber H3 (dynamic resolution, ADR-009) |
| Classification | Jenks natural breaks (stored as metadata at Gold time) |
| Tile generation | tippecanoe PMTiles (pre-built binary, ADR-008) |
| Tile serving | Martin v0.14.0 · FastAPI /geo/v1 |
| Frontend | Deck.gl HexagonLayer · MapLibre GL |
| Static visualization | Matplotlib (Robinson projection, 3840×2160) |
| Schema & audit | PostgreSQL 16 + PostGIS 3.4 |
| CI/CD | GitHub Actions (matrix: lint · unit · integration · Trivy) |
| Containerization | Docker multi-stage · docker-compose.prod.yml |

---

## Data Sources & Attributions

### Primary Sources

**Philippine Statistics Authority (PSA)**  
Administrative boundary shapefiles and statistical datasets.  
Source: [https://psa.gov.ph](https://psa.gov.ph)  
License: Open Government Data — Philippine government data is publicly released for non-commercial and research use. Attribution required.

> *Boundary data derived from PSA administrative shapefiles. Statistical indicators
> (poverty incidence, population) sourced from PSA census and survey releases.
> This platform is not affiliated with PSA. All PSA data is used in accordance
> with the Philippine Open Data Portal terms of use.*

**National Mapping and Resource Information Authority (NAMRIA)**  
Authoritative base cartographic layers and geodetic reference data.  
Source: [https://www.namria.gov.ph](https://www.namria.gov.ph)  
License: Government reference data — attribution required per NAMRIA data sharing policy.

> *Cartographic reference data sourced from NAMRIA. Boundary geometries may have
> been simplified for web tile delivery (z4/z8/z12 tolerances). NAMRIA is the
> authoritative source; this platform's simplified geometries are not suitable
> for legal boundary determination.*

**Commission on Elections (COMELEC)**  
Electoral precinct and constituency boundary shapefiles.  
Source: [https://comelec.gov.ph](https://comelec.gov.ph)  
License: Public domain — Philippine government electoral data.

### Data Processing Notes

- All geometries reprojected to EPSG:4326 (WGS 84) from source CRS.
- Geometries validated and repaired with `shapely.make_valid()` (ADR-010).
- PSGC codes normalized via crosswalk table covering 2015–2023 vintage transitions.
- Simplification tolerances: z4 (coarse national), z8 (provincial), z12 (municipal detail).
- Features failing geometry repair are quarantined and excluded from analytical outputs.

---

## Repository Structure

```
ph-geospatial-platform/
├── geo_service/
│   ├── pipeline/
│   │   ├── bronze/          # Raw archive ingestion + SHA-256 dedup
│   │   ├── silver/          # Ephemeral CRS normalize + repair (ADR-011)
│   │   │   └── parallel.py  # ProcessPoolExecutor GEOS isolation (ADR-007)
│   │   ├── gold/
│   │   │   ├── h3_aggregate.py   # Dynamic H3 + Jenks
│   │   │   └── pmtiles.py        # tippecanoe wrapper
│   │   └── lineage.py       # geo_lineage_edges population
│   ├── api/
│   │   ├── routes/          # FastAPI /geo/v1 endpoints
│   │   └── middleware/      # Rate limiting (slowapi)
│   └── infra/
│       └── duckdb_conn.py   # DuckDB connection manager
├── dags/
│   └── geo_pipeline_daily.py    # Airflow DAG (Section 25)
├── sql/
│   ├── init/
│   │   └── 001_geo_platform_schema.sql   # Complete DDL (Section 24)
│   └── lineage/
│       └── lineage_verification.sql
├── fitness_functions/
│   └── test_arch.py         # Architecture fitness functions (Section 2.2)
├── config/
│   └── quality_contracts/   # YAML quality thresholds per dataset
├── scripts/
│   └── scd_type2_manual_update.py
├── .github/
│   └── workflows/
│       └── ci.yml           # Matrix CI (Section 28)
├── Dockerfile               # Multi-stage + pre-built tippecanoe (ADR-008)
├── docker-compose.prod.yml  # Production stack (Section 26)
├── RUNBOOK.md               # Operational runbook (Section 29)
└── README.md
```

---

## Quick Start

```bash
# 1. Create Docker secrets
mkdir -p secrets
echo "postgresql://geo_pipeline_role:password@postgres:5432/geo_platform" \
  > secrets/postgres_dsn.txt
echo "minioadmin" > secrets/minio_access_key.txt
echo "minioadmin" > secrets/minio_secret_key.txt
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  > secrets/airflow_fernet_key.txt

# 2. Start full stack
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 3. Verify health
curl -f http://localhost:8002/health/ready && echo "FastAPI OK"
curl -f http://localhost:3002/health && echo "Martin OK"

# 4. Upload a shapefile archive to trigger pipeline
mc cp psa_provincial_2023.7z minio/geo-uploads/

# Airflow MinioSensor detects within 15 minutes and triggers geo_pipeline_daily DAG.
```

---

## Portfolio Context

This platform demonstrates four engineering capabilities targeting senior data engineering roles:

**Principal-engineer design thinking** — ADR-011 explicitly audits the Medallion pattern
and retains it as a constraint rather than a merit. Documenting *why* a pattern is wrong
while keeping it for operational reasons is a senior-vs-mid-level signal.

**Domain-specific performance engineering** — ADR-012's Hilbert curve bbox indexing with
`write_covering_bbox=True` and DuckDB row-group pruning, validated via `EXPLAIN` output,
is defensible cold in a technical interview. The ~10× speedup has a named mechanism.

**Operational maturity** — FMEA risk register (Section 19), 46-check deploy gate, SCD Type 2
with fitness function enforcement, and `RUNBOOK.md` collectively demonstrate that production
quality is measured at first incident, not first demo.

**Cross-domain analytical design** — Poverty incidence × GDP growth ASOF JOIN across
`dim_region`-conformed fact tables from separate PSA data sources. Kimball bus matrix
showing conformed dimensions across four fact tables is the architectural proof.

---

*System design: v2.2 · Sprint: Compressed 4–7 day · Last updated: 2026-06-05*  
*Data: PSA + NAMRIA + COMELEC (Philippine government open data)*
