# PH Geospatial Intelligence Platform — Complete System Architecture

> **ByteByteGo-style Explainer | v2.2**  
> **For LLM Analysis & Visualization**  
> This document contains all data models, architecture decisions, and pipeline definitions extracted from the interactive visualization. The companion `.html` file is a fully self-contained interactive React app.

---

## 📁 Files Included

| File | Purpose | Size |
|------|---------|------|
| `ph_geospatial_explainer_minified.html` | **Interactive visualization** — open in browser. Single file, CDN dependencies only (React 18, Framer Motion, Tailwind, Babel). | ~64KB |
| `ph_geospatial_explainer_llm.md` | **This file** — structured data for text analysis, code generation, or feeding to other LLMs like Claude, GPT-4, Gemini. | ~25KB |

---

## 🏗️ System Overview

**PH Geospatial Intelligence Platform** is a full-stack geospatial data pipeline for Philippine government statistics (PSA — Philippine Statistics Authority, NAMRIA — National Mapping, COMELEC — Elections). It follows the Kimball dimensional architecture with a Bronze-Silver-Gold medallion pipeline.

### Core Architectural Bet
> Pre-compute everything at pipeline time (Jenks breaks, H3 aggregation, vector tiles) so the serving layer is read-only, stateless, and trivially scalable.

### Ranked Architecture Characteristics

> **v2.2 BYOD canonical ranking** (supersedes blueprint Section 2.1 per P1 > P2 SoT rule):

1. **Availability** — Public tile API serves MapLibre clients; downtime is user-visible. Read path is read-only DuckDB per-thread — no write contention on serving.
2. **Scalability** — Bronze→Silver→Gold pipeline processes nationwide boundaries; H3 aggregation at multiple resolutions. ProcessPoolExecutor in silver scales across cores (ADR-007).
3. **Testability** — Fitness functions enforce architectural invariants via string/AST contracts. 49-check deploy gate enforced in CI.
4. **Deployability** — Single `docker-compose up` brings the full stack online. GitHub Actions gates all merges.

> The blueprint's original ranking (Reliability → Data Integrity → Evolvability) remains
> valid for pipeline-internal decisions. The v2.2 BYOD ranking governs the serving layer
> and public API surface. See `docs/ARCHITECTURE.md` for the full C-06 resolution note.

---

## 📊 1. STAGES — 6 Explainer Stages (Interactive Navigation)

These are the 6 stages of the explainer UI:

```javascript
const STAGES = [
      {
        id: 'concept',
        title: 'Conceptual Foundation',
        subtitle: 'Architecture Principles & Design Decisions',
        description: 'The system begins with ranked architecture characteristics: Reliability, Data Integrity, Evolvability, Deployability, Performance, Scalability. The core bet: pre-compute everything at pipeline time so the serving layer is read-only and trivially scalable.',
        icon: 'Lightbulb',
        color: '#F59E0B',
        bgColor: '#FEF3C7',
      },
      {
        id: 'domain',
        title: 'Domain Model',
        subtitle: 'Kimball Dimensional Architecture',
        description: 'Conformed dimensions (dim_region, dim_date) enable cross-domain ASOF JOIN analysis. SCD Type 2 preserves boundary change history. The grain: one row per (administrative unit, indicator, vintage).',
        icon: 'Database',
        color: '#3B82F6',
        bgColor: '#DBEAFE',
      },
      {
        id: 'runtime',
        title: 'Runtime Architecture',
        subtitle: 'Services, Storage & Network Boundaries',
        description: 'Docker Compose topology with three network zones: geo-internal (pipeline + storage), geo-serving (FastAPI + Martin), econintel-internal (shared PostgreSQL). ProcessPoolExecutor isolates GEOS operations.',
        icon: 'Server',
        color: '#10B981',
        bgColor: '#D1FAE5',
      },
      {
        id: 'pipeline',
        title: 'Pipeline Flow',
        subtitle: 'Bronze → Ephemeral Silver → Gold → PMTiles',
        description: 'Airflow DAG with BranchPythonOperator: inspect → branch → bronze → silver (ProcessPoolExecutor) → gold (H3 + Jenks) → tippecanoe PMTiles → 49-check deploy gate. Silver is ephemeral — deleted after Gold verification per ADR-011. BYOD: operation_mode auto-detected from .dbf attributes; unknown indicators quarantined with reason code.',
        icon: 'GitBranch',
        color: '#8B5CF6',
        bgColor: '#EDE9FE',
      },
      {
        id: 'dataflow',
        title: 'Data Movement',
        subtitle: 'From Source Archive to Served Tile',
        description: 'SHA-256 dedup → GeoParquet with Hilbert bbox indexing (ADR-012) → ephemeral Silver simplification → Gold H3 aggregation with dynamic resolution → PMTiles HTTP range requests → Martin tile server → MapLibre GL.',
        icon: 'ArrowRight',
        color: '#EC4899',
        bgColor: '#FCE7F3',
      },
      {
        id: 'output',
        title: 'Produced Output',
        subtitle: 'r/MapPorn + r/dataisbeautiful + EconIntel Dashboard',
        description: 'Matplotlib 4K PNG choropleth (Robinson projection) → r/MapPorn. Deck.gl H3 HexagonLayer animation → r/dataisbeautiful. MapLibre GL vector tiles → EconIntel Dashboard. ASOF JOIN cross-domain analytics → DuckDB spatial views.',
        icon: 'Monitor',
        color: '#06B6D4',
        bgColor: '#CFFAFE',
      },
    ];
```

