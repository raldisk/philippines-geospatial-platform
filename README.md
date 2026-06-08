# PH Geospatial Intelligence Platform

> Production-grade geospatial data engineering platform for Philippine administrative
> boundaries and statistical indicators — Bronze→Silver→Gold medallion pipeline,
> PMTiles vector tiles, H3 aggregation, and FastAPI serving.

**Author:** Herald V. Collamar ([@raldisk](https://github.com/raldisk))
**Status:** v2.2 — BYOD Production Ready
**Audit:** 56 checks PASS (15 mechanical + 14 BYOD + 23 architecture)

---

## BYOD — Bring Your Own Data

Any user with PSA/NAMRIA-compatible `.7z` geospatial archives — or any standard
shapefile/GeoPackage dataset — can run the full pipeline without modifying Python code.

```bash
git clone https://github.com/raldisk/philippines-geospatial-platform
cd philippines-geospatial-platform
cp .env.example .env          # configure MinIO, PostgreSQL, Airflow credentials

# Create Docker secrets
mkdir -p secrets
echo "postgresql://geo_pipeline_role:password@postgres:5432/geo_platform" \
  > secrets/postgres_dsn.txt
echo "minioadmin" > secrets/minio_access_key.txt
echo "minioadmin" > secrets/minio_secret_key.txt

# Start full stack
docker-compose -f docker-compose.prod.yml up -d

# Verify health (all four deps probed)
curl -f http://localhost:8002/health/ready

# Upload your archive — pipeline triggers automatically
mc cp your_dataset.7z minio/geo-uploads/
```

See [`docs/BYOD_GUIDE.md`](docs/BYOD_GUIDE.md) for format support, column mapping,
indicator registration, and troubleshooting.

---

## Architecture

```
Any .7z / .shp dataset
        │
        ▼  SHA-256 dedup → MinioSensor trigger
┌───────────────────────────────────────────┐
│  BRONZE — immutable GeoParquet            │
│  write_covering_bbox=True (ADR-012)       │
│  PostgreSQL audit trail                   │
└────────────────┬──────────────────────────┘
                 │  ephemeral Silver
                 │  CRS reproject → make_valid → simplify z4/z8/z12
                 │  ProcessPoolExecutor (ADR-007) → deleted post-Gold
                 ▼
┌───────────────────────────────────────────┐
│  GOLD — H3 aggregation + Jenks breaks    │
│  DuckDB spatial ASOF JOIN                │
│  Hilbert bbox indexing (ADR-012)         │
└────────────┬───────────────┬─────────────┘
             ▼               ▼
      tippecanoe        FastAPI /geo/v1
      PMTiles           /tiles /h3 /geojson
      Martin proxy      /metadata /health
             │               │
             └───────┬───────┘
                     ▼
          MapLibre GL · Deck.gl H3
          Matplotlib 4K PNG · EconIntel
```

---

## Stack

| Layer | Technology |
|-------|------------|
| Orchestration | Apache Airflow 3.x (LocalExecutor) |
| Object storage | MinIO S3A |
| Geometry | GeoPandas · Shapely · GDAL |
| Analytics | DuckDB + spatial extension |
| H3 aggregation | Uber H3 (dynamic resolution, ADR-009) |
| Tile generation | tippecanoe PMTiles (SHA-256 pinned, ADR-008) |
| Tile serving | Martin v0.14.0 |
| API | FastAPI · SlowAPI · structlog · Prometheus |
| Schema | PostgreSQL 16 + PostGIS 3.4 (Kimball SCD Type 2) |
| CI/CD | GitHub Actions — ruff · mypy · pytest · Trivy |

---

## Docs

| Document | Purpose |
|----------|---------|
| [`docs/BYOD_GUIDE.md`](docs/BYOD_GUIDE.md) | Setup guide for external users |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operational runbook, incident scenarios |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | ADR index, coupling analysis, ranked characteristics |
| [`docs/SOURCE-OF-TRUTH-MATRIX.md`](docs/SOURCE-OF-TRUTH-MATRIX.md) | Document authority hierarchy |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |

---

## Known Tracked Debt

| Item | Tracked in |
|------|-----------|
| 4K PNG uses PlateCarree — Robinson needed for r/MapPorn | `CHANGELOG.md` RENDER-01 |
| Synthetic data — replace with real PSA/NAMRIA archives | `CHANGELOG.md` DATA-01 |
| Deploy gate static checks only (FAANG-02) | Next sprint |
| No circuit breaker on DuckDB (FAANG-06) | Evaluate under load |

---

*Data: PSA + NAMRIA + COMELEC (Philippine government open data)*
*System design: v2.2 · Updated: 2026-06-08*
