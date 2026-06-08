"""
geo_service/pipeline/gold/pmtiles.py

Gold layer: PMTiles generation via pre-built tippecanoe binary (ADR-008).

ADR-001: PMTiles over MBTiles — HTTP range requests, CDN-cacheable, no SQLite locking.
ADR-008: Pre-built binary from felt/tippecanoe GitHub Releases, SHA-256 pinned.
Res 16.4: CI build 8–12 min → ~25 s via pre-built binary.
Day 2 (2–5h): --maximum-tile-bytes=500000, file < 50 MB, pmtiles verify passes.

HALT surfaces (propagate to Airflow → Day 2 gate):
  BinaryIntegrityError    : SHA-256 mismatch.
  TileGenerationError     : tippecanoe non-zero exit or timeout.
  PMTilesVerificationError: `pmtiles verify` fails.
  FileSizeError           : output ≥ 50 MB.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from pathlib import Path

import structlog

from geo_service.config import settings
from geo_service.domain.exceptions import (
    BinaryIntegrityError,
    FileSizeError,
    PMTilesVerificationError,
    TileGenerationError,
)

log = structlog.get_logger()


# ── Binary integrity check (ADR-008) ──────────────────────────────────────────
def verify_binary_integrity() -> None:
    """
    SHA-256 pin verification for pre-built tippecanoe binary.
    Skip via TIPPECANOE_SKIP_SHA256=1 (CI smoke-test bypass only).
    Raises BinaryIntegrityError on mismatch — HALT surface.
    """
    if settings.TIPPECANOE_SKIP_SHA256:
        log.warning(
            "tippecanoe.sha256_check.skipped",
            reason="TIPPECANOE_SKIP_SHA256=1 — CI bypass only",
        )
        return

    path = Path(settings.TIPPECANOE_BIN)
    if not path.exists():
        raise BinaryIntegrityError(
            f"tippecanoe binary not found at {path}. "
            "Install via Dockerfile tippecanoe-binary stage (ADR-008)."
        )

    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = settings.TIPPECANOE_SHA256.lower()

    if sha != expected:
        raise BinaryIntegrityError(
            f"tippecanoe SHA-256 mismatch.\n"
            f"  Expected : {expected}\n"
            f"  Actual   : {sha}\n"
            "Update TIPPECANOE_SHA256 in config.py via reviewed PR. Do not proceed."
        )

    log.info(
        "tippecanoe.sha256_check.passed",
        binary=str(path),
        sha256_prefix=sha[:16] + "...",
        version=settings.TIPPECANOE_VERSION,
    )


# ── tippecanoe subprocess ──────────────────────────────────────────────────────
def _run_tippecanoe(
    geojson_path: Path,
    output_path: Path,
    layer_name: str,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [
        settings.TIPPECANOE_BIN,
        "--output", str(output_path),
        "--layer", layer_name,
        "--minimum-zoom", str(settings.TILE_MIN_ZOOM),
        "--maximum-zoom", str(settings.TILE_MAX_ZOOM),
        "--maximum-tile-bytes", str(settings.TILE_MAX_BYTES),
        "--drop-densest-as-needed",
        "--no-feature-limit",
        "--force",
        str(geojson_path),
    ]
    if extra_args:
        cmd.extend(extra_args)

    log.info("tippecanoe.run.start", cmd=" ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise TileGenerationError(
            f"tippecanoe exceeded 600s timeout for {geojson_path.name}. "
            "Reduce to provincial subset per Day 2 fallback gate."
        ) from exc
    except FileNotFoundError as exc:
        raise TileGenerationError(
            f"tippecanoe not found at {settings.TIPPECANOE_BIN}. "
            "Verify Docker tippecanoe-binary stage ran."
        ) from exc

    return result


# ── pmtiles verify ─────────────────────────────────────────────────────────────
def _verify_pmtiles(output_path: Path) -> None:
    """
    Run `pmtiles verify` — structural integrity check.
    Warns (does not HALT) if pmtiles CLI is absent.
    Raises PMTilesVerificationError on non-zero exit — HALT surface.
    """
    if not shutil.which(settings.PMTILES_BIN):
        log.warning(
            "pmtiles.verify.skipped",
            reason=f"pmtiles CLI not found at {settings.PMTILES_BIN}",
            hint="go install github.com/protomaps/go-pmtiles@latest",
        )
        return

    result = subprocess.run(
        [settings.PMTILES_BIN, "verify", str(output_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    log.info(
        "pmtiles.verify.result",
        output=str(output_path),
        returncode=result.returncode,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
    )

    if result.returncode != 0:
        raise PMTilesVerificationError(
            f"`pmtiles verify` FAILED for {output_path.name}.\n"
            f"  stdout : {result.stdout.strip()}\n"
            f"  stderr : {result.stderr.strip()}\n"
            "HALT — do not open day2_tile_preview.html until this passes."
        )


# ── File size gate ─────────────────────────────────────────────────────────────
def _check_file_size(output_path: Path, dataset_name: str) -> int:
    size_bytes = output_path.stat().st_size
    size_mb = size_bytes / 1_000_000
    limit_mb = settings.TILE_MAX_OUTPUT_MB

    log.info(
        "pmtiles.size_check",
        dataset=dataset_name,
        size_mb=round(size_mb, 2),
        limit_mb=limit_mb,
        passes=size_mb < limit_mb,
    )

    if size_mb >= limit_mb:
        raise FileSizeError(
            f"PMTiles {output_path.name} is {size_mb:.1f} MB (limit: {limit_mb:.0f} MB).\n"
            "Mitigations (Section 29 runbook):\n"
            "  1. Cap --maximum-zoom to 10 for national datasets.\n"
            "  2. Reduce to provincial subset (Day 2 fallback gate).\n"
            "  3. Raise TILE_MAX_BYTES to 750000 (lossy, last resort)."
        )
    return size_bytes


# ── GeoParquet → GeoJSON ───────────────────────────────────────────────────────
def export_curated_to_geojson(
    parquet_path: str | Path,
    geojson_path: str | Path,
    dataset_name: str,
) -> Path:
    """Convert Day 1 Curated GeoParquet → GeoJSON for tippecanoe input."""
    import geopandas as gpd

    parquet_path = Path(parquet_path)
    geojson_path = Path(geojson_path)
    geojson_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("pmtiles.export_geojson.start", dataset=dataset_name, parquet=str(parquet_path))

    gdf = gpd.read_parquet(parquet_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        log.warning("pmtiles.export_geojson.reprojecting", from_crs=str(gdf.crs))
        gdf = gdf.to_crs(epsg=4326)

    gdf.to_file(str(geojson_path), driver="GeoJSON")
    log.info(
        "pmtiles.export_geojson.complete",
        dataset=dataset_name,
        feature_count=len(gdf),
        geojson=str(geojson_path),
    )
    return geojson_path


# ── Primary entry point ────────────────────────────────────────────────────────
def generate_pmtiles(
    dataset_name: str,
    geojson_path: str | Path,
    output_path: str | Path,
    layer_name: str | None = None,
    extra_tippecanoe_args: list[str] | None = None,
    *,
    skip_integrity_check: bool = False,
) -> dict:
    """
    Generate PMTiles from Curated GeoJSON. validate → tile → verify → size-gate.

    Returns dict: {dataset, output_path, size_bytes, size_mb, verified, tippecanoe_version}.
    All HALT exceptions propagate uncaught → Airflow FAILED → Day 2 gate fires.
    """
    t_start = time.perf_counter()
    geojson_path = Path(geojson_path)
    output_path = Path(output_path)
    layer_name = layer_name or dataset_name
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not geojson_path.exists():
        raise TileGenerationError(
            f"GeoJSON input not found: {geojson_path}. "
            "Confirm Day 1 curated export completed."
        )

    if not skip_integrity_check:
        verify_binary_integrity()

    log.info(
        "pmtiles.generate.start",
        dataset=dataset_name,
        geojson=str(geojson_path),
        output=str(output_path),
        min_zoom=settings.TILE_MIN_ZOOM,
        max_zoom=settings.TILE_MAX_ZOOM,
        max_tile_bytes=settings.TILE_MAX_BYTES,
    )

    result = _run_tippecanoe(geojson_path, output_path, layer_name, extra_tippecanoe_args)

    if result.returncode != 0:
        raise TileGenerationError(
            f"tippecanoe exited {result.returncode} for '{dataset_name}'.\n"
            f"  stderr : {result.stderr[-2000:]}"
        )
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise TileGenerationError(
            f"tippecanoe exit 0 but output missing/empty: {output_path}."
        )

    log.info("tippecanoe.run.complete", dataset=dataset_name, returncode=0)

    size_bytes = _check_file_size(output_path, dataset_name)
    _verify_pmtiles(output_path)

    elapsed_ms = (time.perf_counter() - t_start) * 1000
    log.info(
        "pmtiles.generate.complete",
        dataset=dataset_name,
        output=str(output_path),
        size_mb=round(size_bytes / 1_000_000, 2),
        elapsed_ms=round(elapsed_ms, 0),
    )

    return {
        "dataset": dataset_name,
        "output_path": str(output_path),
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1_000_000, 2),
        "verified": True,
        "tippecanoe_version": settings.TIPPECANOE_VERSION,
        "min_zoom": settings.TILE_MIN_ZOOM,
        "max_zoom": settings.TILE_MAX_ZOOM,
    }


# ── Airflow task entry point (Section 25) ─────────────────────────────────────
def run_gold_pmtiles(
    dataset_name: str,
    parquet_path: str | Path,
    output_dir: str | Path,
) -> dict:
    """
    Full Gold PMTiles task: GeoParquet → GeoJSON → PMTiles → verify.
    Called by Airflow task_pmtiles in geo_pipeline_daily.py (Section 25).
    Returns generate_pmtiles() result dict for XCom push.
    GeoJSON intermediate cleaned post-verify (ephemeral, per ADR-011 spirit).
    """
    output_dir = Path(output_dir)
    geojson_path = output_dir / f"{dataset_name}.geojson"
    pmtiles_path = output_dir / f"{dataset_name}.pmtiles"

    export_curated_to_geojson(parquet_path, geojson_path, dataset_name)
    result = generate_pmtiles(dataset_name, geojson_path, pmtiles_path)

    geojson_path.unlink(missing_ok=True)
    log.info("pmtiles.geojson.cleaned", path=str(geojson_path))

    return result
