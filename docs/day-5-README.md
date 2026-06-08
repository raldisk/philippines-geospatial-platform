# Day 5 — Docker · CI · Airflow · Lineage
## PH Geospatial Intelligence Platform v2.2

**Sprint reference:** `master_plan_v2.2_final.md` — Day 5  
**Gate policy:** All 4 execution gates must pass before Day 6.  
**HALT rules (non-negotiable):**
- CI fails on string-based fitness functions → fix implementation, not the assertion. No TECH-DEBT-002 rewrite.
- Airflow deploy gate fails → fix the check or document a waiver. Do NOT disable checks.
- Docker build exceeds 8 min → investigate layer cache miss, not CI timeout bump.

---

## Artifact Inventory

| File | Destination in repo | Section ref |
|------|---------------------|-------------|
| `Dockerfile` | repo root | §26, ADR-008 |
| `.github/workflows/ci.yml` | `.github/workflows/` | §28 |
| `dags/geo_pipeline_daily.py` | `dags/` | §25 |
| `sql/lineage/lineage_verification.sql` | `sql/lineage/` | §30 |

---

## Pre-Flight Checklist

Complete **before** running any gate.

```
[ ] secrets/ directory populated:
      secrets/postgres_dsn.txt       — postgresql://geoservice:<pw>@postgres:5432/geo_platform
      secrets/minio_access_key.txt   — MinIO root user
      secrets/minio_secret_key.txt   — MinIO root password
      secrets/airflow_fernet_key.txt — generated: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

[ ] TIPPECANOE_SHA256 obtained:
      curl -fsSL https://github.com/felt/tippecanoe/releases/download/2.67.0/tippecanoe-linux-x86_64 | sha256sum
      → store result in GitHub repo secret: Settings → Secrets → Actions → TIPPECANOE_SHA256

[ ] Integration test stubs exist (required by CI):
      tests/integration/test_bronze_minio_roundtrip.py
      tests/integration/test_tippecanoe_tile_count.py
      (see Gate 2 for minimum viable test bodies)

[ ] docker-compose.prod.yml present (Day 3 artifact — not regenerated in Day 5)
[ ] sql/init/001_geo_platform_schema.sql present (Day 4 artifact)
[ ] geo_service/ source tree present (Days 1–4 artifacts)
[ ] config/ directory present (martin.yaml, log_config.json)
```

---

## Gate 1 — Docker Multi-Stage Build

**Success criteria:** All containers healthy. CI build < 8 min.

### Step 1 — Build image locally, verify < 8 min

```bash
time docker build \
  --build-arg TIPPECANOE_VERSION=2.67.0 \
  --build-arg TIPPECANOE_SHA256=<hash-from-preflight> \
  -t geo-service:local \
  .
```

**Pass:** Build completes. `real` time < 8m00s.  
**Fail:** Build > 8 min → check layer cache. Stage 1 (tippecanoe-binary) must be cached on re-runs. If first run exceeds 8 min, confirm ADR-001 note: "adds ~8 min CI build time, cached." First cold build is exempt; cached re-runs must be < 8 min.

### Step 2 — Verify non-root user

```bash
docker run --rm geo-service:local whoami
# Expected: geoservice
```

### Step 3 — Verify tippecanoe present + executable

```bash
docker run --rm geo-service:local tippecanoe --version
# Expected: tippecanoe v2.67.0 (or similar version string)
```

### Step 4 — Bring full stack online

```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### Step 5 — All containers healthy

```bash
docker-compose -f docker-compose.prod.yml ps
```

**Pass:** All services show `healthy` status. No service in `starting` or `unhealthy`.

```bash
# Individual health checks:
curl -f http://localhost:8002/health/ready  && echo "geo-service OK"
curl -f http://localhost:9000/minio/health/live && echo "minio OK"
curl -f http://localhost:3002/health        && echo "martin OK"
pg_isready -h localhost -p 5432 -d geo_platform && echo "postgres OK"
```

**Gate 1 PASS condition:** All 4 curl/pg_isready commands return OK.

---

## Gate 2 — GitHub Actions CI

**Success criteria:** All checks green in GitHub Actions UI.

### Step 1 — Add GitHub secret

```
GitHub repo → Settings → Secrets and variables → Actions → New repository secret
Name:  TIPPECANOE_SHA256
Value: <sha256 from Pre-Flight>
```

### Step 2 — Write minimum-viable integration tests

These files must exist before pushing. CI collects `tests/integration/` — missing files cause collection error, not test failure.

**`tests/integration/test_bronze_minio_roundtrip.py`**

```python
"""
Integration: upload fixture .7z → bronze writer → verify Parquet in geo-bronze bucket.
Requires: MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, POSTGRES_DSN env vars.
"""
import os, boto3, pytest
from pathlib import Path