---

## 📊 2. ENTITIES — Domain Model (Kimball Dimensional Architecture)

### Dimensions, Facts, Registries, and Crosswalks

```javascript
const ENTITIES = [
      {
        id: 'dim_region',
        name: 'dim_region',
        type: 'dimension',
        layer: 'gold',
        description: 'SCD Type 2 administrative boundaries. PK: region_sk. NK: region_nk (PSGC). Geoms: z4, z8, z12, centroid. valid_from/valid_to/is_current.',
        fields: ['region_sk', 'region_nk', 'region_code', 'region_name', 'admin_level', 'parent_region_nk', 'geom_z4', 'geom_z8', 'geom_z12', 'geom_centroid', 'valid_from', 'valid_to', 'is_current', 'row_hash', 'repair_method', 'source_archive_hash'],
        dependencies: [],
        consumers: ['fact_geo_observation', 'fact_geo_h3_aggregate', 'psgc_crosswalk'],
      },
      {
        id: 'dim_date',
        name: 'dim_date',
        type: 'dimension',
        layer: 'gold',
        description: 'Conformed date dimension. PK: date_sk (YYYYMMDD). Pre-populated 2000-2040.',
        fields: ['date_sk', 'full_date', 'year', 'quarter', 'month', 'month_name', 'week_of_year', 'day_of_week', 'is_weekend', 'fiscal_year', 'fiscal_quarter'],
        dependencies: [],
        consumers: ['fact_geo_observation'],
      },
      {
        id: 'dim_indicator',
        name: 'dim_indicator',
        type: 'dimension',
        layer: 'gold',
        description: 'Statistical indicator catalog. is_regulatory flag routes repair method.',
        fields: ['indicator_sk', 'indicator_nk', 'indicator_name', 'unit', 'source', 'domain', 'is_regulatory', 'description'],
        dependencies: [],
        consumers: ['fact_geo_observation', 'fact_geo_h3_aggregate'],
      },
      {
        id: 'dim_vintage',
        name: 'dim_vintage',
        type: 'dimension',
        layer: 'gold',
        description: 'Data vintage tracker. psgc_vintage links to crosswalk table.',
        fields: ['vintage_sk', 'vintage_year', 'survey_round', 'reference_date', 'published_date', 'is_provisional', 'psgc_vintage'],
        dependencies: [],
        consumers: ['fact_geo_observation', 'fact_geo_h3_aggregate'],
      },
      {
        id: 'fact_geo_observation',
        name: 'fact_geo_observation',
        type: 'fact',
        layer: 'gold',
        description: 'Transaction fact: one row per (region, indicator, vintage). Confidence intervals from PSA.',
        fields: ['obs_sk', 'region_sk', 'indicator_sk', 'vintage_sk', 'date_sk', 'indicator_value', 'confidence_low', 'confidence_high', 'sample_size', 'source_archive_hash', 'pipeline_run_id', 'is_quarantined'],
        dependencies: ['dim_region', 'dim_date', 'dim_indicator', 'dim_vintage'],
        consumers: ['DuckDB ASOF JOIN views', 'API /geo/v1/geojson'],
      },
      {
        id: 'fact_geo_h3_aggregate',
        name: 'fact_geo_h3_aggregate',
        type: 'fact',
        layer: 'gold',
        description: 'H3 hexagonal aggregation with Jenks NaturalBreaks classification. Dynamic resolution 5/6/7.',
        fields: ['agg_sk', 'h3_index', 'h3_resolution', 'indicator_sk', 'vintage_sk', 'indicator_mean', 'indicator_std', 'feature_count', 'jenks_class', 'jenks_breaks', 'pipeline_run_id'],
        dependencies: ['dim_indicator', 'dim_vintage'],
        consumers: ['API /geo/v1/h3', 'Deck.gl HexagonLayer'],
      },
      {
        id: 'geo_ingest_registry',
        name: 'geo_ingest_registry',
        type: 'registry',
        layer: 'bronze',
        description: 'SHA-256 dedup registry. operation_mode: analytical | geometry_only | boundary_catalog.',
        fields: ['id', 'archive_hash', 'archive_filename', 'dataset_name', 'status', 'operation_mode', 'feature_count', 'pipeline_run_id', 'extracted_at', 'bronze_written_at', 'gold_written_at', 'tiles_generated_at', 'failure_reason'],
        dependencies: [],
        consumers: ['Pipeline idempotency check', 'Deploy gate'],
      },
      {
        id: 'geo_schema_registry',
        name: 'geo_schema_registry',
        type: 'registry',
        layer: 'bronze',
        description: 'Schema fingerprint and accepted_h3_resolution per dataset.',
        fields: ['id', 'dataset_name', 'geometry_type', 'column_names', 'numeric_columns', 'categorical_columns', 'psgc_code_column', 'accepted_h3_resolution', 'recommended_indicator', 'schema_fingerprint'],
        dependencies: [],
        consumers: ['Pipeline configuration', 'H3 resolution selector'],
      },
      {
        id: 'geo_quarantine',
        name: 'geo_quarantine',
        type: 'registry',
        layer: 'silver',
        description: 'Append-only failure partition. Alert if >5% of batch. RLS: geo_pipeline_role insert, geo_readonly_role select.',
        fields: ['id', 'archive_hash', 'dataset_name', 'pipeline_run_id', 'stage', 'failure_reason', 'feature_count', 'sample_wkt', 'quarantined_at', 'resolved', 'resolved_at', 'resolution_note'],
        dependencies: [],
        consumers: ['Operational runbook', 'Alerting'],
      },
      {
        id: 'psgc_crosswalk',
        name: 'psgc_crosswalk',
        type: 'crosswalk',
        layer: 'gold',
        description: 'Legacy PSGC code → canonical code mapping. DEFERRABLE INITIALLY DEFERRED FK.',
        fields: ['legacy_code', 'canonical_code', 'vintage_year', 'split_type', 'notes', 'loaded_at'],
        dependencies: ['dim_region'],
        consumers: ['Silver crosswalk.py', 'ASOF JOIN views'],
      },
    ];
```

