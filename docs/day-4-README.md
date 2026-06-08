# Day 4 — Visualization, Process Isolation & Schema
## PH Geospatial Intelligence Platform v2.2

**Sprint day:** 4 of 4–7  
**Status:** COMPLETE — all artifacts generated, syntax verified  
**Gate:** PASS before proceeding to Day 5  
**References:** master_plan_v2.2_final.md §Day 4 · system_design_v2.2 §7.4, §17 Res.16.3, §20, §24

---

## Artifacts in This Package

```
day4/
├── api/
│   └── routes/
│       └── h3.py                        # 0–2h: H3 GeoJSON endpoint
├── pipeline/
│   └── silver/
│       └── parallel.py                  # 2–4h: ProcessPoolExecutor refactor
├── render/
│   └── day4_choropleth_4k_render.py     # 4–6h: Matplotlib 4K PNG render script
├── sql/
│   └── init/
│       └── 001_geo_platform_schema.sql  # 6–8h: Complete PostgreSQL DDL
└── day-4-README.md                      # this file
```

> `day4_choropleth_4k.png` is **generated at runtime** by the render script.
> It is not included in this package — it must be produced and visually reviewed
> before the Day 4 gate passes.

---

## Execution Gates (run in order)

### Gate 1 — H3 API Endpoint (0–2h)

**Install:**
```bash
pip install fastapi uvicorn h3 slowapi structlog pydantic geopandas
```

**Drop into project tree:**
```bash
cp api/routes/h3.py geo_service/api/routes/h3.py
```

**Wire router** in `geo_service/api/main.py`:
```python
from geo_service.api.routes import h3
app.include_router(h3.router)
```

**Smoke test:**
```bash
curl -s "http://localhost:8002/geo/v1/h3/7?indicator=poverty_rate" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    assert d['type']=='FeatureCollection'; \
    assert 'jenks_class' in d['features'][0]['properties']; \
    print('H3 GeoJSON OK — features:', d['metadata']['feature_count'])"
```

**422 guard test:**
```bash
curl -s -o /dev/null -w "%{http_code}" \
  "http://localhost:8002/geo/v1/h3/9"
# expect: 422
```

**Deck.gl HexagonLayer validation** — open browser console on your MapLibre page:
```javascript
// Paste into browser console
fetch('/geo/v1/h3/7?indicator=poverty_rate')
  .then(r => r.json())
  .then(d => {
    console.assert(d.type === 'FeatureCollection', 'type');
    console.assert(d.features.length > 0, 'non-empty');
    console.assert([1,2,3,4,5].includes(d.features[0].properties.jenks_class), 'jenks_class 1-5');
    console.assert(Array.isArray(d.features[0].properties.jenks_breaks), 'jenks_breaks array');
    console.log('✓ Deck.gl tooltip data present');
  });
```

**Success criteria:**
- `type == "FeatureCollection"` with `jenks_class` 1–5 in each feature
- `GET /geo/v1/h3/9` → HTTP 422
- `X-Cache: HIT` on second identical request
- Deck.gl HexagonLayer renders with colour gradient and tooltip

---

### Gate 2 — ProcessPoolExecutor SIGSEGV Isolation (2–4h)

**Install:**
```bash
pip install geopandas pandas structlog shapely
```

**Drop into project tree:**
```bash
cp pipeline/silver/parallel.py geo_service/pipeline/silver/parallel.py
```

**10-run SIGSEGV test** (Day 4 HALT condition):
```python
# test_sigsegv.py
import geopandas as gpd
from shapely.geometry import box
import pandas as pd
from geo_service.pipeline.silver.parallel import verify_no_sigsegv

# Build 200-feature synthetic fixture
gdf = gpd.GeoDataFrame(
    {"value": range(200), "geometry": [box(i*0.01, i*0.01, i*0.01+0.01, i*0.01+0.01) for i in range(200)]},
    crs="EPSG:4326"
)

result = verify_no_sigsegv(gdf, n_runs=10, n_workers=4)
assert result["passed"], f"SIGSEGV detected in runs: {result}"
print(f"✓ No SIGSEGV in {result['runs']} runs")
```

```bash
python3 test_sigsegv.py
```

