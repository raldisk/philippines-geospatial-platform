# Day 6 Reddit Post Templates

## 1. r/MapPorn Post

### Title (pick one based on which indicator is highest-variance in Day 1 data)

**Option A — Poverty incidence:**
```
Philippine Provincial Poverty Incidence — Choropleth Map [OC]
Jenks natural breaks · Robinson projection · PSA 2021 data · 4K
```

**Option B — If geometry-only mode (no numeric attribute):**
```
Philippine Administrative Boundaries — All Provinces & Municipalities [OC]
NAMRIA · Robinson projection · Vector tiles generated with tippecanoe PMTiles · 4K
```

**Option C — Generic fallback:**
```
Philippines by Province — Spatial Distribution [OC] [4K]
Open government data · PSA + NAMRIA · Built with Python, DuckDB spatial, H3
```

**Rule:** Title must include [OC] flair. Keep under 300 characters.
Post time: Sunday 8:00 AM EST (Manila time: Sunday 9:00 PM PHT).

---

### Methodology Comment (post within 10 minutes of submission)

```
**Methodology & Stack**

Built as part of a production-grade geospatial data engineering platform 
ingesting Philippine government shapefiles through a Bronze→Silver→Gold 
medallion lakehouse.

**Data sources:**
- Boundaries: NAMRIA administrative shapefiles (EPSG:4326, reprojected from source)
- Statistical indicators: PSA (Philippine Statistics Authority) census/survey releases
- PSGC normalization via crosswalk table covering 2015–2023 vintage transitions

**Pipeline:**
1. SHA-256 dedup registry gates re-ingestion of unchanged archives
2. Geometry repair: shapely.make_valid() → PostGIS ST_MakeValid for regulatory datasets
3. Three zoom-level simplification (z4/z8/z12) via GDAL — topology preserved
4. H3 hexagonal aggregation (dynamic resolution per dataset occupancy, Uber H3)
5. Jenks natural breaks classification computed at Gold generation time
6. tippecanoe → PMTiles (HTTP range requests, CDN-compatible, no tile server needed)

**Visualization:**
- Matplotlib · Robinson projection · 3840×2160 PNG
- Color ramp: [insert your ramp — e.g., YlOrRd / diverging RdBu / sequential Blues]
- Legend: Jenks class breaks with exact value boundaries

**Stack:**
Python · GeoPandas · DuckDB spatial · Uber H3 · Jenks · FastAPI · 
Deck.gl · tippecanoe PMTiles · Apache Airflow · PostgreSQL + PostGIS

GitHub: [insert repo URL]

Data is publicly released by Philippine government agencies. 
Not affiliated with PSA or NAMRIA.
```

---

## 2. r/dataisbeautiful Post

### Title

**Option A — Time-series (if multi-vintage data available):**
```
Philippine Poverty Incidence by Province, 2015–2021 — Animated Hex Grid [OC]
Jenks natural breaks · H3 resolution 5 · PSA data · Built with DuckDB spatial + Deck.gl
```

**Option B — Single-vintage fly-through:**
```
Flying Over the Philippines — Province-Level Data Visualization [OC]
H3 hexagonal grid · Jenks natural breaks · Deck.gl + tippecanoe PMTiles
```

**Rule:** Must include [OC] flair. r/dataisbeautiful requires OC = Original Content.

### Post Body

```
**[OC] Philippine Geospatial Data Visualization**

[brief 1–2 sentence description of what the animation shows]

**What you're seeing:**
- Each hexagon is an H3 cell at resolution [5/6/7 — insert actual]
- Color = [indicator name] classified by Jenks natural breaks ([N] classes)
- Animation: [time-series across vintages / camera fly-through over provinces]

**Why H3 hexagons?**
Philippine administrative units range from tiny Pateros (2.4 km²) to 
massive Palawan (14,896 km²). Choropleth maps visually overweight large-area 
low-population provinces. H3 normalizes spatial variance — each hexagon 
represents equal area, making the distribution fairer to interpret.
```

### Stack Comment (post immediately after — this is the portfolio signal)

```
**Full stack for those who want to replicate:**

Data pipeline:
- Python + GeoPandas: shapefile ingestion, CRS normalization, geometry repair
- DuckDB spatial extension: ASOF JOIN linking poverty incidence × GDP growth 
  across conformed dim_region dimension (Kimball dimensional model)
- Uber H3: hexagonal aggregation with dynamic resolution selection per dataset
- Jenks natural breaks: classification computed at pipeline time, not render time
- tippecanoe: PMTiles generation (HTTP range requests, served from MinIO S3A)

Serving:
- FastAPI /geo/v1: tile, H3 GeoJSON, and metadata endpoints
- Martin tile server v0.14.0: PMTiles serving with 512mb memory cache
- Rate limiting: 500/min tiles, 100/min H3 (slowapi)

Orchestration:
- Apache Airflow 2.9.3: MinioSensor → Bronze → ephemeral Silver → Gold → PMTiles
- 46-check deploy gate before any tile is published
- GitHub Actions CI: lint · unit · integration (Bronze→MinIO roundtrip) · Trivy scan

Visualization:
- Deck.gl HexagonLayer: H3 cell rendering with Jenks color mapping
- Animation: [time-series interpolation / camera fly-through via ViewStateTransition]
- Screen recording: [OBS Studio / ffmpeg] → ffmpeg post-process → MP4/GIF < 50MB

Architecture: Bronze→Silver(ephemeral)→Gold medallion lakehouse on MinIO S3A.
GeoParquet with Hilbert curve bbox indexing (write_covering_bbox=True) → ~10× 
DuckDB regional query speedup via row-group pruning.

Data: PSA + NAMRIA (Philippine government open data). Not affiliated.
GitHub: [insert repo URL]
```