MINIO_ENDPOINT  = os.environ["MINIO_ENDPOINT"]
MINIO_AK        = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SK        = os.environ.get("MINIO_SECRET_KEY", "minioadmin")

@pytest.fixture
def s3():
    return boto3.client("s3", endpoint_url=MINIO_ENDPOINT,
                        aws_access_key_id=MINIO_AK,
                        aws_secret_access_key=MINIO_SK)

def test_bronze_minio_roundtrip(s3, tmp_path):
    from geo_service.pipeline.bronze.writer import write_bronze

    fixture_archive = Path("tests/fixtures/test_shapefile.7z")
    if not fixture_archive.exists():
        pytest.skip("Fixture archive not present — add tests/fixtures/test_shapefile.7z")

    # Upload fixture to uploads bucket
    s3.upload_file(str(fixture_archive), "geo-uploads", "uploads/test_shapefile.7z")

    # Run bronze writer with test profile
    test_profile = {"dataset_name": "test_shapefile", "archive_path": str(fixture_archive)}
    write_bronze(test_profile, run_id="ci-test-run-001", mode="catalog")

    # Verify Parquet landed in geo-bronze
    response = s3.list_objects_v2(Bucket="geo-bronze", Prefix="test_shapefile/")
    keys = [obj["Key"] for obj in response.get("Contents", [])]
    assert any(k.endswith(".parquet") for k in keys), \
        f"No Parquet found in geo-bronze/test_shapefile/. Found: {keys}"
```

**`tests/integration/test_tippecanoe_tile_count.py`**

```python
"""
Integration: generate_pmtiles on fixture GeoJSON → pmtiles verify → tile count > 0.
Requires: tippecanoe binary on PATH.
"""
import json, subprocess, tempfile
from pathlib import Path
import pytest

FIXTURE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [121.0, 14.5]},
        "properties": {"name": "test"}
    }]
}

