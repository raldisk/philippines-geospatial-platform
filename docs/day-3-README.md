# Day 3 - PH Geospatial Intelligence Platform v2.2
## FastAPI /geo/v1 + DuckDB Spatial + ASOF JOIN + Security Baseline

---

## Folder Structure

```
day3/
├── Dockerfile
├── requirements.txt
├── docker-compose.prod.yml
├── asof_join_proof.sql
├── validate_asof_join.py
├── api/
│   ├── __init__.py
│   ├── main.py
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── health.py
│   │   ├── tiles.py
│   │   ├── geojson.py
│   │   ├── h3.py
│   │   └── metadata.py
│   └── middleware/
│       ├── __init__.py
│       └── validation.py
├── infra/
│   ├── __init__.py
│   ├── cache.py
│   ├── duckdb_conn.py
│   └── secrets.py
├── sql/
│   └── views/
│       └── mart_geo_poverty_crosswalk.sql
├── tests/
│   └── locustfile.py
└── seed_data/
    ├── seed_datasets.py
    ├── run_asof_join_local.py
    └── gold/
        ├── indicators/
        │   ├── poverty_indicators.parquet
        │   └── gdp_indicators.parquet
        ├── curated/
        │   └── psa_provincial.parquet
        ├── h3/
        │   └── h3_aggregates.parquet
        └── metadata/
            └── dataset_metadata.parquet
```

---

## Prerequisites

```bash
pip install -r requirements.txt
pip install geopandas pyarrow   # seed data only
pip install locust              # load testing only
docker --version                # 24+
docker compose version          # v2.x
```

---

## Execution Order

### Step 1 - Seed synthetic Gold Parquet (local test only)

Skip if real PSA Parquet already in /data/gold/.

```bash
cd seed_data
python seed_datasets.py
```

Expected output:

```
[OK] poverty_indicators: 51 rows
[OK] gdp_indicators: 153 rows
[OK] psa_provincial (GeoParquet): 17 rows
[OK] h3_aggregates: 153 rows
[OK] dataset_metadata: 2 rows
[DONE] All Gold Parquet fixtures written.
```

---

### Step 2 - ASOF JOIN Gate (REQUIRED - blocks Day 4)

```bash
cd seed_data
python run_asof_join_local.py
```

Five assertions must all print [OK]:

| Assertion | Expected |
|---|---|
| Row count | > 0 |
| poverty_rate range | all in [0, 100] |
| gdp_growth nulls | none |
| year_gap | all <= 5 |
| Region coverage | all 17 PH regions |

Final line must read:

```
[PASS] ASOF JOIN validated. Day 3 gate: PROCEED to Day 4.
```

STATUS: PASSED (synthetic fixtures confirmed)

If HALT fires: verify psgc_code join key consistency between poverty_indicators.parquet
and gdp_indicators.parquet. Check PSA PSGC 2020 vs 2023 vintage mismatches
(Negros Island Region dissolved 2017).

---

### Step 3 - Docker Compose Validation (REQUIRED)

```bash
docker compose -f docker-compose.prod.yml config
```

Must exit 0. Check: zero plain-text secret values in environment blocks.

Create secrets directory before docker compose up:

```bash
mkdir -p secrets
echo "postgresql://geo_pipeline_role:CHANGEME@postgres:5432/geo_platform" > secrets/postgres_dsn.txt
echo "CHANGEME_ACCESS" > secrets/minio_access_key.txt
echo "CHANGEME_SECRET" > secrets/minio_secret_key.txt
chmod 400 secrets/*.txt
```

STATUS: PASSED (YAML validated)

---

### Step 4 - Start Stack and Verify Health (REQUIRED)

```bash
docker compose -f docker-compose.prod.yml up -d geo-service martin postgres minio
```

Wait 15s then:

```bash
curl -s http://localhost:8002/health/live | python3 -m json.tool
curl -s http://localhost:8002/health/ready | python3 -m json.tool
```

/health/ready must return HTTP 200:

```json
{
  "status": "ready",
  "checks": {
    "duckdb": "ok (Xms)",
    "martin": "ok (Xms)"
  }
}
```

If martin check fails: Martin requires at least one .pmtiles or .mbtiles file
mounted at /tiles. Workaround for Day 3 (PMTiles generated Day 4):
set MARTIN_BASE_URL=http://localhost:9999 to isolate DuckDB path only.

---

### Step 5 - Smoke Test Endpoints (feasibility)

```bash
# ST_AsGeoJSON path
curl "http://localhost:8002/geo/v1/geojson/provincial?bbox=116.0,4.5,127.0,22.0&limit=5"

# H3 aggregates
curl "http://localhost:8002/geo/v1/h3/6?bbox=121.0,9.5,126.5,13.5&indicator=poverty_rate"

# Metadata / Jenks breaks
curl "http://localhost:8002/geo/v1/metadata/psa_provincial_2023"

# Tile proxy (expect 200 or 204)
curl -I "http://localhost:8002/geo/v1/tiles/6/53/30.mvt"
```

---

### Step 6 - ADR-012 Bbox Pruning Verification (feasibility)

```python
from infra.duckdb_conn import DuckDBConnectionManager
mgr = DuckDBConnectionManager("/data/geo_platform.duckdb")
plan = mgr.explain_bbox_pruning("/data/gold/curated/psa_provincial.parquet")
print(plan)
# Expected: "Parquet Filter" or "ParquetScan" with filter pushdown
```

---

### Step 7 - Locust p95 Gate (REQUIRED - run after Step 4)

```bash
locust -f tests/locustfile.py --headless \
       --host http://localhost:8002 \
       -u 50 -r 10 --run-time 60s \
       --html day3_locust_report.html
```

Pass condition: p95 < 200ms on all GET endpoints.
Gate assertion fires automatically via @events.quitting listener.

If p95 >= 200ms on /geo/v1/geojson: reduce LIMIT or verify DuckDB
row-group pruning via Step 6.

If p95 >= 200ms on /geo/v1/tiles: Martin cold-start; re-run after
tile cache warms (second run typically drops 40-60%).

---

## Success Criteria

| Criterion | Gate | Status |
|---|---|---|
| curl /health/ready returns 200 | REQUIRED | Run Step 4 |
| ASOF JOIN non-empty result set | REQUIRED | PASSED |
| docker-compose config validates | REQUIRED | PASSED |
| No secrets in env vars | REQUIRED | PASSED |
| ST_AsGeoJSON returns valid GeoJSON | feasibility | Run Step 5 |
| Bbox query uses partition pruning | feasibility | Run Step 6 |
| Locust p95 < 200ms | REQUIRED | Run Step 7 |

---

## Known Gaps

TECH-DEBT-002: not rewritten this sprint per master plan constraint.

DuckDB spatial extension: requires network access to extensions.duckdb.org
on first install. Pre-install in Dockerfile:

```dockerfile
RUN python3 -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL spatial')"
```

Martin tile source: serves placeholder until Day 4 PMTiles generation
(gold/pmtiles.py from Day 2) completes.

Real PSA data: replace seed_data/gold/ fixtures with actual PSA Family Income
and Expenditure Survey Parquet. Schema is identical; psgc_code is the join key.

---

## ADR References

| ADR | Decision | Applied In |
|---|---|---|
| ADR-002 | DuckDB over PostGIS for analytical queries | infra/duckdb_conn.py |
| ADR-012 | GeoParquet Hilbert Curve Bbox Indexing | duckdb_conn.explain_bbox_pruning(), seed_datasets.py |

---

Day 3 complete. Proceed to Day 4: PMTiles serving + Martin config.