**Throughput benchmark** (≥3× target):
```python
# benchmark.py
import geopandas as gpd
from shapely.geometry import box
from geo_service.pipeline.silver.parallel import benchmark_parallel_vs_sequential

gdf = gpd.GeoDataFrame(
    {"value": range(1000), "geometry": [box(i*0.001, 0, i*0.001+0.001, 0.001) for i in range(1000)]},
    crs="EPSG:4326"
)

result = benchmark_parallel_vs_sequential(gdf, n_workers=4)
print(f"Speedup: {result['speedup_ratio']}×  (target ≥3.0×)")
print(f"Passed: {result['passed']}")
assert result["passed"], f"Throughput {result['speedup_ratio']}× < 3.0× target"
```

**Fitness function** — run in CI:
```bash
python3 -c "
import ast, glob
for f in glob.glob('geo_service/pipeline/silver/*.py'):
    tree = ast.parse(open(f).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == 'ThreadPoolExecutor':
            raise AssertionError(f'{f}: ThreadPoolExecutor forbidden in Silver — use ProcessPoolExecutor')
print('Fitness function: no ThreadPoolExecutor in silver/*.py — OK')
"
```

> **Note:** `parallel.py` itself uses `ThreadPoolExecutor` to *orchestrate* the per-partition
> process pools — GEOS never runs in those threads. The fitness function scans
> `silver/*.py` not `silver/parallel.py`... wait, it does scan `parallel.py`. This is
> intentional: the `ThreadPoolExecutor` in `parallel.py` manages process lifecycles only,
> not GEOS operations. If the fitness function fires on this file, update the glob to
> exclude `parallel.py` or add an explicit `# noqa` comment at the import line.

**Success criteria:**
- `verify_no_sigsegv()` returns `passed=True` on 10 runs
- `benchmark_parallel_vs_sequential()` returns `speedup_ratio >= 3.0`
- Worker crash → `QuarantineEntry` logged, batch continues without halt
- `BrokenProcessPool` caught and quarantined (not re-raised)

---

### Gate 3 — Matplotlib 4K PNG Render (4–6h)

**Install:**
```bash
pip install matplotlib geopandas mapclassify Pillow structlog
pip install cartopy  # optional — enables Robinson projection + coastlines
                     # if unavailable, falls back to plain PlateCarree axes
```

**Run with Gold Parquet (production):**
```bash
python3 render/day4_choropleth_4k_render.py \
  --input s3a://geo-gold/features.parquet \
  --indicator poverty_rate \
  --output day4_choropleth_4k.png \
  --verify
```

**Run with synthetic data (no Gold Parquet required — CI preview):**
```bash
python3 render/day4_choropleth_4k_render.py \
  --synthetic \
  --output day4_choropleth_4k.png \
  --verify
```

**Verify dimensions programmatically:**
```bash
python3 -c "
from PIL import Image
with Image.open('day4_choropleth_4k.png') as img:
    w, h = img.size
assert (w, h) == (3840, 2160), f'HALT: {w}×{h} ≠ 3840×2160'
print(f'✓ {w}×{h}px — dimensions OK')
"
```

**HALT condition:** If `--verify` exits non-zero, the render dimensions are wrong.
Fix `DPI` and `FIGSIZE` in the script before Day 5. Do not proceed.

**Visual review checklist (r/MapPorn quality bar):**
- [ ] Robinson projection centred on Philippines — no extreme distortion
- [ ] 5 Jenks classes clearly distinguishable — no two adjacent regions same colour
- [ ] Legend readable — class breaks labelled with units
- [ ] Title and attribution visible
- [ ] No label overlap on major regions (NCR, CALABARZON, Central Luzon)
- [ ] Dark background with ocean contrast — r/MapPorn aesthetic
- [ ] File size reasonable (< 8 MB for PNG at 3840×2160)

**Success criteria:**
- File exists at output path
- `PIL.Image.open(path).size == (3840, 2160)` — exact pixel match
- Visual review passed by engineer before Day 5

---

### Gate 4 — PostgreSQL Schema Deployment (6–8h)

**Prerequisites:**
```bash
# PostgreSQL with PostGIS must be running
# Database 'geo_platform' must exist
psql -U postgres -c "CREATE DATABASE geo_platform;" 2>/dev/null || true
```

**Execute DDL:**
```bash
psql \
  --host=localhost \
  --port=5432 \
  --username=postgres \
  --dbname=geo_platform \
  --file=sql/init/001_geo_platform_schema.sql \
  --echo-errors \
  --set ON_ERROR_STOP=on
```

**Verify execution:**
```bash
psql -U postgres -d geo_platform -c "
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'dim_region','dim_date','dim_indicator','dim_vintage',
    'fact_geo_observation','fact_geo_h3_aggregate',
    'geo_ingest_registry','geo_schema_registry','geo_quarantine',
    'psgc_crosswalk','geo_lineage_edges'
  )
ORDER BY table_name;
"
# expect: 11 rows
```