### Key Design Patterns
- **Conformed dimensions** (`dim_region`, `dim_date`) enable cross-domain ASOF JOIN analysis
- **SCD Type 2** preserves boundary change history (PSGC 2023 vs 2015 splits municipalities)
- **Grain**: one row per (administrative unit, indicator, vintage)
- **SHA-256 dedup** via `geo_ingest_registry`
- **Append-only quarantine** (`geo_quarantine`) — alert if >5% of batch fails

---

## 📊 3. SERVICES — Runtime Architecture

### Docker Compose Topology (3 Network Zones)

```javascript
const SERVICES = [
      {
        id: 'geo-service',
        name: 'geo-service',
        type: 'api',
        port: 8002,
        description: 'FastAPI + uvicorn. 4 workers. Non-root user (geoservice:1001). Endpoints: /tiles, /geojson, /h3, /metadata, /health.',
        endpoints: [
          { path: '/geo/v1/tiles/:z/:x/:y.mvt', method: 'GET', desc: 'Proxy to Martin tile server' },
          { path: '/geo/v1/geojson/:layer', method: 'GET', desc: 'DuckDB ST_AsGeoJSON' },
          { path: '/geo/v1/h3/:resolution', method: 'GET', desc: 'H3 aggregates with Jenks class' },
          { path: '/geo/v1/metadata/:dataset', method: 'GET', desc: 'Jenks breaks, schema info' },
          { path: '/health/live', method: 'GET', desc: 'Liveness probe' },
          { path: '/health/ready', method: 'GET', desc: 'Readiness probe' },
        ],
        dependencies: ['Martin', 'PostgreSQL', 'DuckDB', 'MinIO'],
        network: 'geo-serving',
      },
      {
        id: 'martin',
        name: 'Martin Tile Server',
        type: 'tileserver',
        port: 3002,
        description: 'ghcr.io/maplibre/martin:v0.14.0. PMTiles HTTP range proxy. Memory cache: 512MB.',
        endpoints: [
          { path: '/:z/:x/:y.mvt', method: 'GET', desc: 'Vector tile from PMTiles archive' },
          { path: '/health', method: 'GET', desc: 'Health check' },
        ],
        dependencies: ['MinIO'],
        network: 'geo-serving',
      },
      {
        id: 'airflow',
        name: 'Airflow',
        type: 'orchestrator',
        port: 8080,
        description: 'apache/airflow:2.9.3. LocalExecutor. DAG: geo_pipeline_daily. Schedule: */15 * * * *. max_active_runs: 1.',
        endpoints: [
          { path: '/dags/geo_pipeline_daily', method: 'GET', desc: 'Pipeline DAG' },
          { path: '/graph?dag_id=geo_pipeline_daily', method: 'GET', desc: 'Task graph visualization' },
        ],
        dependencies: ['PostgreSQL', 'MinIO'],
        network: 'geo-internal',
      },
      {
        id: 'postgres',
        name: 'PostgreSQL + PostGIS',
        type: 'database',
        port: 5432,
        description: 'postgis/postgis:16-3.4-alpine. geo_platform database. RLS enabled. Service accounts: geo_pipeline_role, geo_readonly_role.',
        endpoints: [
          { path: 'dim_region', method: 'TABLE', desc: 'SCD Type 2 dimension' },
          { path: 'fact_geo_observation', method: 'TABLE', desc: 'Transaction fact' },
          { path: 'geo_ingest_registry', method: 'TABLE', desc: 'SHA-256 dedup' },
        ],
        dependencies: [],
        network: 'geo-internal',
      },
      {
        id: 'minio',
        name: 'MinIO S3A',
        type: 'storage',
        port: 9000,
        description: 'quay.io/minio/minio. Erasure coded. Buckets: geo-uploads, geo-bronze, geo-silver-tmp (ephemeral), geo-gold, geo-pmtiles, geo-quarantine.',
        endpoints: [
          { path: 's3a://geo-uploads/*.7z', method: 'OBJECT', desc: 'Incoming shapefile archives' },
          { path: 's3a://geo-bronze/', method: 'OBJECT', desc: 'Immutable raw GeoParquet' },
          { path: 's3a://geo-gold/', method: 'OBJECT', desc: 'Curated GeoParquet + Hilbert bbox' },
          { path: 's3a://geo-pmtiles/', method: 'OBJECT', desc: 'Static vector tile archives' },
        ],
        dependencies: [],
        network: 'geo-internal',
      },
      {
        id: 'duckdb',
        name: 'DuckDB Spatial',
        type: 'analytics',
        port: null,
        description: 'Embedded in-process. File: geo_analytics.duckdb. LOAD spatial. Per-thread read connections. Threading.Lock write connection. ASOF JOIN views.',
        endpoints: [
          { path: 'mart_geo_poverty_crosswalk', method: 'VIEW', desc: 'ASOF JOIN: poverty × GDP × boundary' },
          { path: 'geo_region_stats', method: 'VIEW', desc: 'Regional summary statistics' },
          { path: 'geo_h3_aggregates', method: 'VIEW', desc: 'Hexagonal aggregation views' },
        ],
        dependencies: ['MinIO', 'PostgreSQL'],
        network: 'geo-internal',
      },
    ];
```

