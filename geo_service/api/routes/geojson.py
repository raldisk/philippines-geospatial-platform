"""
GET /geo/v1/geojson/:layer — DuckDB ST_AsGeoJSON with optional bbox filter.
Rate limit: 50/minute (23.6).
ADR-012: bbox filter -> row-group pruning via Hilbert covering bbox.
"""
import json
import os
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from geo_service.api.limiter import limiter
from geo_service.api.middleware.validation import parse_bbox
from geo_service.infra.cache import TTLCache
from geo_service.infra.duckdb_conn import get_conn_manager

log = structlog.get_logger()
router = APIRouter(prefix="/geo/v1", tags=["geojson"])

_geojson_cache = TTLCache(ttl_seconds=3600)

# Whitelist of available layers -> parquet paths (populated at startup from Gold layer)
_LAYER_PATHS: dict[str, str] = {
    "provincial": "/data/gold/curated/psa_provincial.parquet",
    "municipal":  "/data/gold/curated/psa_municipal.parquet",
    "barangay":   "/data/gold/curated/psa_barangay.parquet",
}


def _validate_layer(layer: str) -> str:
    if layer not in _LAYER_PATHS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Layer {layer!r} not found. Available: {list(_LAYER_PATHS)}",
        )
    return layer


_GEOJSON_RATE_LIMIT: str = os.getenv("RATE_LIMIT_GEOJSON", "50/minute")


@router.get("/geojson/{layer}")
@limiter.limit(_GEOJSON_RATE_LIMIT)
async def get_geojson(
    layer: str,
    request: Request,
    bbox: str | None = Query(
        None,
        description="lon_min,lat_min,lon_max,lat_max — constrained to PH envelope",
        example="116.0,4.5,127.0,22.0",
    ),
    limit: int = Query(5000, ge=1, le=20000),
) -> JSONResponse:
    """
    Return GeoJSON FeatureCollection from Gold Parquet.
    bbox filter uses DuckDB Hilbert row-group pruning (ADR-012).
    ST_AsGeoJSON is the only geometry operation on the serving path (no shapely — ADR-007).
    """
    _validate_layer(layer)
    parquet_path = _LAYER_PATHS[layer]

    cache_key = f"geojson:{layer}:{bbox}:{limit}"
    if cached := _geojson_cache.get(cache_key):
        return JSONResponse(content=cached, headers={"X-Cache": "HIT"})

    conn = get_conn_manager().read_conn()

    # Build spatial filter clause
    where_clause = ""
    params: list[float] = []
    if bbox:
        try:
            b = parse_bbox(bbox)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=str(exc))
        # DuckDB ST_Within + covering bbox enables row-group pruning (ADR-012)
        where_clause = """
            WHERE ST_Intersects(
                geometry,
                ST_MakeEnvelope($1, $2, $3, $4)
            )
        """
        params = [b.lon_min, b.lat_min, b.lon_max, b.lat_max]

    sql = f"""
        SELECT
            region_name,
            ST_AsGeoJSON(geometry)  AS geom_json,
            * EXCLUDE (geometry)
        FROM read_parquet('{parquet_path}')
        {where_clause}
        LIMIT {limit}
    """

    t0 = time.monotonic()
    try:
        if params:
            rows = conn.execute(sql, params).fetchall()
            cols = [d[0] for d in conn.execute(sql, params).description]
        else:
            result = conn.execute(sql)
            cols = [d[0] for d in result.description]
            rows = result.fetchall()
    except Exception as exc:
        log.error("geojson.query_failed", layer=layer, bbox=bbox, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analytical layer temporarily unavailable",
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info("geojson.query", layer=layer, bbox=bbox, rows=len(rows), ms=elapsed_ms)

    # Build FeatureCollection
    features = []
    geom_idx = cols.index("geom_json")
    for row in rows:
        props = {c: v for c, v in zip(cols, row) if c != "geom_json"}
        features.append({
            "type": "Feature",
            "geometry": json.loads(row[geom_idx]),
            "properties": props,
        })

    fc = {"type": "FeatureCollection", "features": features}
    _geojson_cache.set(cache_key, fc)
    return JSONResponse(content=fc, headers={"X-Cache": "MISS"})
