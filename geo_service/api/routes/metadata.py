"""
GET /geo/v1/metadata/:dataset — return Jenks breaks + schema metadata.
No rate limit annotation needed (low-cardinality, read from DuckDB metadata tables).
"""
import time

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from geo_service.api.main import limiter
from geo_service.infra.cache import TTLCache
from geo_service.infra.duckdb_conn import get_conn_manager

log = structlog.get_logger()
router = APIRouter(prefix="/geo/v1", tags=["metadata"])

_meta_cache = TTLCache(ttl_seconds=3600)

_META_PARQUET = "/data/gold/metadata/dataset_metadata.parquet"


@router.get("/metadata/{dataset}")
@limiter.limit("100/minute")
async def get_metadata(dataset: str, request: Request) -> JSONResponse:
    """
    Return Jenks break points, indicator list, and feature count for a dataset.
    Used by frontend to initialize color ramps and legend without a tile request.
    """
    # Basic allowlist: alphanumeric + underscore only
    if not dataset.replace("_", "").isalnum() or len(dataset) > 80:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid dataset name",
        )

    cache_key = f"metadata:{dataset}"
    if cached := _meta_cache.get(cache_key):
        return JSONResponse(content=cached, headers={"X-Cache": "HIT"})

    conn = get_conn_manager().read_conn()
    sql = """
        SELECT
            dataset_name,
            indicator,
            jenks_break_1,
            jenks_break_2,
            jenks_break_3,
            jenks_break_4,
            feature_count,
            vintage_year,
            geometry_type,
            h3_resolution
        FROM read_parquet($1)
        WHERE dataset_name = $2
    """

    t0 = time.monotonic()
    try:
        result = conn.execute(sql, [_META_PARQUET, dataset])
        cols = [d[0] for d in result.description]
        rows = [dict(zip(cols, row)) for row in result.fetchall()]
    except Exception as exc:
        log.error("metadata.query_failed", dataset=dataset, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Metadata layer temporarily unavailable",
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info("metadata.query", dataset=dataset, rows=len(rows), ms=elapsed_ms)

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dataset {dataset!r} not found in metadata registry",
        )

    payload = {"dataset": dataset, "indicators": rows}
    _meta_cache.set(cache_key, payload)
    return JSONResponse(content=payload, headers={"X-Cache": "MISS"})
