# Architecture Reference — PH Geospatial Intelligence Platform v2.2

> **Precedence note (C-06 resolution):** The ranking below supersedes the blueprint's
> Section 2.1 (which listed Reliability → Data Integrity → Evolvability). This document
> is the authoritative source per Source-of-Truth Matrix rule P1 > P2 — the release
> package takes precedence over the system blueprint when they conflict. The shift
> reflects the BYOD context: a public tile API serving external users makes Availability
> the dominant operational concern, whereas the blueprint was written from a pure
> data-integrity-first pipeline perspective. Both views are correct in their domain;
> this document governs runtime behaviour.

## 1. Ranked Architectural Characteristics

Architectural fitness functions (see `fitness_functions/`) enforce these priorities in order:

| Rank | Characteristic | Rationale |
|------|----------------|-----------|
| 1 | **Availability** | Public tile API serves MapLibre clients; downtime is user-visible. Read path is read-only DuckDB per-thread — no write contention on serving. |
| 2 | **Scalability** | Bronze→Silver→Gold pipeline processes PSGC nationwide boundaries; H3 aggregation at multiple resolutions. ProcessPoolExecutor in silver layer scales across cores (ADR-007). |
| 3 | **Testability** | Fitness functions enforce architectural invariants via string/AST contracts, not integration tests. 48/48 CI gates enforced. |
| 4 | **Deployability** | Docker + docker-compose.prod.yml; Airflow DAG drives pipeline; GitHub Actions CI gates all merges. |

---

## 2. Coupling Analysis

| Consumer | Provider | Contract Type | Notes |
|----------|----------|---------------|-------|
| `bronze.writer` | `extract.inspector` | File contract (Parquet) | Bronze reads inspector output from `/data/bronze/`; no direct import. |
| `silver.simplify` | `bronze.writer` | File contract (GeoParquet) | Silver reads bronze Parquet; decoupled by path convention. |
| `silver.parallel` | `silver.simplify` | In-process (ProcessPool) | Orchestrator only — GEOS never called inside thread workers (ADR-007). |
| `gold.h3_aggregate` | `silver.simplify` | File contract (GeoParquet) | Gold reads silver output; spatial extension loaded per-conn. |
| `gold.pmtiles` | `gold.h3_aggregate` | File contract (Parquet) | PMTiles layer reads H3 aggregates; tippecanoe called as subprocess. |
| `api.routes.*` | `infra.duckdb_conn` | Depends() injection | FastAPI routes receive read conn via `Depends(get_duckdb_conn)`; no direct instantiation. |
| `api.main` | `infra.metrics` | Direct import | `REQUEST_DURATION` histogram and `metrics_app` imported once at app init. |
| `dags.geo_pipeline_daily` | `pipeline.*` | Airflow PythonOperator | DAG imports pipeline callables; deploy_gate uses ALL_SUCCESS trigger (fitness fn enforced). |

---

## 3. ADR Index

| ID | Title | Decision |
|----|-------|----------|
| ADR-001 | DuckDB as analytical engine | Single-file DuckDB replaces PostgreSQL for analytical queries; spatial extension bundled from v0.10+. |
| ADR-002 | Per-thread read connections | `threading.local()` read-only connections avoid GIL contention on FastAPI worker threads. |
| ADR-003 | GeoParquet over Shapefile | GeoParquet with Hilbert bbox covering index; ogr2ogr conversion from PSA shapefiles. |
| ADR-004 | H3 resolution strategy | Resolution 6 for province-level, resolution 8 for barangay; Jenks natural breaks for choropleth classes. |
| ADR-005 | PMTiles over MBTiles | PMTiles served from object storage (MinIO); no tile server process required on serving path. |
| ADR-006 | structlog over logging | structlog with contextvars enables per-request `request_id` binding without thread locals. |
| ADR-007 | ProcessPoolExecutor in silver | GEOS is not thread-safe; silver simplification runs in process-isolated workers. ThreadPoolExecutor banned in silver modules except `parallel.py` orchestrator. |
| ADR-008 | SlowAPI rate limiting | 60 req/min per IP on all routes; protects DuckDB from query storms. |
| ADR-009 | Airflow DAG deploy gate | `deploy_gate` task uses ALL_SUCCESS trigger; partial pipeline success must not promote to Gold. |
| ADR-010 | Secrets via /run/secrets | No hardcoded credentials in source; MinIO access keys injected via Docker secrets or env. |
| ADR-011 | PSGC as conformed dimension | PSA PSGC code is the join key across boundaries, poverty indicators, and GDP indicators. |
| ADR-012 | Hilbert bbox covering index | `write_covering_bbox=True` on all GeoParquet writes; DuckDB row-group pruning confirmed via EXPLAIN. |
