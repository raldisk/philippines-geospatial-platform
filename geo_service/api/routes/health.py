"""
Health probes — /health/live and /health/ready.

/health/ready verifies ALL four external dependencies:
  1. DuckDB — live query: SELECT ST_AsText(...)
  2. Martin  — HTTP probe: GET /health
  3. MinIO   — HeadBucket probe (FAANG-03 fix)
  4. PostgreSQL — SELECT 1 probe (FAANG-03 fix)

HALT trigger: if /health/ready returns non-200, stop Day 3.
"""
import os
import time

import httpx
import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from geo_service.infra.duckdb_conn import get_conn_manager

log = structlog.get_logger()
router = APIRouter(tags=["health"])

MARTIN_BASE_URL = os.getenv("MARTIN_BASE_URL", "http://martin:3002")

# MinIO / PostgreSQL probe config — read from env (injected via Docker secrets wrapper)
MINIO_ENDPOINT   = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_BUCKET     = os.getenv("GEO_UPLOAD_BUCKET", "geo-uploads")
POSTGRES_DSN     = os.getenv("POSTGRES_DSN", "")


@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness() -> JSONResponse:
    """
    Kubernetes liveness probe.
    Passes if process is running — no dependency checks.
    """
    return JSONResponse({"status": "alive", "service": "geo-service"})


@router.get("/health/ready", status_code=status.HTTP_200_OK)
async def readiness() -> JSONResponse:
    """
    Kubernetes readiness probe.
    Returns 200 ONLY if DuckDB, Martin, MinIO, AND PostgreSQL all pass.
    Returns 503 if any dependency is unhealthy.

    FAANG-03 fix: added MinIO HeadBucket and PostgreSQL SELECT 1 probes.
    """
    checks: dict[str, str] = {}
    failed = False

    # --- DuckDB check ---------------------------------------------------
    t0 = time.monotonic()
    try:
        conn = get_conn_manager().read_conn()
        conn.execute("SELECT ST_AsText(ST_Point(120.9842, 14.5995))").fetchone()
        checks["duckdb"] = f"ok ({int((time.monotonic() - t0) * 1000)}ms)"
    except Exception as exc:
        log.error("health.ready.duckdb_fail", error=str(exc))
        checks["duckdb"] = f"fail: {exc}"
        failed = True

    # --- Martin check ---------------------------------------------------
    t1 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{MARTIN_BASE_URL}/health")
            if resp.status_code != 200:
                raise RuntimeError(f"Martin returned {resp.status_code}")
        checks["martin"] = f"ok ({int((time.monotonic() - t1) * 1000)}ms)"
    except Exception as exc:
        log.error("health.ready.martin_fail", error=str(exc))
        checks["martin"] = f"fail: {exc}"
        failed = True

    # --- MinIO check (FAANG-03) -----------------------------------------
    # HeadBucket: confirms MinIO is reachable and bucket exists.
    # Uses boto3 if available; falls back to raw HTTP HEAD if not.
    t2 = time.monotonic()
    try:
        try:
            import boto3
            from botocore.config import Config as BotoConfig
            from geo_service.infra.secrets import get_minio_access_key, get_minio_secret_key

            s3 = boto3.client(
                "s3",
                endpoint_url=MINIO_ENDPOINT,
                aws_access_key_id=get_minio_access_key(),
                aws_secret_access_key=get_minio_secret_key(),
                config=BotoConfig(connect_timeout=2, read_timeout=2, retries={"max_attempts": 1}),
            )
            s3.head_bucket(Bucket=MINIO_BUCKET)
            checks["minio"] = f"ok ({int((time.monotonic() - t2) * 1000)}ms)"
        except ImportError:
            # boto3 not available — fall back to HTTP liveness endpoint
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{MINIO_ENDPOINT}/minio/health/live")
                if resp.status_code not in (200, 204):
                    raise RuntimeError(f"MinIO liveness returned {resp.status_code}")
            checks["minio"] = f"ok-http ({int((time.monotonic() - t2) * 1000)}ms)"
    except Exception as exc:
        log.error("health.ready.minio_fail", error=str(exc))
        checks["minio"] = f"fail: {exc}"
        failed = True

    # --- PostgreSQL check (FAANG-03) ------------------------------------
    # SELECT 1: confirms PostgreSQL is reachable and the DSN is valid.
    t3 = time.monotonic()
    try:
        postgres_dsn = POSTGRES_DSN
        if not postgres_dsn:
            # Try reading from Docker secret
            try:
                from geo_service.infra.secrets import get_postgres_dsn
                postgres_dsn = get_postgres_dsn()
            except Exception:
                pass

        if not postgres_dsn:
            raise RuntimeError("POSTGRES_DSN not configured — set env var or Docker secret")

        try:
            import psycopg2
            conn_pg = psycopg2.connect(postgres_dsn, connect_timeout=2)
            with conn_pg.cursor() as cur:
                cur.execute("SELECT 1")
            conn_pg.close()
        except ImportError:
            # psycopg2 not available — try asyncpg
            import asyncpg  # type: ignore[import]
            pg_conn = await asyncpg.connect(postgres_dsn, timeout=2)
            await pg_conn.fetchval("SELECT 1")
            await pg_conn.close()

        checks["postgresql"] = f"ok ({int((time.monotonic() - t3) * 1000)}ms)"
    except Exception as exc:
        log.error("health.ready.postgres_fail", error=str(exc))
        checks["postgresql"] = f"fail: {exc}"
        failed = True

    http_status = status.HTTP_503_SERVICE_UNAVAILABLE if failed else status.HTTP_200_OK
    return JSONResponse(
        {"status": "degraded" if failed else "ready", "checks": checks},
        status_code=http_status,
    )