### Network Zones
- **geo-internal** (internal: true) — Pipeline + Storage. No outbound internet.
- **geo-serving** — FastAPI + Martin tile server
- **econintel-internal** (external) — Shared PostgreSQL

### Service Details
| Service | Port | Type | Key Feature |
|---------|------|------|-------------|
| geo-service | 8002 | FastAPI | 4 workers, non-root user, rate limiting |
| Martin | 3002 | Tile Server | PMTiles HTTP range proxy, 512MB cache |
| Airflow | 8080 | Orchestrator | 15-min schedule, max_active_runs: 1 |
| PostgreSQL | 5432 | Database | PostGIS, RLS enabled |
| MinIO | 9000 | Storage | 6 buckets, erasure coded |
| DuckDB | — | Analytics | Embedded, ASOF JOIN views |

---

## 📊 4. PIPELINE_TASKS — Airflow DAG

### Bronze → Silver (Ephemeral) → Gold → PMTiles

```javascript
const PIPELINE_TASKS = [
      { id: 'sense', name: 'MinioSensor', type: 'sensor', desc: 'Poll uploads/*.7z every 60s. Timeout 14min. Mode: reschedule.', duration: '0-14min', next: ['inspect'] },
      { id: 'inspect', name: 'inspect_archive', type: 'python', desc: 'profile_shapefile() → operation_mode. XCom: dataset_profile, pipeline_run_id.', duration: '~30s', next: ['branch'] },
      { id: 'branch', name: 'branch_operation_mode', type: 'branch', desc: 'BranchPythonOperator: analytical → bronze_analytical; geometry_only|boundary_catalog → bronze_catalog; NO_NEW_ARCHIVES → skip.', duration: '~1s', next: ['bronze_a', 'bronze_b', 'skip'] },
      { id: 'bronze_a', name: 'bronze_write_analytical', type: 'python', desc: 'GeoParquet → s3a://geo-bronze/. WKB + Hilbert bbox index. Registry insert.', duration: '~2min', next: ['silver'] },
      { id: 'bronze_b', name: 'bronze_write_catalog', type: 'python', desc: 'SCD Type 2 dim_region load. No fact table generation.', duration: '~1min', next: ['silver'] },
      { id: 'skip', name: 'skip_no_new_archives', type: 'python', desc: 'No-op. Sensor false positive.', duration: '~1s', next: [] },
      { id: 'silver', name: 'silver_transform', type: 'python', desc: 'CRS normalize → make_valid → simplify z4/z8/z12. ProcessPoolExecutor: 4 workers, 300s timeout. SIGSEGV isolation. TriggerRule: ONE_SUCCESS.', duration: '~3-5min', next: ['gold'] },
      { id: 'gold', name: 'gold_generate', type: 'python', desc: 'H3 aggregate + Jenks NaturalBreaks. Dynamic resolution 5/6/7. Kimball ETL: fact_geo_observation + fact_geo_h3_aggregate. Matplotlib 4K PNG + Deck.gl artifact.', duration: '~2-3min', next: ['pmtiles'] },
      { id: 'pmtiles', name: 'pmtiles_generate', type: 'python', desc: 'tippecanoe v2.67.0 (pre-built binary, SHA-256 pinned). --maximum-tile-bytes=500000. National-scale: <50MB.', duration: '~3-5min', next: ['deploy_gate'] },
      { id: 'deploy_gate', name: 'deploy_gate', type: 'python', desc: '46-check validation. Silver temp cleanup on pass. Lineage edges → geo_lineage_edges. Metrics push → Prometheus.', duration: '~30s', next: [] },
    ];
```