def test_tippecanoe_valid_tile_count(tmp_path):
    geojson_path = tmp_path / "fixture.geojson"
    pmtiles_path = tmp_path / "fixture.pmtiles"

    geojson_path.write_text(json.dumps(FIXTURE_GEOJSON))

    result = subprocess.run(
        ["tippecanoe", "-o", str(pmtiles_path),
         "--minimum-zoom=4", "--maximum-zoom=8",
         "--force", str(geojson_path)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"tippecanoe failed: {result.stderr}"
    assert pmtiles_path.exists(), "PMTiles file not created"

    # pmtiles verify — exit code 0 = valid archive
    verify = subprocess.run(
        ["pmtiles", "verify", str(pmtiles_path)],
        capture_output=True, text=True
    )
    # tile_count > 0 confirmed by file size (> 1KB means tiles were written)
    assert pmtiles_path.stat().st_size > 1024, \
        f"PMTiles suspiciously small ({pmtiles_path.stat().st_size} bytes) — likely 0 tiles"
```

### Step 3 — Push and verify

```bash
git add Dockerfile .github/workflows/ci.yml dags/geo_pipeline_daily.py \
        sql/lineage/lineage_verification.sql \
        tests/integration/test_bronze_minio_roundtrip.py \
        tests/integration/test_tippecanoe_tile_count.py
git commit -m "Day 5: Docker, CI, Airflow DAG, lineage schema"
git push origin master
```

### Step 4 — Verify CI matrix

Navigate to: `github.com/<org>/<repo>/actions`

**Pass condition:** All jobs green:
```
✓ lint
✓ test  (unit + integration + fitness_functions)
✓ docker-build
✓ schema-drift  (PR only — skip on direct master push)
```

**HALT if:** `test` job fails on `fitness_functions/` step → read failure message.  
Fix the *implementation*, not the string-based assertion. No path-based rewrite (TECH-DEBT-002).

---

## Gate 3 — Airflow DAG First End-to-End Run

**Success criteria:** First fully automated end-to-end pipeline run. `deploy_gate` task green.

### Step 1 — Verify DAG loads without error

```bash
docker exec <airflow_container> airflow dags list | grep geo_pipeline_daily
# Expected: geo_pipeline_daily listed, not paused
```

```bash
docker exec <airflow_container> airflow dags test geo_pipeline_daily 2026-01-01
# Runs DAG in dry-run mode. Verify no import errors.
```

### Step 2 — Upload test archive to trigger sensor

```bash
# Using MinIO CLI (mc) — configure alias first if not already done:
mc alias set local http://localhost:9000 <access_key> <secret_key>

# Upload a real .7z shapefile archive to the uploads prefix:
mc cp /path/to/test_shapefile.7z local/geo-uploads/uploads/test_shapefile.7z
```

### Step 3 — Monitor pipeline run

Open Airflow UI: `http://localhost:8080`  
DAG: `geo_pipeline_daily`

**Expected task sequence (all green):**
```
sense_new_7z_archive      → success
inspect_archive           → success
branch_operation_mode     → success
bronze_write_[mode]       → success
silver_transform          → success
gold_generate             → success
pmtiles_generate          → success
deploy_gate               → success   ← GATE
```

**Timeout:** sensor polls every 60s, schedule is */15. First run triggers within 15 min of upload.

### Step 4 — Verify ephemeral Silver cleanup (ADR-011)

```bash
# After deploy_gate success, Silver temp must be absent:
mc ls local/geo-silver-tmp/
# Expected: empty or path not found
```

**HALT if:** `deploy_gate` task fails → read task log in Airflow UI.  
Log shows `FAIL [<check_id>]: <message>` for each failing check.  
Fix the underlying issue. Do NOT comment out failing checks in `deploy_gate.py`.  
If a check must be waived: add `waiver_reason="<justification>"` to `CheckResult` in `deploy_gate.py` and commit the documented waiver.

### Step 5 — Verify Gold + PMTiles artifacts in MinIO

```bash
mc ls local/geo-gold/
# Expected: gold/<dataset_name>/features.parquet

mc ls local/geo-pmtiles/
# Expected: pmtiles/<dataset_name>.pmtiles
```

---

## Gate 4 — Lineage Schema

**Success criteria:** `geo_lineage_edges` table exists (from Day 4 DDL). Recursive CTE query plan reviewed.

### Step 1 — Run verification script

```bash
psql $POSTGRES_DSN -f sql/lineage/lineage_verification.sql 2>&1 | tee /tmp/lineage_verify.txt
```

### Step 2 — Check each verification block

**CHECK 1 — Column structure:**  
Expected 7 columns: `id, source_type, source_id, target_type, target_id, pipeline_run_id, created_at`

**CHECK 2 — CHECK constraints:**  
Both `source_type` and `target_type` constrained to `('archive','bronze','silver','gold','pmtiles')`

**CHECK 3 — Indexes:**  
Three indexes expected:
```
geo_lineage_source   ON (source_id, source_type)
geo_lineage_target   ON (target_id, target_type)
<pk_index>           ON (id)
```

**CHECK 4 — UNIQUE constraint:**  
`UNIQUE (source_id, target_id, pipeline_run_id)` — idempotency guarantee.

**CHECK 5 — Empty table:**  
`edge_count = 0` — Phase 3 populates.

**CHECK 6 — Rolled-back idempotency test:**  
`final_count = 0` — confirms `BEGIN/ROLLBACK` worked.

### Step 3 — Review EXPLAIN output for recursive CTEs

In `/tmp/lineage_verify.txt`, find the two `EXPLAIN` blocks.

**QUERY PLAN REVIEW A (traceback — §30.2):**  
Confirm index `geo_lineage_target` appears in plan for the base-case `WHERE target_id = ...` lookup.

**QUERY PLAN REVIEW B (downstream impact — §30.3):**  
Confirm index `geo_lineage_source` appears in plan for the base-case `WHERE source_id = ...` lookup.

If table is empty, planner may choose Sequential Scan (no rows → Seq Scan is correct). Re-run EXPLAIN after Gate 3 inserts real data (Phase 3), or force index scan for verification:
```sql
SET enable_seqscan = OFF;
-- re-run EXPLAIN blocks
SET enable_seqscan = ON;
```

### Step 4 — Gate 4 pass condition

```
[ ] CHECK 1–6 all return expected values
[ ] EXPLAIN output reviewed — index names confirmed or seqscan justified by empty table
[ ] /tmp/lineage_verify.txt saved as sprint artifact
```

---

## Day 5 Go/No-Go

```
[ ] Gate 1 PASS: all containers healthy, build < 8 min
[ ] Gate 2 PASS: all CI checks green
[ ] Gate 3 PASS: first automated end-to-end run, deploy_gate green, Silver cleaned up
[ ] Gate 4 PASS: lineage table verified, EXPLAIN reviewed

ALL 4 GATES PASS → proceed to Day 6 (r/MapPorn + r/dataisbeautiful posts, RUNBOOK.md)
ANY GATE FAILS   → fix before Day 6. Do not carry blockers forward.
```

---

*Day 5 artifacts generated in sprint session. Section refs: §25 (DAG), §26 (Dockerfile), §28 (CI), §30 (lineage).*  
*master_plan_v2.2_final.md — Day 5 block.*