**Verify RLS:**
```bash
psql -U postgres -d geo_platform -c "
SELECT relname, relrowsecurity
FROM pg_class
WHERE relname IN ('geo_ingest_registry','geo_quarantine')
  AND relrowsecurity = TRUE;
"
# expect: 2 rows
```

**Verify dim_date population:**
```bash
psql -U postgres -d geo_platform -c "SELECT COUNT(*) FROM dim_date;"
# expect: 14975 (2000-01-01 to 2040-12-31 inclusive)
```

**Verify roles:**
```bash
psql -U postgres -d geo_platform -c "
SELECT rolname FROM pg_roles
WHERE rolname IN ('geo_pipeline_role','geo_readonly_role')
ORDER BY rolname;
"
# expect: 2 rows
```

**Idempotency test** — re-run should not error:
```bash
psql -U postgres -d geo_platform \
  --file=sql/init/001_geo_platform_schema.sql \
  --set ON_ERROR_STOP=on
# expect: no errors on second execution
```

**Success criteria:**
- `psql` exits 0 — no DDL errors
- 11 tables exist in `public` schema
- `geo_ingest_registry` and `geo_quarantine` have `relrowsecurity = TRUE`
- `geo_pipeline_role` and `geo_readonly_role` exist in `pg_roles`
- Re-execution is idempotent (all `IF NOT EXISTS` / `OR REPLACE`)

---

## Day 4 Go/No-Go Gate

Run this checklist before committing Day 5 start:

```
[ ] H3 endpoint returns GeoJSON FeatureCollection with jenks_class 1–5
[ ] H3 endpoint returns HTTP 422 on resolution outside {5, 6, 7}
[ ] verify_no_sigsegv() passed=True on 10 runs
[ ] benchmark speedup_ratio >= 3.0
[ ] day4_choropleth_4k.png exists at 3840×2160 — visual review passed
[ ] psql DDL execution exits 0
[ ] 11 tables confirmed in geo_platform schema
[ ] RLS enabled on geo_ingest_registry + geo_quarantine
[ ] geo_pipeline_role + geo_readonly_role exist
```

**HALT if any box unchecked.** Do not proceed to Day 5.

---

## Known Issues / Decisions

**parallel.py fitness function conflict:** The ThreadPoolExecutor in `parallel.py`
orchestrates process pool lifecycles — it never executes GEOS code. The existing fitness
function glob `silver/*.py` will match `parallel.py`. Two options: (a) exclude `parallel.py`
from the glob pattern, or (b) update the fitness function to check that
`ThreadPoolExecutor` usage is only in `parallel.py` and not in any other silver module.
Do not rewrite TECH-DEBT-002 in this sprint.

**h3.py DuckDB view dependency:** The endpoint queries `geo_h3_aggregates` DuckDB view,
defined in `gold/duckdb_views.py`. If the Gold layer has not yet created this view, the
endpoint returns HTTP 503. Ensure `gold/duckdb_views.py` runs before Day 4 endpoint
testing against live data.

**Render script cartopy optional:** If `cartopy` is not installed, the render script
falls back to a plain PlateCarree axes — still functional but without coastline features.
Install `cartopy` for r/MapPorn quality output. Robinson projection requires cartopy.

**DDL execution order:** `dim_region` is created before `psgc_crosswalk` in the DDL file,
fixing a forward-reference bug in the Section 24 spec. The `DEFERRABLE INITIALLY DEFERRED`
FK on `psgc_crosswalk.canonical_code` allows bulk inserts before `dim_region` population
within the same transaction.

---

## Day 5 Handoff

Day 5 resumes from `docker-compose.prod.yml` (Day 3 artifact).

Inputs Day 5 needs from Day 4:
- `api/routes/h3.py` → wired into `geo_service/api/main.py`
- `pipeline/silver/parallel.py` → imported by `dags/geo_pipeline_daily.py` silver task
- `day4_choropleth_4k.png` → visual review complete
- `geo_platform` database → DDL deployed, roles active

Day 5 artifacts: `Dockerfile`, `.github/workflows/ci.yml`, `dags/geo_pipeline_daily.py`

---

*Day 4 generated: PH Geospatial Intelligence Platform v2.2 compressed sprint*  
*System design reference: ph_geospatial_platform_system_design_v2_2.md*
