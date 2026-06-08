"""
api/routes/h3.py
GET /geo/v1/h3/{resolution}

Returns GeoJSON FeatureCollection of H3 hexagon polygons.
Each feature property includes jenks_class for Deck.gl colour mapping.

ADR-003: H3 for Deck.gl layer; resolutions 5, 6, 7 only.
Section 23.4: bbox validated against PH envelope before any query.
Section 23.6: 100/min rate limit on H3 endpoint.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

import h3 as h3lib
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from geo_service.api.limiter import limiter

from geo_service.infra.cache import TTLCache
from geo_service.infra.duckdb_conn import get_duckdb_conn

log = structlog.get_logger()
router = APIRouter(prefix="/geo/v1")

# 1-hour TTL — Gold aggregates regenerated at most daily
_cache: TTLCache = TTLCache(ttl_seconds=3600)

# ── Resolution guard ────────────────────────────────────────────────────────
# BYOD-API-01: accepts full H3 range 0–15.
# Operators may restrict to a subset via VALID_H3_RESOLUTIONS env var
# (comma-separated, e.g. "5,6,7").  Default: all resolutions.
_valid_res_env = os.getenv("VALID_H3_RESOLUTIONS", "")
VALID_RESOLUTIONS: frozenset[int] = (
    frozenset(int(r.strip()) for r in _valid_res_env.split(",") if r.strip())
    if _valid_res_env
    else frozenset(range(16))  # 0–15 inclusive
)

# ── Philippine envelope defaults (BYOD-API-02: env-overridable) ────────────
# Users ingesting sub-national or non-PH datasets can override these.
# Example: TARGET_BBOX_LON_MIN=121.0 for Metro Manila only.
_PH: dict[str, float] = {
    "lon_min": float(os.getenv("TARGET_BBOX_LON_MIN", "116.0")),
    "lon_max": float(os.getenv("TARGET_BBOX_LON_MAX", "127.0")),
    "lat_min": float(os.getenv("TARGET_BBOX_LAT_MIN", "4.5")),
    "lat_max": float(os.getenv("TARGET_BBOX_LAT_MAX", "22.0")),
}


class BboxParam(BaseModel):
    lon_min: float = Field(..., ge=_PH["lon_min"], le=_PH["lon_max"])
    lat_min: float = Field(..., ge=_PH["lat_min"], le=_PH["lat_max"])
    lon_max: float = Field(..., ge=_PH["lon_min"], le=_PH["lon_max"])
    lat_max: float = Field(..., ge=_PH["lat_min"], le=_PH["lat_max"])

    @validator("lon_max")
    def lon_max_gt_min(cls, v: float, values: dict) -> float:
        if "lon_min" in values and v <= values["lon_min"]:
            raise ValueError("lon_max must be greater than lon_min")
        return v

    @validator("lat_max")
    def lat_max_gt_min(cls, v: float, values: dict) -> float:
        if "lat_min" in values and v <= values["lat_min"]:
            raise ValueError("lat_max must be greater than lat_min")
        return v


def _parse_bbox(raw: str) -> BboxParam:
    """Parse 'lon_min,lat_min,lon_max,lat_max' into validated BboxParam."""
    try:
        parts = [float(x.strip()) for x in raw.split(",")]
    except ValueError:
        raise ValueError("bbox must be four comma-separated floats")
    if len(parts) != 4:
        raise ValueError("bbox must have exactly four values: lon_min,lat_min,lon_max,lat_max")
    return BboxParam(lon_min=parts[0], lat_min=parts[1], lon_max=parts[2], lat_max=parts[3])


# ── Indicator whitelist (Section 23.4 SQL-injection guard) ─────────────────

_INDICATOR_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")


def _validate_indicator(indicator: str) -> str:
    if not _INDICATOR_RE.match(indicator):
        raise ValueError(
            f"Invalid indicator {indicator!r}: must match [a-z][a-z0-9_]{{0,49}}"
        )
    return indicator


# ── DuckDB query builder ────────────────────────────────────────────────────


def _build_h3_query(resolution: int, indicator: str) -> str:
    """
    Query geo_h3_aggregates DuckDB view.
    View schema (gold/duckdb_views.py):
        h3_index TEXT, h3_resolution TINYINT, indicator_nk TEXT,
        indicator_mean DOUBLE, indicator_std DOUBLE,
        feature_count INTEGER, jenks_class TINYINT,
        jenks_breaks TEXT (JSON array)

    bbox filtering is done in Python — PH cell count at res 5-7 is ~300-5000 total.
    Indicator already validated — safe to interpolate.
    """
    return f"""
        SELECT
            h3_index,
            indicator_mean,
            indicator_std,
            feature_count,
            jenks_class,
            jenks_breaks
        FROM geo_h3_aggregates
        WHERE h3_resolution = {resolution}
          AND indicator_nk = '{indicator}'
        ORDER BY h3_index
    """


# ── GeoJSON assembly ────────────────────────────────────────────────────────


def _h3_centroid_in_bbox(h3_index: str, bbox: BboxParam) -> bool:
    """True iff H3 cell centroid falls within bbox."""
    lat, lon = h3lib.h3_to_geo(h3_index)
    return (
        bbox.lon_min <= lon <= bbox.lon_max
        and bbox.lat_min <= lat <= bbox.lat_max
    )


def _rows_to_geojson(
    rows: list[dict[str, Any]],
    bbox: Optional[BboxParam],
) -> dict[str, Any]:
    """
    Convert H3 aggregate rows → GeoJSON FeatureCollection.

    h3lib.h3_to_geo_boundary() returns [(lat, lon), ...].
    GeoJSON expects [lon, lat] ordering — transposed here.
    Ring is closed by appending first coordinate at the end.
    """
    features: list[dict] = []
    skipped = 0

    for row in rows:
        h3_index: str = row["h3_index"]

        # bbox post-filter (no DuckDB spatial call required for centroid check)
        if bbox and not _h3_centroid_in_bbox(h3_index, bbox):
            continue

        try:
            boundary_latlon = h3lib.h3_to_geo_boundary(h3_index)
            # Transpose (lat, lon) → [lon, lat] for GeoJSON compliance
            coords_lonlat = [[lon, lat] for lat, lon in boundary_latlon]
            coords_lonlat.append(coords_lonlat[0])  # close ring

            jenks_breaks = row["jenks_breaks"]
            if isinstance(jenks_breaks, str):
                jenks_breaks = json.loads(jenks_breaks)

            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [coords_lonlat],
                    },
                    "properties": {
                        "h3_index": h3_index,
                        "indicator_mean": (
                            float(row["indicator_mean"])
                            if row["indicator_mean"] is not None
                            else None
                        ),
                        "indicator_std": (
                            float(row["indicator_std"])
                            if row.get("indicator_std") is not None
                            else None
                        ),
                        "feature_count": int(row["feature_count"]),
                        # jenks_class 1–5 — Deck.gl colour accessor maps this
                        "jenks_class": int(row["jenks_class"]),
                        # Pass breaks for tooltip legend rendering
                        "jenks_breaks": jenks_breaks,
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "h3.boundary_conversion_failed",
                h3_index=h3_index,
                error=str(exc),
            )
            skipped += 1

    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "feature_count": len(features),
            "skipped_count": skipped,
        },
    }


# ── Endpoint ────────────────────────────────────────────────────────────────


_H3_RATE_LIMIT: str = os.getenv("RATE_LIMIT_H3", "100/minute")


@router.get(
    "/h3/{resolution}",
    response_class=JSONResponse,
    summary="H3 hexagon aggregates as GeoJSON",
    responses={
        200: {"description": "GeoJSON FeatureCollection with jenks_class per hexagon"},
        422: {"description": "Invalid resolution, indicator, or bbox"},
        503: {"description": "DuckDB analytical layer unavailable"},
    },
)
@limiter.limit(_H3_RATE_LIMIT)
async def get_h3_aggregates(
    request: Request,  # required by slowapi for key_func
    resolution: int,
    bbox: Optional[str] = Query(
        None,
        description=(
            "Philippine-envelope bounding box: lon_min,lat_min,lon_max,lat_max. "
            "Envelope: 116–127°E, 4.5–22°N."
        ),
        example="119.0,10.0,125.0,18.0",
    ),
    indicator: str = Query(
        "poverty_rate",
        description="Indicator natural key from dim_indicator.indicator_nk",
        example="poverty_rate",
    ),
    conn=Depends(get_duckdb_conn),
) -> JSONResponse:
    """
    GET /geo/v1/h3/{resolution}

    Returns a GeoJSON FeatureCollection of H3 hexagons at the requested
    resolution.  Each feature carries a ``jenks_class`` property (1–5) for
    Deck.gl H3HexagonLayer / GeoJsonLayer colour mapping and a full
    ``jenks_breaks`` array for tooltip legend rendering.

    Deck.gl usage (JavaScript):
        const layer = new GeoJsonLayer({
            id: 'h3-layer',
            data: '/geo/v1/h3/7?indicator=poverty_rate',
            getFillColor: d => JENKS_COLORS[d.properties.jenks_class - 1],
            getTooltip: d => `${d.properties.h3_index}: ${d.properties.indicator_mean?.toFixed(2)}`,
        });
    """
    t0 = time.perf_counter()

    # ── Resolution guard ────────────────────────────────────────────────────
    if resolution not in VALID_RESOLUTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Resolution {resolution} not in accepted set {sorted(VALID_RESOLUTIONS)}. "
                "Valid H3 resolutions are 0–15. Operator may restrict via "
                "VALID_H3_RESOLUTIONS env var."
            ),
        )

    # ── Input validation ────────────────────────────────────────────────────
    try:
        validated_indicator = _validate_indicator(indicator)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    validated_bbox: Optional[BboxParam] = None
    if bbox:
        try:
            validated_bbox = _parse_bbox(bbox)
        except (ValueError, Exception) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid bbox: {exc}",
            ) from exc

    # ── Cache lookup ────────────────────────────────────────────────────────
    cache_key = f"h3:{resolution}:{validated_indicator}:{bbox}"
    if cached := _cache.get(cache_key):
        log.info(
            "h3.cache_hit",
            resolution=resolution,
            indicator=validated_indicator,
            duration_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        return JSONResponse(content=cached, headers={"X-Cache": "HIT"})

    # ── DuckDB query ────────────────────────────────────────────────────────
    log.info(
        "h3.query_start",
        resolution=resolution,
        indicator=validated_indicator,
        bbox=bbox,
    )

    try:
        query = _build_h3_query(resolution, validated_indicator)
        rows: list[dict] = conn.execute(query).df().to_dict(orient="records")
    except Exception as exc:
        log.error(
            "h3.query_failed",
            resolution=resolution,
            indicator=validated_indicator,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analytical layer temporarily unavailable",
        ) from exc

    # ── GeoJSON assembly ────────────────────────────────────────────────────
    geojson = _rows_to_geojson(rows, validated_bbox)

    duration_ms = round((time.perf_counter() - t0) * 1000, 1)

    headers: dict[str, str] = {
        "X-Cache": "MISS",
        "X-Query-Duration-Ms": str(duration_ms),
    }

    if not geojson["features"]:
        # Empty result — signal to consumer (same pattern as EconIntel)
        headers["X-Geo-Synthetic-Data"] = "true"
        log.warning(
            "h3.empty_result",
            resolution=resolution,
            indicator=validated_indicator,
            bbox=bbox,
        )
    else:
        _cache.set(cache_key, geojson)

    log.info(
        "h3.query_complete",
        resolution=resolution,
        indicator=validated_indicator,
        feature_count=geojson["metadata"]["feature_count"],
        duration_ms=duration_ms,
    )

    return JSONResponse(content=geojson, headers=headers)
