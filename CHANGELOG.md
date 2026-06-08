# Changelog

All notable changes to **PH Geospatial Intelligence Platform** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [2.2.0] — 2026-06-08 — BYOD Production Ready

### Summary

Full BYOD (Bring Your Own Data) hardening pass. The platform is now data-agnostic:
any user with PSA/NAMRIA-compatible `.7z` geospatial archives — or any standard
shapefile/GeoPackage dataset — can clone, configure `.env`, and run the full pipeline
without modifying a single line of Python code.

All 56 audit checks verified PASS (15 mechanical + 4 cross-cutting + 23 architecture
+ 14 BYOD). Zero hidden failures. Architecture debt is documented and tracked.

### Added

- `geo_service/pipeline/extract/archive.py` — `find_new_archives()` and
  `extract_archive()` with recursive `.shp` discovery, skip logic for non-geospatial
  files, multi-shapefile support, and `LAYER_FILTER` env var.
- `.env.example` — 30+ environment variables documented with defaults and descriptions.
- `docs/BYOD_GUIDE.md` — format support matrix, column mapping guide, indicator
  registration workflow, first-run checklist, troubleshooting table.
- `docs/ARCHITECTURE.md` — ranked architectural characteristics, coupling analysis
  table, ADR index (ADR-001–ADR-012). C-06 precedence note documents that this
  ranking (Availability → Scalability → Testability → Deployability) supersedes the
  blueprint's original Reliability-first ranking for the public API surface.
- `docs/AUDIT-CORRECTION-FAANG03.md` — correction note for stale FAIL entry in
  external BYOD audit. FAANG-03 was fixed during the same session the external audit
  was written; internal audit and `health.py` both confirm PASS.
- `docs/SOURCE-OF-TRUTH-MATRIX.md` — independent document authority hierarchy,
  conflict resolution rules, change management chains, and open items registry.
- `geo_service/api/limiter.py` — extracted from `main.py` to resolve circular import
  (FIX-05 / CC-01).

### Changed

- `geo_service/api/routes/health.py` — `/health/ready` now probes all four external
  dependencies: DuckDB (live spatial query), Martin (HTTP GET), MinIO (boto3
  `HeadBucket` + HTTP fallback), PostgreSQL (`SELECT 1` via psycopg2 + asyncpg
  fallback). Returns HTTP 503 if any dependency is unhealthy. (FAANG-03 fix.)
- `geo_service/config.py` — added `MINIO_BUCKET`, `MINIO_ENDPOINT`,
  `MINIO_ARCHIVE_PREFIX`, `BRONZE_BUCKET`, `SILVER_TEMP_BUCKET`, `GOLD_BUCKET`,
  `ARCHIVE_LOCAL_STAGING`, `TARGET_CRS`, `REGION_COLUMN_MAP`, `LAYER_FILTER`,
  `SILVER_MAX_WORKERS`, `SILVER_MEMORY_LIMIT_MB`. All external paths via `os.getenv()`.
- `geo_service/infra/duckdb_conn.py` — tenacity retry with exponential backoff
  (`stop_after_attempt(3)`, `wait_exponential(min=1, max=10)`) on both read and write
  connections. (DDIA-01 fix.)
- `geo_service/api/main.py` — `RequestIDMiddleware` generates UUID per request and
  binds via `structlog.contextvars`. Prometheus `/metrics` endpoint via
  `WSGIMiddleware`. (FAANG-01, FAANG-05 fixes.)
- `geo_service/infra/logging_config.py` — structlog `JSONRenderer` with ISO 8601
  timestamps; `request_id` bound per request. (FAANG-01 fix.)
- `geo_service/infra/metrics.py` — added `INGESTION_RUNS` Prometheus counter with
  `archive_name`, `status`, `vintage` labels (BYOD-12).
- `geo_service/pipeline/silver/parallel.py` — `SILVER_MAX_WORKERS` and
  `SILVER_MEMORY_LIMIT_MB` via env. `QuarantineEntry` enhanced with `archive_path`
  (str) and `quarantined_at` (UTC ISO 8601 auto-stamp). (BYOD-05.)
- `geo_service/api/routes/h3.py` — `VALID_RESOLUTIONS = frozenset(range(16))` (0–15);
  `VALID_H3_RESOLUTIONS` env-restrict; `_PH` bbox from four `TARGET_BBOX_*` env vars;
  `RATE_LIMIT_H3` env-driven. (BYOD-09, BYOD-10, BYOD-11.)
- `geo_service/api/routes/geojson.py` — `RATE_LIMIT_GEOJSON` env-driven. (BYOD-11.)
- `dags/geo_pipeline_daily.py` — `schedule_interval` → `schedule` for Airflow 3
  compatibility (FIX-07). `task_inspect` uses `DiscoveredArchive` from `archive.py`.
  Bucket names read from `settings.*_BUCKET`. (BYOD-08.)