### Pipeline Flow
```
MinioSensor (poll uploads/*.7z every 60s, timeout 14min)
    ↓
inspect_archive → profile_shapefile() → operation_mode (XCom)
    ↓
branch_operation_mode → [bronze_analytical | bronze_catalog | skip]
    ↓
bronze_write → GeoParquet → s3a://geo-bronze/ (WKB + Hilbert bbox index)
    ↓
silver_transform → CRS normalize → make_valid → simplify z4/z8/z12
    (ProcessPoolExecutor: 4 workers, 300s timeout, SIGSEGV isolation)
    ↓
gold_generate → H3 aggregate + Jenks NaturalBreaks (dynamic resolution 5/6/7)
    → Kimball ETL: fact_geo_observation + fact_geo_h3_aggregate
    → Matplotlib 4K PNG + Deck.gl artifact
    ↓
pmtiles_generate → tippecanoe v2.67.0 (SHA-256 pinned binary)
    --maximum-tile-bytes=500000, national-scale <50MB
    ↓
deploy_gate → 46-check validation
    → Silver temp cleanup on pass (ADR-011)
    → Lineage edges → geo_lineage_edges
    → Metrics push → Prometheus
```

---

## 📊 5. ADRS — Architecture Decision Records (12 total)

```javascript
const ADRS = [
      { id: 'adr001', code: 'ADR-001', title: 'PMTiles over MBTiles', status: 'Accepted', rationale: 'HTTP range requests native to S3. No Martin intermediary for static datasets. CDN-cacheable without config.' },
      { id: 'adr002', code: 'ADR-002', title: 'DuckDB over PostGIS for analytics', status: 'Accepted', rationale: 'ASOF JOIN support (PostGIS lacks natively). PostGIS retained for spatial predicates (ST_Within, ST_Intersects).' },
      { id: 'adr003', code: 'ADR-003', title: 'H3 + Administrative Boundaries', status: 'Accepted', rationale: 'H3 normalizes spatial variance. Admin boundaries are correct for policy reporting. Both precomputed at Gold.' },
      { id: 'adr004', code: 'ADR-004', title: 'Append-only quarantine', status: 'Accepted', rationale: 'Immutable input = debuggable. Any run is reproducible from Bronze. Quarantine provides audit trail without blocking.' },
      { id: 'adr005', code: 'ADR-005', title: 'Batch over streaming', status: 'Accepted', rationale: 'Annual PSA updates. Exactly-once via SHA-256 idempotency. 15-min polling sufficient.' },
      { id: 'adr006', code: 'ADR-006', title: 'Two-phase inspection', status: 'Accepted', rationale: 'Mandatory profile_shapefile() before Bronze write. Operation mode: analytical | geometry_only | boundary_catalog.' },
      { id: 'adr007', code: 'ADR-007', title: 'ProcessPoolExecutor for GEOS', status: 'Accepted', rationale: 'GEOS thread-safety: SIGSEGV risk. Process isolation. 15% perf overhead acceptable for batch.' },
      { id: 'adr008', code: 'ADR-008', title: 'Pre-built tippecanoe binary', status: 'Accepted', rationale: 'GitHub Releases binary, SHA-256 pinned. Weekly version check. CI time: 8-12min → ~25s.' },
      { id: 'adr009', code: 'ADR-009', title: 'Dynamic H3 resolution', status: 'Accepted', rationale: 'Per-dataset occupancy validation. ≥70% occupancy, ≥2 features/hex. Resolution stored in schema registry.' },
      { id: 'adr010', code: 'ADR-010', title: 'Two-tier geometry repair', status: 'Accepted', rationale: 'shapely.make_valid() for non-regulatory. PostGIS ST_MakeValid for is_regulatory=TRUE. Post-repair validity assertion.' },
      { id: 'adr011', code: 'ADR-011', title: 'Silver ephemeral', status: 'Accepted', rationale: 'Medallion over-engineered for annual batch. Silver = compute stage only. Deleted after Gold verification. Storage: ~1.5× source.' },
      { id: 'adr012', code: 'ADR-012', title: 'GeoParquet Hilbert bbox', status: 'Accepted', rationale: 'DuckDB row-group pruning. ~10× speedup on regional subset queries. Single kwarg: write_covering_bbox=True.' },
    ];
```

