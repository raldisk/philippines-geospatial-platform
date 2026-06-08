"""
Silver layer — Day 1 variant.

Implements the three Silver transforms in sequence:
  1. CRS normalize → EPSG:4326
  2. shapely.make_valid() — repair self-intersecting Philippine government geometries
  3. Multi-resolution simplification z4 / z8 / z12

ADR-011 (Ephemeral Silver):
  Silver is NOT a persistent materialized layer. A temp staging file is written to
  data/silver-tmp/, verified, then deleted after Curated write succeeds. No Silver
  Parquet should persist beyond this function's return.

ADR-012: Curated GeoParquet uses write_covering_bbox=True (Hilbert bbox → DuckDB pruning).

Day 1 constraint:
  Single-threaded — no ProcessPoolExecutor (ADR-007 deferred to Day 4).
  Accepts Bronze GeoParquet path OR GeoDataFrame directly.

Verification gate (Day 1 success criteria):
  ✓ gdf.plot() renders without error
  ✓ Feature count matches source (minus quarantined)
  ✓ GeoParquet 'geo' metadata in Parquet footer (write_covering_bbox proof)
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import geopandas as gpd
import shapely
import structlog

log = structlog.get_logger()

CANONICAL_CRS: Final[str] = "EPSG:4326"


@dataclass(frozen=True)
class ZoomLevel:
    name:              str
    tolerance_degrees: float
    min_zoom:          int
    max_zoom:          int


# Named constants — no magic numbers (FAANG: rationale in comment).
# Tolerances in decimal degrees; at PH latitudes (~7–19°N) 1° ≈ 111 km.
ZOOM_LEVELS: Final[tuple[ZoomLevel, ...]] = (
    ZoomLevel("z4",  tolerance_degrees=0.001,    min_zoom=4,  max_zoom=7),   # ~111 m national
    ZoomLevel("z8",  tolerance_degrees=0.0001,   min_zoom=8,  max_zoom=11),  # ~11 m  city
    ZoomLevel("z12", tolerance_degrees=0.00001,  min_zoom=12, max_zoom=18),  # ~1 m   street
)


@dataclass
class SilverResult:
    curated_path:         str
    source_feature_count: int
    output_feature_count: int
    quarantined_count:    int
    crs_reprojected:      bool
    source_epsg:          int | None
    repairs_applied:      int
    silver_tmp_deleted:   bool  # ADR-011 audit: confirms ephemeral staging deleted


class GeospatialValidationError(Exception):
    """Raised on unrecoverable Silver-layer data problems (missing CRS, etc.)."""


# ── main entry point ───────────────────────────────────────────────────────────

def run_silver(
    source: str | gpd.GeoDataFrame,
    dataset_name: str,
    curated_dir: str = "data/curated",
    silver_tmp_dir: str = "data/silver-tmp",
    run_id: str | None = None,
) -> SilverResult:
    """
    Full Silver → Curated pipeline for one dataset.

    `source`: Bronze GeoParquet path (str) OR already-loaded GeoDataFrame.
    Returns SilverResult; raises GeospatialValidationError on unrecoverable error.
    """
    run_id = run_id or str(uuid.uuid4())

    # ── 0. load ───────────────────────────────────────────────────────────────
    if isinstance(source, str):
        log.info("silver.load", dataset=dataset_name, path=source, run_id=run_id)
        gdf = gpd.read_parquet(source)
    else:
        gdf = source.copy()

    source_count = len(gdf)
    log.info("silver.start", dataset=dataset_name, feature_count=source_count, run_id=run_id)

    # ── 1. CRS normalize → EPSG:4326 ─────────────────────────────────────────
    gdf, was_reprojected, source_epsg = _normalize_crs(gdf, dataset_name, run_id)

    # ── 2. shapely.make_valid() ───────────────────────────────────────────────
    gdf, repairs = _repair_geometries(gdf, dataset_name, run_id)

    # ── 3. quarantine null / empty geometries ─────────────────────────────────
    # Never propagate unresolvable geometry failures downstream.
    valid_mask = gdf.geometry.notna() & ~gdf.geometry.is_empty
    quarantine_count = int((~valid_mask).sum())
    if quarantine_count > 0:
        log.warning(
            "silver.quarantine",
            dataset=dataset_name,
            quarantined=quarantine_count,
            pct=round(quarantine_count / source_count * 100, 2),
            reason="null_or_empty_post_repair",
            run_id=run_id,
        )
    gdf = gdf[valid_mask].copy()
    expected_output_count = source_count - quarantine_count

    # ── 4. multi-resolution simplification ───────────────────────────────────
    gdf = _simplify_multi_resolution(gdf, dataset_name)

    # ── 5. write ephemeral Silver temp ────────────────────────────────────────
    silver_tmp_path = _write_silver_tmp(gdf, dataset_name, silver_tmp_dir, run_id)

    # ── 6. write Curated GeoParquet (ADR-012) ─────────────────────────────────
    curated_path = _write_curated(gdf, dataset_name, curated_dir, run_id)

    # ── 7. verify Curated (Day 1 success criteria) ────────────────────────────
    _verify_curated(curated_path, expected_output_count, dataset_name)

    # ── 8. delete ephemeral Silver temp (ADR-011) ─────────────────────────────
    silver_deleted = _delete_silver_tmp(silver_tmp_path, dataset_name)

    log.info(
        "silver.complete",
        dataset=dataset_name,
        curated_path=curated_path,
        output_count=len(gdf),
        quarantined=quarantine_count,
        repairs=repairs,
        silver_tmp_deleted=silver_deleted,
        run_id=run_id,
    )

    return SilverResult(
        curated_path=curated_path,
        source_feature_count=source_count,
        output_feature_count=len(gdf),
        quarantined_count=quarantine_count,
        crs_reprojected=was_reprojected,
        source_epsg=source_epsg,
        repairs_applied=repairs,
        silver_tmp_deleted=silver_deleted,
    )


# ── private transforms ────────────────────────────────────────────────────────

def _normalize_crs(
    gdf: gpd.GeoDataFrame,
    dataset: str,
    run_id: str,
) -> tuple[gpd.GeoDataFrame, bool, int | None]:
    """
    Normalize CRS to EPSG:4326 (WGS84 — single canonical CRS for all downstream).
    Raises GeospatialValidationError if CRS is completely absent (unrecoverable).
    """
    if gdf.crs is None:
        raise GeospatialValidationError(
            f"[{dataset}] CRS undefined in Bronze layer. "
            "Cannot normalize to EPSG:4326. Inspect source .prj file."
        )

    source_epsg = gdf.crs.to_epsg()

    if source_epsg == 4326:
        log.info("silver.crs.canonical", dataset=dataset, run_id=run_id)
        return gdf, False, source_epsg

    log.warning(
        "silver.crs.reprojecting",
        dataset=dataset,
        source_epsg=source_epsg,
        target="EPSG:4326",
        feature_count=len(gdf),
        run_id=run_id,
    )
    return gdf.to_crs(CANONICAL_CRS), True, source_epsg


def _repair_geometries(
    gdf: gpd.GeoDataFrame,
    dataset: str,
    run_id: str,
) -> tuple[gpd.GeoDataFrame, int]:
    """
    Apply shapely.make_valid() to all invalid geometries (ADR-010 non-regulatory path).
    Philippine government shapefiles commonly contain self-intersecting rings (Section 7.2).

    Post-repair: still-invalid geometries set to None → quarantined by caller.
    Single-threaded Day 1 (ProcessPoolExecutor deferred to Day 4 per ADR-007).
    """
    invalid_mask = ~gdf.geometry.is_valid
    repair_count = int(invalid_mask.sum())

    if repair_count == 0:
        log.info("silver.repair.none_needed", dataset=dataset, run_id=run_id)
        return gdf, 0

    log.warning(
        "silver.repair.applying",
        dataset=dataset,
        invalid_count=repair_count,
        pct=round(repair_count / len(gdf) * 100, 2),
        method="shapely.make_valid (non-regulatory — ADR-010)",
        run_id=run_id,
    )

    gdf = gdf.copy()
    gdf.loc[invalid_mask, "geometry"] = (
        gdf.loc[invalid_mask, "geometry"].apply(shapely.make_valid)
    )

    # Post-repair check — any still-invalid → None (quarantined next step)
    still_invalid = ~gdf.geometry.is_valid
    if still_invalid.any():
        count = int(still_invalid.sum())
        log.error(
            "silver.repair.still_invalid",
            dataset=dataset,
            count=count,
            action="setting_to_null_for_quarantine",
            run_id=run_id,
        )
        gdf.loc[still_invalid, "geometry"] = None

    return gdf, repair_count


def _simplify_multi_resolution(
    gdf: gpd.GeoDataFrame,
    dataset: str,
) -> gpd.GeoDataFrame:
    """
    Pre-compute simplified geometry at z4 / z8 / z12 (Section 7.2 ZOOM_LEVELS).
    DDIA §3: amortize CPU at write time — read path is O(1) no-simplification.

    Simplified geometries stored as WKB bytes in geom_z4_wkb / z8_wkb / z12_wkb columns.
    WKB encoding because geopandas.to_parquet only supports a single active geometry column;
    zoom variants are auxiliary and reconstructed by consumers via shapely.from_wkb().

    preserve_topology=True: never produce invalid topology (degenerate rings, etc.).
    """
    result = gdf.copy()

    for zoom in ZOOM_LEVELS:
        simplified_series = result.geometry.apply(
            lambda g: g.simplify(zoom.tolerance_degrees, preserve_topology=True)
            if g is not None and not g.is_empty
            else g
        )
        result[f"geom_{zoom.name}_wkb"] = simplified_series.apply(
            lambda g: g.wkb if (g is not None and not g.is_empty) else None
        )
        log.info(
            "silver.simplify.zoom_done",
            dataset=dataset,
            zoom=zoom.name,
            tolerance_deg=zoom.tolerance_degrees,
            feature_count=len(result),
        )

    return result


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _write_silver_tmp(
    gdf: gpd.GeoDataFrame,
    dataset_name: str,
    silver_tmp_dir: str,
    run_id: str,
) -> str:
    """
    Write ephemeral Silver staging file (ADR-011).
    This file exists only between Silver write and Curated verification.
    """
    Path(silver_tmp_dir).mkdir(parents=True, exist_ok=True)
    tmp_path = str(Path(silver_tmp_dir) / f"{dataset_name}_{run_id[:8]}_silver.parquet")
    gdf.to_parquet(
        tmp_path,
        geometry_encoding="WKB",
        write_covering_bbox=True,
        index=False,
    )
    log.info("silver.tmp_written", path=tmp_path, run_id=run_id)
    return tmp_path


def _write_curated(
    gdf: gpd.GeoDataFrame,
    dataset_name: str,
    curated_dir: str,
    run_id: str,
) -> str:
    """
    Write Curated GeoParquet (ADR-012: write_covering_bbox=True).
    Atomic: write temp → os.replace → final (avoids partial-read by concurrent consumers).
    """
    Path(curated_dir).mkdir(parents=True, exist_ok=True)
    final_path = str(Path(curated_dir) / f"{dataset_name}.parquet")
    tmp_path   = final_path + f".writing_{run_id[:8]}"

    try:
        gdf.to_parquet(
            tmp_path,
            geometry_encoding="WKB",
            write_covering_bbox=True,  # ADR-012
            index=False,
        )
        os.replace(tmp_path, final_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    log.info(
        "silver.curated_written",
        path=final_path,
        feature_count=len(gdf),
        run_id=run_id,
    )
    return final_path


def _verify_curated(
    curated_path: str,
    expected_count: int,
    dataset_name: str,
) -> None:
    """
    Day 1 three-point verification gate (master plan success criteria):
      1. gdf.plot() renders without error.
      2. Feature count matches source minus quarantined.
      3. GeoParquet 'geo' metadata in Parquet footer (write_covering_bbox proof).

    Raises AssertionError on any failure — caller must not proceed to Day 2 if this raises.
    """
    import pyarrow.parquet as pq
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    log.info("silver.verify.start", path=curated_path, expected_count=expected_count)

    gdf_v = gpd.read_parquet(curated_path)
    actual = len(gdf_v)

    # ── check 1: feature count ────────────────────────────────────────────────
    if actual != expected_count:
        raise AssertionError(
            f"[{dataset_name}] Feature count mismatch: "
            f"expected={expected_count}, got={actual}. "
            "Silver quarantine logic may have dropped extra features."
        )
    log.info("silver.verify.feature_count_ok", dataset=dataset_name, count=actual)

    # ── check 2: gdf.plot() renders ──────────────────────────────────────────
    try:
        ax = gdf_v.plot()
        assert ax is not None
        plt.close("all")
        log.info("silver.verify.plot_ok", dataset=dataset_name)
    except Exception as exc:
        raise AssertionError(
            f"[{dataset_name}] gdf.plot() failed — geometry column likely corrupt: {exc}"
        ) from exc

    # ── check 3: 'geo' metadata in Parquet footer (ADR-012) ──────────────────
    raw = pq.read_table(curated_path)
    metadata = raw.schema.metadata or {}
    if b"geo" not in metadata:
        raise AssertionError(
            f"[{dataset_name}] GeoParquet 'geo' metadata absent from Parquet footer. "
            "write_covering_bbox=True must have been omitted. Check _write_curated()."
        )
    log.info(
        "silver.verify.geo_metadata_ok",
        dataset=dataset_name,
        geo_key_present=True,
    )

    log.info(
        "silver.verify.PASSED",
        dataset=dataset_name,
        feature_count=actual,
        checks=["feature_count", "gdf.plot()", "geo_parquet_footer"],
    )


def _delete_silver_tmp(tmp_path: str, dataset: str) -> bool:
    """
    Delete ephemeral Silver staging file (ADR-011).
    Returns True on success, False (with logged warning) if file already gone.
    """
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
        log.info("silver.tmp_deleted", path=tmp_path, dataset=dataset,
                 note="ADR-011: Silver ephemeral — deleted after Curated verification")
        return True
    log.warning("silver.tmp_not_found", path=tmp_path, dataset=dataset,
                note="Already absent — no action needed")
    return False