- `docs/RUNBOOK.md` — Day 7 addendum added. Scenario 6: full indicator registration
  workflow with pre-register, list, regulatory flag, safe delete, and quarantine
  diagnosis queries. (BYOD-06.)
- `docs/ph_geospatial_explainer_llm.md` — characteristics ranking corrected to v2.2
  BYOD canonical (Availability → Scalability → Testability → Deployability). Deploy
  gate count corrected (46 → 49). BYOD section added. Source references updated.
- `sql/init/001_geo_platform_schema.sql` — `dim_region` and `dim_indicator` empty on
  fresh install (no hardcoded region/indicator INSERTs). `geo_pipeline_role` restricted
  to `SELECT, INSERT` on fact tables; `UPDATE`, `DELETE` revoked. (KIMBALL-04 fix,
  BYOD-04.)
- `fitness_functions/test_string_contracts.py` — docstring reworded to remove
  `os.path.exists` literal, eliminating CI false-positive. (FIX-01.)

### Fixed

| ID | Description |
|----|-------------|
| FIX-01 | Docstring false-positive in fitness_functions caused CI collection error. |
| FIX-05 / CC-01 | Circular import between `api.main` and `api.routes.*` via shared limiter. |
| FIX-07 | Airflow 3 incompatibility — `schedule_interval` deprecated parameter. |
| FAANG-01 | Plain-text logs — replaced with structlog JSON + per-request `request_id`. |
| FAANG-03 | `/health/ready` only checked DuckDB and Martin — MinIO and PostgreSQL probes added. |
| FAANG-05 | No Prometheus metrics endpoint — `/metrics` with `REQUEST_DURATION` histogram added. |
| KIMBALL-04 | Fact table append-only was convention only — DB permissions now enforce it. |
| DDIA-01 | DuckDB connection failures crashed pipeline — tenacity retry added. |

### Known Tracked Debt (non-blocking for v2.2)

| ID | Item | Next Sprint |
|----|------|-------------|
| DDIA-03 | No dedicated dead-letter queue for failed DAG run payloads. | Yes |
| DDIA-04 | Inspector has no checkpoint/resume — reprocesses full archive on retry. | Evaluate at >500 MB |
| FAANG-02 | Deploy gate static checks only — no live connectivity probes. | Yes |
| FAANG-06 | No circuit breaker — tenacity retry only (DuckDB is local). | Evaluate under load |
| RENDER-01 | 4K PNG uses PlateCarree projection — Robinson required for r/MapPorn. | Before Reddit submit |
| DATA-01 | Synthetic data in `day1_profile_report.json` and `day4_choropleth_4k.png`. | Before production traffic |

---

## [2.1.0] — 2026-06-07 — Remediation Pass (Stage 4C)

### Added

- `geo_service/infra/metrics.py` — Prometheus `REQUEST_DURATION` histogram, `metrics_app`
  via `WSGIMiddleware`, `/metrics` endpoint. (FAANG-05.)
- `docs/ARCHITECTURE.md` — initial version with ranked characteristics, coupling table,
  ADR index. (FOSA-01, FOSA-03.)

### Changed

- `geo_service/infra/duckdb_conn.py` — tenacity retry added. (DDIA-01.)
- `geo_service/api/main.py` — `RequestIDMiddleware` added; `/metrics` mounted. (FAANG-01.)
- `geo_service/infra/logging_config.py` — structlog JSON renderer with `request_id` binding.
- `requirements.txt` — added `tenacity`, `prometheus-client`.

### Fixed

- `fitness_functions/test_string_contracts.py` — docstring literal `os.path.exists` removed.

---

## [2.0.0] — 2026-06-06 — Day 1–6 Sprint Complete

Initial 7-day sprint delivering:

- Bronze→Silver→Gold medallion pipeline via Airflow DAG with `BranchPythonOperator`.
- DuckDB + spatial extension for analytical queries and geometry operations.
- PMTiles generated via tippecanoe, served by Martin tile server.
- FastAPI `/geo/v1` with rate limiting (SlowAPI), structured error handling, SCD Type 2
  dimensional model (Kimball), H3 aggregation with Jenks natural breaks.
- 49-check deploy gate (`run_all_checks()`). CI: ruff → mypy → unit → integration →
  fitness → Docker build → Trivy scan.
- 4K choropleth PNG (synthetic data, PlateCarree projection).
- Docker Compose production topology with non-root user, Docker secrets, health checks.

---

*Maintained by Herald Collamar ([@raldisk](https://github.com/raldisk))*