### Summary Table
| ADR | Decision | Status | Key Rationale |
|-----|----------|--------|---------------|
| ADR-001 | PMTiles over MBTiles | Accepted | HTTP range requests native to S3, CDN-cacheable |
| ADR-002 | DuckDB over PostGIS for analytics | Accepted | ASOF JOIN support; PostGIS for spatial predicates |
| ADR-003 | H3 + Administrative Boundaries | Accepted | Both precomputed at Gold for different use cases |
| ADR-004 | Append-only quarantine | Accepted | Immutable input = debuggable, reproducible runs |
| ADR-005 | Batch over streaming | Accepted | Annual PSA updates, SHA-256 idempotency |
| ADR-006 | Two-phase inspection | Accepted | Mandatory profile_shapefile() before Bronze write |
| ADR-007 | ProcessPoolExecutor for GEOS | Accepted | GEOS thread-safety: SIGSEGV risk isolation |
| ADR-008 | Pre-built tippecanoe binary | Accepted | CI time: 8-12min → ~25s |
| ADR-009 | Dynamic H3 resolution | Accepted | Per-dataset occupancy ≥70%, ≥2 features/hex |
| ADR-010 | Two-tier geometry repair | Accepted | shapely.make_valid() vs PostGIS ST_MakeValid |
| ADR-011 | Silver ephemeral | Accepted | Medallion over-engineered for annual batch |
| ADR-012 | GeoParquet Hilbert bbox | Accepted | DuckDB row-group pruning, ~10× speedup |

---

## 📊 6. SLOs — Service Level Objectives

```javascript
const SLOs = [
      { metric: 'Tile availability', target: '99.5%', window: '30-day rolling', alert: '< 99%' },
      { metric: 'Tile p95 latency', target: '< 50ms', window: '1-hour window', alert: '> 100ms' },
      { metric: 'Pipeline success rate', target: '95%', window: '7-day rolling', alert: '< 90%' },
      { metric: 'Quarantine rate', target: '< 5%', window: 'Per pipeline run', alert: '> 10%' },
      { metric: 'Schema drift incidents', target: '0/quarter', window: 'Calendar quarter', alert: 'Any detection' },
    ];
```

| Metric | Target | Window | Alert Threshold |
|--------|--------|--------|-----------------|
| Tile availability | 99.5% | 30-day rolling | < 99% |
| Tile p95 latency | < 50ms | 1-hour window | > 100ms |
| Pipeline success rate | 95% | 7-day rolling | < 90% |
| Quarantine rate | < 5% | Per pipeline run | > 10% |
| Schema drift incidents | 0/quarter | Calendar quarter | Any detection |

