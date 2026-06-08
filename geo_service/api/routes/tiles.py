"""
GET /geo/v1/tiles/:z/:x/:y.mvt — proxy to Martin tile server.
Rate limit: 500/minute (23.6).
Martin is the canonical tile source; FastAPI adds auth surface, rate limit, cache header.
"""
import os
import time

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from geo_service.api.limiter import limiter
from geo_service.infra.cache import TTLCache

log = structlog.get_logger()
router = APIRouter(prefix="/geo/v1", tags=["tiles"])

MARTIN_BASE_URL = os.getenv("MARTIN_BASE_URL", "http://martin:3002")
TILE_DATASET = os.getenv("TILE_DATASET", "ph_boundaries")

_tile_cache = TTLCache(ttl_seconds=3600)

# ---------------------------------------------------------------------------
# Tile validation helpers
# ---------------------------------------------------------------------------
_MAX_ZOOM = 14


def _validate_tile_coords(z: int, x: int, y: int) -> None:
    if z < 0 or z > _MAX_ZOOM:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Zoom must be 0–{_MAX_ZOOM}, got {z}",
        )
    max_coord = 2**z
    if not (0 <= x < max_coord and 0 <= y < max_coord):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tile {z}/{x}/{y} out of range for zoom {z}",
        )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/tiles/{z}/{x}/{y}.mvt")
@limiter.limit("500/minute")
async def get_tile(z: int, x: int, y: int, request: Request) -> Response:
    """
    Proxy Martin MVT tile.
    Cache key: z/x/y (tiles are deterministic; dataset versioned via Martin config).
    Returns 204 on empty tile (Martin convention).
    """
    _validate_tile_coords(z, x, y)

    cache_key = f"tile:{z}:{x}:{y}"
    if cached := _tile_cache.get(cache_key):
        return Response(
            content=cached["data"],
            media_type="application/vnd.mapbox-vector-tile",
            headers={"X-Cache": "HIT", "Cache-Control": "public, max-age=3600"},
        )

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{MARTIN_BASE_URL}/{TILE_DATASET}/{z}/{x}/{y}"
            )
    except httpx.RequestError as exc:
        log.error("tiles.martin_unreachable", z=z, x=x, y=y, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tile server unreachable",
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info("tiles.proxy", z=z, x=x, y=y, status=resp.status_code, ms=elapsed_ms)

    if resp.status_code == 204:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Martin returned {resp.status_code}",
        )

    data = resp.content
    _tile_cache.set(cache_key, {"data": data})
    return Response(
        content=data,
        media_type="application/vnd.mapbox-vector-tile",
        headers={"X-Cache": "MISS", "Cache-Control": "public, max-age=3600"},
    )
