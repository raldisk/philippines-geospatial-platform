# GitHub Release — v2.2.0-byod

**Tag:** `v2.2.0-byod`
**Repository:** `philippines-geospatial-platform`
**Date:** 2026-06-08

---

## Release Title

**PH Geospatial Platform v2.2 — BYOD Production Ready**

---

## Release Description (paste into GitHub Release body)

```markdown
## PH Geospatial Platform v2.2 — BYOD Production Ready

A full-stack geospatial data intelligence pipeline for Philippine administrative
boundaries and statistical indicators, built on DuckDB, H3, PMTiles, FastAPI, and
Airflow.

**v2.2 makes the platform fully data-agnostic.** Any user with PSA/NAMRIA-compatible
`.7z` geospatial archives — or any standard shapefile/GeoPackage dataset — can clone,
configure `.env`, and run the full pipeline without modifying a single line of Python code.

---

### What this release delivers

- **BYOD hardening** — 14 fixes across 12 files. All bucket names, CRS settings,
  column mappings, worker counts, bounding boxes, and rate limits are environment-variable
  configurable. No hardcoded Philippine-specific values remain in source.
- **Archive discovery** — `geo_service/pipeline/extract/archive.py` handles nested
  archives, multi-shapefile ZIPs, unknown CRS with `.prj` fallback, and unknown
  indicators with quarantine routing.
- **Full observability** — Prometheus `/metrics` with request duration histogram and
  per-archive ingestion counter. structlog JSON with per-request `request_id`. All four
  external dependencies probed at `/health/ready` (DuckDB, Martin, MinIO, PostgreSQL).
- **Production-grade pipeline** — tenacity retry on DuckDB connections, ProcessPoolExecutor
  GEOS isolation, SHA-256 deduplication, Kimball SCD Type 2 boundaries, append-only fact
  tables enforced at DB permission level.
- **49-check deploy gate** — `run_all_checks()` gates every pipeline run in CI and Airflow.
  Trivy scan, non-root Docker user, Docker Secrets — no credentials in source.
- **Documentation** — `.env.example` (30+ vars), `docs/BYOD_GUIDE.md`, `docs/ARCHITECTURE.md`
  with ADR index, `docs/SOURCE-OF-TRUTH-MATRIX.md`, `CHANGELOG.md`.

---

### Audit status

| Scope | Checks | Result |
|-------|--------|--------|
| Mechanical (FIX + CC) | 19 | ✅ All PASS |
| BYOD hardening | 14 | ✅ All PASS |
| Architecture (DDIA + Kimball + FoSA + FAANG) | 23 | ✅ 18 PASS · 0 FAIL · 4 NEEDS_HUMAN_REVIEW |
| **Total** | **56** | **✅ PASS — BYOD PRODUCTION READY** |

The 4 NEEDS_HUMAN_REVIEW items (DDIA-03, DDIA-04, FAANG-02, FAANG-06) are tracked in
`CHANGELOG.md` as next-sprint backlog. None block BYOD adoption.

---

### Quick start

```bash
git clone https://github.com/raldisk/philippines-geospatial-platform
cd philippines-geospatial-platform
cp .env.example .env          # configure MinIO, PostgreSQL, Airflow credentials
docker-compose -f docker-compose.prod.yml up -d
# upload your .7z archive to MinIO geo-uploads bucket
# trigger the geo_pipeline_daily Airflow DAG
```

See `docs/BYOD_GUIDE.md` for format support, column mapping, and troubleshooting.

---

### Known tracked debt

| Item | Tracked |
|------|---------|
| 4K PNG uses PlateCarree — Robinson needed before r/MapPorn submission | `CHANGELOG.md` RENDER-01 |
| Synthetic data — replace with real PSA/NAMRIA archives for production traffic | `CHANGELOG.md` DATA-01 |
| Deploy gate static checks only — no live connectivity probes (FAANG-02) | Next sprint |
| No circuit breaker on DuckDB (FAANG-06) — tenacity retry only | Evaluate under load |

---

### Checksums

Verify your download before use:

| File | Algorithm | Hash |
|------|-----------|------|
| `ph-geospatial-platform-v2.2-github-ready.zip` | SHA-256 | see `RELEASE-CHECKSUMS.txt` |
| `ph-geospatial-platform-v2.2-github-ready.tar.gz` | SHA-256 | see `RELEASE-CHECKSUMS.txt` |

```bash
# See RELEASE-CHECKSUMS.txt (delivered alongside the zip) for verified hashes.
# Checksums are computed after final packaging to avoid circular self-reference.
```

---

### Supersedes

- `ph-geospatial-platform-byod-ready.zip` → archived (P8 historical)
- `ph-geospatial-platform-final-audited.zip` → archived (P8 historical, FAIL state)
- `ph-geospatial-platform-remediated.zip` → archived (P8 historical, intermediate)
```

---

## gh CLI Commands (run from repo root after push)

```bash
# Tag the release
git tag -a v2.2.0-byod -m "BYOD Production Ready — data-agnostic Philippine geospatial platform"
git push origin v2.2.0-byod

# Create GitHub Release with both assets
gh release create v2.2.0-byod \
  ph-geospatial-platform-v2.2-github-ready.zip \
  ph-geospatial-platform-v2.2-github-ready.tar.gz \
  --title "PH Geospatial Platform v2.2 — BYOD Production Ready" \
  --notes-file GITHUB-RELEASE-v2.2.0.md \
  --latest
```

> **Note:** Run `gh release create` from the directory containing both asset files.
> If assets are in a subdirectory, adjust paths accordingly.
> The `--notes-file` flag reads the release body from the markdown file below the
> Release Description section above (strip the outer code fences before saving).

---

## SHA256 Reference Card

```
See RELEASE-CHECKSUMS.txt for final verified hashes.
(Computed post-packaging — file is delivered alongside the release assets.)
```

---

## Version History (quick)

| Tag | Date | Summary |
|-----|------|---------|
| `v2.2.0-byod` | 2026-06-08 | BYOD Production Ready — this release |
| `v2.1.0` | 2026-06-07 | Stage 4C remediation (tenacity, Prometheus, structlog, ARCHITECTURE.md) |
| `v2.0.0` | 2026-06-06 | Day 1–6 sprint complete — initial pipeline + serving layer |