---

## 🔧 Technical Stack Summary

| Layer | Technology | Version/Details |
|-------|-----------|-----------------|
| Frontend Framework | React 18 | UMD via CDN |
| Animation | Framer Motion | v10.16.4 |
| Styling | Tailwind CSS | CDN |
| Icons | Lucide | Inline SVG components (no dependency) |
| Transpiler | Babel Standalone | UMD via CDN |
| API | FastAPI + uvicorn | 4 workers, port 8002 |
| Tile Server | Martin | v0.14.0, PMTiles proxy |
| Orchestrator | Apache Airflow | 2.9.3, LocalExecutor |
| Database | PostgreSQL + PostGIS | 16-3.4-alpine |
| Analytics | DuckDB Spatial | Embedded, ASOF JOIN |
| Storage | MinIO | S3A-compatible, 6 buckets |
| Pipeline | Python + GeoParquet | H3, tippecanoe, ProcessPoolExecutor |
| Output | MapLibre GL / Deck.gl / Matplotlib | Vector tiles, H3 hexagons, 4K PNG |

---

## 🔒 Security Architecture

- **Docker Secrets**: `postgres_dsn`, `minio_access_key`, `minio_secret_key` mounted at `/run/secrets/` — code reads files, not env vars. `lru_cache` reads once at startup.
- **PostgreSQL RLS**: 
  - `pipeline_own_records` — pipeline role owns its records
  - `readonly_select` — read-only role can select
  - `quarantine_insert_only` — pipeline can insert to quarantine
  - `quarantine_readonly` — read-only can view quarantine
  - Roles: `geo_pipeline_role`, `geo_readonly_role`
- **Rate Limiting**: `slowapi`
  - Tiles: 500/min
  - H3: 100/min
  - GeoJSON: 50/min
  - `X-Forwarded-For` trusted only from known proxy IPs
- **Bbox Validation**: Philippine envelope (lon 116-127°, lat 4.5-22°). Whitelist indicator pattern. Never pass raw user input to DuckDB query string.

---

## 🔄 End-to-End Data Movement

```
PSA/NAMRIA/COMELEC
    ↓ 7z shapefile archives
s3a://geo-uploads/
    ↓
MinioSensor (poll every 60s)
    ↓
profile_shapefile() → operation_mode
    ↓
Bronze: Immutable GeoParquet + Hilbert bbox index (ADR-012)
    → s3a://geo-bronze/
    ↓
Silver (Ephemeral): CRS normalize → make_valid → simplify z4/z8/z12
    → ProcessPoolExecutor, 4 workers, 300s timeout
    → s3a://geo-silver-tmp/ (deleted after Gold verification per ADR-011)
    ↓
Gold: H3 aggregate + Jenks NaturalBreaks (dynamic resolution 5/6/7)
    → Kimball ETL: fact_geo_observation + fact_geo_h3_aggregate
    → s3a://geo-gold/
    ↓
PMTiles: tippecanoe v2.67.0 (SHA-256 pinned)
    → --maximum-tile-bytes=500000
    → s3a://geo-pmtiles/ (national-scale <50MB)
    ↓
Martin Tile Server → HTTP range requests (512MB cache)
    ↓
Consumers:
    ├── MapLibre GL → Vector tiles (EconIntel Dashboard)
    ├── Deck.gl → H3 HexagonLayer animation (r/dataisbeautiful)
    └── Matplotlib → 4K PNG choropleth (r/MapPorn)
```

---

## 🎯 Output Channels

### 1. r/MapPorn — 4K PNG Choropleth
- Robinson projection
- Jenks NaturalBreaks classification (k=5)
- Clean legend, readable labels
- PSA + NAMRIA attribution

### 2. r/dataisbeautiful — Animated H3 Heatmap
- Deck.gl HexagonLayer
- Dynamic resolution 5/6/7
- Time-series fly-through
- OC flair + stack comment

### 3. EconIntel Dashboard — Live Vector Tiles
- MapLibre GL
- Sub-50ms p95 tile response
- HTTP range request proxy via Martin
- Optional enrichment edge
- Graceful degradation: NULL geo

---

## 🧠 Core Value Proposition: ASOF JOIN Cross-Domain Analytics

```sql
-- Poverty incidence × GDP growth × Boundary geometry
SELECT
    g.region_nk,
    g.region_name,
    g.vintage_year,
    g.poverty_incidence_pct,
    m.gdp_growth_pct,
    ABS(DATEDIFF('day', g.reference_date, m.reference_date)) AS macro_staleness_days
FROM geo_base g
ASOF JOIN macro_base m
    ON g.region_nk = m.region_nk
    AND m.reference_date <= g.reference_date
```

