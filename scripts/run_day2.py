#!/usr/bin/env python3
"""
run_day2.py

Day 2 execution runner — PH Geospatial Intelligence Platform v2.2.

Ties together all three Day 2 tasks:
  0–2h  Task A : H3 aggregation + Jenks classification
  2–5h  Task B : tippecanoe PMTiles generation + verification
  5–8h  Task C : Opens day2_tile_preview.html in browser (manual gate)

Usage:
    # Full Day 2 run (real PSA data):
    python run_day2.py

    # With synthetic fixture (no real .parquet required):
    python run_day2.py --fixture

    # Run only Task A (H3):
    python run_day2.py --task h3

    # Run only Task B (PMTiles):
    python run_day2.py --task pmtiles --geojson path/to/curated.geojson

    # Override value column (if .dbf uses different field name):
    GEO_VALUE_COLUMN=poverty_incidence python run_day2.py

HALT conditions (from master plan):
    - H3ResolutionError      → no resolution passes occupancy gate → gate FAILED
    - TileGenerationError    → tippecanoe failure → gate FAILED
    - PMTilesVerificationError → pmtiles verify fails → gate FAILED
    - FileSizeError          → output ≥ 50 MB → gate FAILED
    - Tile not visible in browser → manual gate FAILED
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

import geopandas as gpd

from geo_service.config import settings
from geo_service.domain.exceptions import (
    FileSizeError,
    H3ResolutionError,
    PMTilesVerificationError,
    TileGenerationError,
)
from geo_service.infra.logging_config import configure_logging
from geo_service.pipeline.gold.h3_aggregate import run_gold_h3
from geo_service.pipeline.gold.pmtiles import run_gold_pmtiles

import structlog

log = structlog.get_logger()


# ── Gate tracker ───────────────────────────────────────────────────────────────
GATES: dict[str, bool | None] = {
    "task_a_h3_jenks": None,
    "task_b_pmtiles_file": None,
    "task_b_pmtiles_verify": None,
    "task_b_size_under_50mb": None,
    "task_c_tile_visible": None,  # manual — runner prints prompt
}


def print_gate_report() -> None:
    print("\n" + "=" * 60)
    print("  DAY 2 GATE REPORT")
    print("=" * 60)
    all_pass = True
    for gate, status in GATES.items():
        if status is True:
            sym = "✓ PASS"
        elif status is False:
            sym = "✗ FAIL"
            all_pass = False
        else:
            sym = "— SKIP"
        print(f"  {sym}  {gate}")
    print("=" * 60)
    if all_pass:
        print("  ✓ Day 2 gate PASSED — proceed to Day 3.")
    else:
        print("  ✗ Day 2 gate FAILED — resolve FAIL items before Day 3.")
    print("=" * 60 + "\n")


# ── Task A: H3 aggregation ─────────────────────────────────────────────────────
def run_task_a(gdf: gpd.GeoDataFrame, dataset_name: str) -> tuple[int, object]:
    print("\n[Task A 0–2h] H3 aggregation + Jenks classification...")
    t0 = time.perf_counter()

    try:
        resolution, df = run_gold_h3(gdf, settings.VALUE_COLUMN, dataset_name)
    except H3ResolutionError as exc:
        log.error("task_a.h3_resolution_error", error=str(exc))
        GATES["task_a_h3_jenks"] = False
        print(f"\n  HALT: H3ResolutionError — {exc}")
        print("  Check day1_profile_report.json → operation_mode must not be 'geometry_only'.")
        raise

    elapsed = time.perf_counter() - t0
    GATES["task_a_h3_jenks"] = True

    print(f"\n  ✓ H3 aggregation complete in {elapsed:.1f}s")
    print(f"    resolution selected : {resolution}")
    print(f"    hexes output        : {len(df)}")
    print(f"    jenks_class range   : {df['jenks_class'].min()}–{df['jenks_class'].max()}")
    print(f"    jenks_breaks sample : {df['jenks_breaks'].iloc[0]}")

    # Save H3 output for inspection
    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    h3_output = output_dir / f"{dataset_name}_h3_r{resolution}.parquet"
    df.to_parquet(str(h3_output))
    print(f"    saved → {h3_output}")

    return resolution, df


# ── Task B: PMTiles generation ─────────────────────────────────────────────────
def run_task_b(
    gdf: gpd.GeoDataFrame | None,
    dataset_name: str,
    geojson_override: str | None = None,
) -> dict:
    print("\n[Task B 2–5h] tippecanoe PMTiles generation...")
    t0 = time.perf_counter()

    output_dir = settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        if geojson_override:
            # Direct GeoJSON path provided — skip GeoParquet step
            from geo_service.pipeline.gold.pmtiles import generate_pmtiles
            result = generate_pmtiles(
                dataset_name=dataset_name,
                geojson_path=geojson_override,
                output_path=output_dir / f"{dataset_name}.pmtiles",
            )
        elif gdf is not None:
            # Export GDF to GeoParquet, then run full pipeline
            parquet_path = output_dir / f"{dataset_name}_curated.parquet"
            gdf.to_parquet(str(parquet_path))
            result = run_gold_pmtiles(
                dataset_name=dataset_name,
                parquet_path=parquet_path,
                output_dir=output_dir,
            )
        else:
            # Use already-existing curated parquet from settings
            result = run_gold_pmtiles(
                dataset_name=dataset_name,
                parquet_path=settings.CURATED_PARQUET,
                output_dir=output_dir,
            )

    except TileGenerationError as exc:
        log.error("task_b.tippecanoe_failed", error=str(exc))
        GATES["task_b_pmtiles_file"] = False
        GATES["task_b_pmtiles_verify"] = False
        GATES["task_b_size_under_50mb"] = False
        print(f"\n  HALT: TileGenerationError — {exc}")
        raise

    except PMTilesVerificationError as exc:
        log.error("task_b.verify_failed", error=str(exc))
        GATES["task_b_pmtiles_file"] = True
        GATES["task_b_pmtiles_verify"] = False
        print(f"\n  HALT: PMTilesVerificationError — {exc}")
        raise

    except FileSizeError as exc:
        log.error("task_b.size_exceeded", error=str(exc))
        GATES["task_b_pmtiles_file"] = True
        GATES["task_b_pmtiles_verify"] = True
        GATES["task_b_size_under_50mb"] = False
        print(f"\n  HALT: FileSizeError — {exc}")
        raise

    elapsed = time.perf_counter() - t0
    GATES["task_b_pmtiles_file"] = True
    GATES["task_b_pmtiles_verify"] = True
    GATES["task_b_size_under_50mb"] = True

    print(f"\n  ✓ PMTiles generated in {elapsed:.1f}s")
    print(f"    output   : {result['output_path']}")
    print(f"    size     : {result['size_mb']:.2f} MB  (limit: 50 MB)")
    print(f"    verified : {result['verified']}")
    print(f"    zoom     : z{result['min_zoom']}→z{result['max_zoom']}")

    return result


# ── Task C: MapLibre GL render (manual gate) ───────────────────────────────────
def run_task_c(pmtiles_path: str) -> None:
    print("\n[Task C 5–8h] MapLibre GL tile preview...")

    preview_html = Path(__file__).parent / "day2_tile_preview.html"
    if not preview_html.exists():
        print(f"  WARNING: day2_tile_preview.html not found at {preview_html}")
        print("  Open manually and load your .pmtiles file.")
    else:
        print(f"\n  Opening {preview_html} in browser...")
        webbrowser.open(f"file://{preview_html.resolve()}")

    print(f"\n  Load this file in the browser:")
    print(f"    {pmtiles_path}")
    print()
    print("  Manual gate checklist:")
    print("    □  Status badge shows green (no red)")
    print("    □  Tile boundaries visible at zoom 4 (national overview)")
    print("    □  Tile boundaries visible at zoom 8 (provincial detail)")
    print("    □  Tile boundaries visible at zoom 12 (municipality detail)")
    print("    □  Jenks colour classes appear at zoom ≥ 7")
    print("    □  Zero errors in browser DevTools console (F12)")
    print()

    response = input("  Did the tile render correctly? (y/n): ").strip().lower()
    GATES["task_c_tile_visible"] = response == "y"

    if not GATES["task_c_tile_visible"]:
        print("\n  HALT: Tile not visible. Troubleshoot:")
        print("    1. Check source-layer name matches tippecanoe --layer value")
        print("    2. Run: pmtiles verify output.pmtiles")
        print("    3. Open DevTools → Network → filter 'pmtiles'")
        print("    4. Check MinIO CORS if loading via URL")


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Day 2 runner — PH Geospatial Platform v2.2")
    parser.add_argument("--fixture", action="store_true",
                        help="Use synthetic fixture instead of real PSA data")
    parser.add_argument("--task", choices=["h3", "pmtiles", "all"], default="all",
                        help="Run specific task only")
    parser.add_argument("--geojson", type=str, default=None,
                        help="Path to pre-existing GeoJSON (skips parquet step)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Skip browser open for Task C")
    args = parser.parse_args()

    configure_logging(log_level=settings.LOG_LEVEL, json_output=settings.LOG_JSON)
    dataset_name = settings.DATASET_NAME

    print("=" * 60)
    print("  PH Geospatial Intelligence Platform v2.2 — Day 2")
    print("=" * 60)
    print(f"  dataset    : {dataset_name}")
    print(f"  value_col  : {settings.VALUE_COLUMN}")
    print(f"  output_dir : {settings.OUTPUT_DIR}")
    print(f"  fixture    : {args.fixture}")
    print("=" * 60)

    # ── Load data ──────────────────────────────────────────────────────────────
    gdf = None
    if args.fixture or not Path(settings.CURATED_PARQUET).exists():
        print("\n  Loading synthetic fixture (no real data detected)...")
        from tests.fixtures.generate_fixture import make_fixture_gdf
        gdf = make_fixture_gdf()
        print(f"  Fixture: {len(gdf)} provinces, columns={list(gdf.columns)}")
    elif args.task != "pmtiles" or args.geojson is None:
        print(f"\n  Loading curated GeoParquet: {settings.CURATED_PARQUET}")
        gdf = gpd.read_parquet(str(settings.CURATED_PARQUET))
        print(f"  Loaded: {len(gdf)} features, columns={list(gdf.columns)}")

    # ── Run tasks ──────────────────────────────────────────────────────────────
    pmtiles_path = None

    try:
        if args.task in ("h3", "all"):
            run_task_a(gdf, dataset_name)

        if args.task in ("pmtiles", "all"):
            result = run_task_b(gdf, dataset_name, geojson_override=args.geojson)
            pmtiles_path = result["output_path"]

        if args.task == "all" and not args.no_browser and pmtiles_path:
            run_task_c(pmtiles_path)
        elif args.task == "all":
            GATES["task_c_tile_visible"] = None  # manual skip noted

    except (H3ResolutionError, TileGenerationError,
            PMTilesVerificationError, FileSizeError) as exc:
        log.error("day2.halt", error=type(exc).__name__, detail=str(exc)[:200])
        print_gate_report()
        return 1

    print_gate_report()

    # Save gate report to output dir
    settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = settings.OUTPUT_DIR / "day2_gate_report.json"
    report_path.write_text(json.dumps(GATES, indent=2))
    print(f"  Gate report saved → {report_path}")

    all_mandatory_passed = (
        GATES["task_a_h3_jenks"] is True
        and GATES["task_b_pmtiles_file"] is True
        and GATES["task_b_pmtiles_verify"] is True
        and GATES["task_b_size_under_50mb"] is True
    )
    return 0 if all_mandatory_passed else 1


if __name__ == "__main__":
    sys.exit(main())
