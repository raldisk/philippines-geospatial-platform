# Audit Correction — FAANG-03 Status

**Date:** 2026-06-08
**Item:** FAANG-03 — MinIO and PostgreSQL health probes in `/health/ready`
**External audit verdict:** FAIL (in `final-verification-audit-Day7-BYOD-READY.md`)
**Actual status:** **PASS — already fixed**

---

## Explanation

The external BYOD audit document (`final-verification-audit-Day7-BYOD-READY.md`)
marks FAANG-03 as FAIL with the note: "MinIO and PostgreSQL probes still missing."

This is a **stale entry**. The audit document was generated during the same session
that applied the BYOD hardening fixes, but was written before FAANG-03 was patched.
The internal audit bundled in the release package (`docs/final-verification-audit-Day7-FINAL.md`)
correctly marks FAANG-03 as **PASS (fixed)**.

---

## Verification

`geo_service/api/routes/health.py` contains:

- **MinIO probe:** `boto3.client.head_bucket(Bucket=MINIO_BUCKET)` with HTTP fallback
  to `GET /minio/health/live` when boto3 is unavailable.
- **PostgreSQL probe:** `psycopg2.connect + SELECT 1` with asyncpg fallback.
- Both probes gate `/health/ready` — any failure returns `HTTP 503`.

The docstring in `health.py` explicitly reads:
```
/health/ready verifies ALL four external dependencies:
  1. DuckDB — live query: SELECT ST_AsText(...)
  2. Martin  — HTTP probe: GET /health
  3. MinIO   — HeadBucket probe (FAANG-03 fix)
  4. PostgreSQL — SELECT 1 probe (FAANG-03 fix)
```

---

## Authority Chain

| Document | FAANG-03 verdict | Precedence |
|----------|-----------------|------------|
| `geo_service/api/routes/health.py` | PASS (code present) | P1 — release truth |
| `docs/final-verification-audit-Day7-FINAL.md` | PASS (fixed this session) | Internal audit |
| `final-verification-audit-Day7-BYOD-READY.md` (external) | FAIL (stale) | Superseded |

**Canonical verdict: PASS.**

---

## Remaining Architecture Debt (actual)

With FAANG-03 resolved, the remaining items are all NEEDS_HUMAN_REVIEW, not FAIL:

| ID | Item | Risk Level |
|----|------|------------|
| DDIA-03 | No dedicated dead-letter queue path for failed DAG run payloads | Medium |
| DDIA-04 | No checkpoint/resume in inspector — reprocesses full archive on retry | Low (small archives) |
| FAANG-02 | Deploy gate static-only checks — no live DuckDB/MinIO/PostgreSQL probes | Medium |
| FAANG-06 | No circuit breaker — tenacity retry only | Low (DuckDB is local) |

All four are documented in the Remediation Plan next-sprint backlog.