The cross-domain poverty-incidence × GDP-growth ASOF JOIN is the primary analytical value proposition. Both fact tables share conformed `dim_region` and `dim_date` dimensions, making the join possible without key remapping.

---

## 🚀 How to Use With Claude (or other LLMs)

### For Interactive Visualization
**Upload the `.html` file** to Claude. It can:
- Render it in a browser preview (if the environment supports HTML)
- Modify React components, add new stages, change styling
- Export modified versions
- The file is completely self-contained — only needs internet for CDN resources (React, Framer Motion, Tailwind, Babel)

### For Text/Code Analysis
**Paste this `.md` file** to Claude. It can:
- Analyze architecture decisions and suggest improvements
- Generate SQL/DDL from entity definitions
- Create Python ETL scripts from pipeline task definitions
- Generate documentation, runbooks, or ADR templates
- Compare against other system architectures
- Identify security gaps or SLO blind spots

### Example Prompts for Claude
> "Generate a Python ETL script for the Gold stage using H3 aggregation logic from ADR-009"

> "Create PostgreSQL DDL for the dim_region SCD Type 2 table with all fields and constraints"

> "Review ADR-007 and suggest alternatives to ProcessPoolExecutor for GEOS isolation"

> "Design a monitoring dashboard for the 5 SLOs defined in this system"

> "Convert the pipeline DAG into a GitHub Actions workflow for CI/CD"

---

## 🔌 BYOD — Bring Your Own Data (v2.2)

The platform is fully data-agnostic as of v2.2. Any user with PSA/NAMRIA-compatible `.7z` geospatial archives, or any standard shapefile/GeoPackage dataset, can run the full pipeline without modifying Python code.

### BYOD Setup (3 steps)
```bash
git clone https://github.com/raldisk/philippines-geospatial-platform
cp .env.example .env          # configure credentials + bucket names
# upload .7z archive to MinIO geo-uploads bucket
# trigger Airflow DAG or run scripts/run_day1.py manually
```

### Key BYOD Additions (v2.2)
| File | Purpose |
|------|---------|
| `geo_service/pipeline/extract/archive.py` | `find_new_archives()` + `extract_archive()` — recursive `.shp` discovery, skip logic for non-geospatial files, multi-shapefile support, `LAYER_FILTER` env var |
| `.env.example` | 30+ environment variables documented with defaults and descriptions |
| `docs/BYOD_GUIDE.md` | Format support, column mapping, indicator registration, first-run checklist, troubleshooting table |

### Configurable via Environment Variables
- **Buckets:** `GEO_UPLOAD_BUCKET`, `BRONZE_BUCKET`, `SILVER_TEMP_BUCKET`, `GOLD_BUCKET`
- **CRS:** `TARGET_CRS` (default EPSG:4326) + missing `.prj` fallback
- **Column mapping:** `REGION_COLUMN_MAP` (JSON env var)
- **Workers:** `SILVER_MAX_WORKERS`, `SILVER_MEMORY_LIMIT_MB`
- **Bbox:** `TARGET_BBOX_MINX/MINY/MAXX/MAXY` (default: Philippine bounding box)
- **Rate limits:** `RATE_LIMIT_H3`, `RATE_LIMIT_GEOJSON`
- **H3 resolutions:** `VALID_H3_RESOLUTIONS`

### Observability Additions
- `INGESTION_RUNS` Prometheus counter with `archive_name`, `status`, `vintage` labels
- All config reads via `os.getenv()` — no hardcoded paths, buckets, or region names in source

### Quarantine Behaviour
Unknown indicators and failed silver partitions are quarantined with `QuarantineEntry(archive_path=..., quarantined_at=<UTC ISO8601>, reason_code=...)` rather than crashing the pipeline. RUNBOOK Scenario 6 documents the indicator registration workflow.

---



- System Design v2.2 (blueprint — P2 authority)
- Master Plan v2.2 Final (workflow — P4 authority)
- Corrected Design v2.2.1 (diagram corrections — P3)
- Release Package: ph-geospatial-platform-byod-ready.zip (P1 — current implementation truth)
- **12 ADRs** | **10 Pipeline Tasks** | **6 Services** | **5 SLOs** | **10 Entities**

---

*Generated for cross-LLM compatibility. All data inline. No external API dependencies.*
*v2.2 BYOD update: 2026-06-08 — C-06 ranking resolved, BYOD section added, deploy gate count corrected (46→49).*
