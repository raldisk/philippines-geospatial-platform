"""
geo_service/pipeline/gold/h3_aggregate.py

Gold layer: H3 hexagonal aggregation with dynamic resolution selection.

ADR-003 : H3 for Deck.gl animation layer (precomputed at Gold — never recomputed at serve time).
ADR-009 : Dynamic resolution — per-dataset occupancy validation, highest passing res wins.
Res 16.5: ≥70% hex occupancy, ≥2 features/hex; candidates (7, 6, 5) descending.
Sec  7.3: Jenks breaks computed once here, stored as JSON metadata in output DataFrame.

HALT surface: H3ResolutionError propagates uncaught → Airflow task FAILED → Day 2 gate fires.
"""

from __future__ import annotations

import json
import time
from typing import Final

import geopandas as gpd
import h3
import mapclassify
import pandas as pd
import structlog

from geo_service.config import settings
from geo_service.domain.exceptions import H3ResolutionError

log = structlog.get_logger()

# ── Constants (pulled from settings, overridable via env) ─────────────────────
CANDIDATE_RESOLUTIONS: Final[tuple[int, ...]] = settings.H3_CANDIDATE_RESOLUTIONS
MIN_OCCUPANCY_RATIO: Final[float] = settings.H3_MIN_OCCUPANCY_RATIO
MIN_FEATURES_PER_HEX: Final[int] = settings.H3_MIN_FEATURES_PER_HEX
JENKS_CLASSES: Final[int] = settings.JENKS_CLASSES


# ── Resolution Selector (ADR-009, Resolution 16.5) ────────────────────────────
def select_resolution(gdf: gpd.GeoDataFrame, dataset_name: str) -> int:
    """
    Returns highest H3 resolution satisfying occupancy thresholds.

    Iterates CANDIDATE_RESOLUTIONS (7, 6, 5) — finest first — returns on first pass.
    Raises H3ResolutionError if none pass → HALT surface. Do NOT catch silently.
    Selected value is persisted to geo_schema_registry.accepted_h3_resolution by DAG.
    """
    centroids = gdf.geometry.centroid

    for resolution in CANDIDATE_RESOLUTIONS:
        t0 = time.perf_counter()

        h3_indices = [
            h3.geo_to_h3(pt.y, pt.x, resolution)
            for pt in centroids
            if not pt.is_empty
        ]

        if not h3_indices:
            log.warning(
                "h3.resolution_candidate.no_centroids",
                dataset=dataset_name,
                resolution=resolution,
            )
            continue

        feature_counts: pd.Series = pd.Series(h3_indices).value_counts()
        total_hexes: int = len(set(h3_indices))
        occupied_hexes: int = len(feature_counts)
        multi_feature_hexes: int = int((feature_counts >= MIN_FEATURES_PER_HEX).sum())

        occupancy_ratio: float = occupied_hexes / total_hexes if total_hexes > 0 else 0.0
        multi_ratio: float = (
            multi_feature_hexes / occupied_hexes if occupied_hexes > 0 else 0.0
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        log.info(
            "h3.resolution_candidate",
            dataset=dataset_name,
            resolution=resolution,
            total_hexes=total_hexes,
            occupied_hexes=occupied_hexes,
            multi_feature_hexes=multi_feature_hexes,
            occupancy_ratio=round(occupancy_ratio, 4),
            multi_feature_ratio=round(multi_ratio, 4),
            passes_threshold=occupancy_ratio >= MIN_OCCUPANCY_RATIO,
            elapsed_ms=round(elapsed_ms, 1),
        )

        if occupancy_ratio >= MIN_OCCUPANCY_RATIO:
            log.info(
                "h3.resolution_selected",
                dataset=dataset_name,
                resolution=resolution,
                occupancy_ratio=round(occupancy_ratio, 4),
            )
            return resolution

    raise H3ResolutionError(
        f"No resolution in {CANDIDATE_RESOLUTIONS} satisfies "
        f"MIN_OCCUPANCY_RATIO={MIN_OCCUPANCY_RATIO} for dataset '{dataset_name}'. "
        "Manual review required — dataset may be geometry-only or island-sparse. "
        "Check day1_profile_report.json operation_mode before proceeding."
    )


# ── H3 Aggregator (Section 7.3) ────────────────────────────────────────────────
def aggregate_to_h3(
    gdf: gpd.GeoDataFrame,
    value_column: str,
    dataset_name: str,
    resolutions: tuple[int, ...] | None = None,
    *,
    auto_select: bool = True,
) -> dict[int, pd.DataFrame]:
    """
    Aggregate GeoDataFrame to H3 hexagonal grid with Jenks classification.

    Parameters
    ----------
    gdf           : Curated GeoDataFrame from Day 1 (EPSG:4326, valid geometries).
    value_column  : Numeric column to aggregate. Fails fast if non-numeric.
    dataset_name  : Used for logging and schema registry.
    resolutions   : Explicit resolution set. If None and auto_select=True,
                    select_resolution() picks single best resolution (ADR-009).
                    If None and auto_select=False, iterates all CANDIDATE_RESOLUTIONS.
    auto_select   : True (default) for production. False for debugging.

    Returns
    -------
    dict[int, pd.DataFrame]
        Keys: H3 resolution integers.
        Each DataFrame columns:
            h3_index            : H3 cell address (str)
            {value_column}_mean : Mean of value within hex (float)
            {value_column}_std  : Std dev within hex (float; NaN for single-feature hexes)
            feature_count       : Features mapped to hex (int)
            jenks_class         : 1–5 Jenks class (int; 0 = no data)
            jenks_breaks        : JSON-serialised break boundaries (str)
            resolution          : H3 resolution for this partition (int)

    Raises
    ------
    ValueError        : value_column missing or non-numeric.
    H3ResolutionError : auto_select=True, no resolution passes gate (HALT surface).
    """
    t_start = time.perf_counter()

    # ── Input validation ───────────────────────────────────────────────────────
    if value_column not in gdf.columns:
        raise ValueError(
            f"Column '{value_column}' not in GeoDataFrame. "
            f"Available: {list(gdf.columns)}. "
            "Set GEO_VALUE_COLUMN env var or check day1_profile_report.json."
        )
    if not pd.api.types.is_numeric_dtype(gdf[value_column]):
        raise ValueError(
            f"H3 aggregation requires numeric column. "
            f"Got dtype={gdf[value_column].dtype} for '{value_column}'. "
            "Check .dbf attribute types from Day 1 profile."
        )

    log.info(
        "h3.aggregate.start",
        dataset=dataset_name,
        value_column=value_column,
        input_features=len(gdf),
        auto_select=auto_select,
    )

    # ── Resolution selection ───────────────────────────────────────────────────
    if resolutions is not None:
        target_resolutions = resolutions
    elif auto_select:
        winning = select_resolution(gdf, dataset_name)
        target_resolutions = (winning,)
    else:
        target_resolutions = CANDIDATE_RESOLUTIONS

    results: dict[int, pd.DataFrame] = {}

    for resolution in target_resolutions:
        t_res = time.perf_counter()

        centroids = gdf.geometry.centroid
        working = gdf.copy()
        working["h3_index"] = [
            h3.geo_to_h3(pt.y, pt.x, resolution)
            for pt in centroids
            if not pt.is_empty
        ]

        aggregated: pd.DataFrame = (
            working.groupby("h3_index")[value_column]
            .agg(["mean", "count", "std"])
            .reset_index()
            .rename(
                columns={
                    "mean": f"{value_column}_mean",
                    "count": "feature_count",
                    "std": f"{value_column}_std",
                }
            )
        )
        aggregated["feature_count"] = aggregated["feature_count"].astype(int)
        aggregated["resolution"] = resolution

        # ── Jenks (precomputed once — Section 7.3 mandate) ─────────────────────
        valid_values = aggregated[f"{value_column}_mean"].dropna()

        k_actual = min(JENKS_CLASSES, max(2, len(valid_values)))
        if k_actual < JENKS_CLASSES:
            log.warning(
                "h3.jenks.fallback_k",
                dataset=dataset_name,
                resolution=resolution,
                valid_hexes=len(valid_values),
                requested_k=JENKS_CLASSES,
                actual_k=k_actual,
            )

        classifier = mapclassify.NaturalBreaks(valid_values, k=k_actual)
        aggregated.loc[valid_values.index, "jenks_class"] = classifier.yb
        aggregated["jenks_class"] = aggregated["jenks_class"].fillna(0).astype(int)

        # JSON string — single source of truth; loaded by /geo/v1/metadata/{dataset}
        breaks_json = json.dumps([round(float(b), 6) for b in classifier.bins.tolist()])
        aggregated["jenks_breaks"] = breaks_json

        elapsed_res_ms = (time.perf_counter() - t_res) * 1000
        log.info(
            "h3.aggregate.resolution_complete",
            dataset=dataset_name,
            resolution=resolution,
            hexes_output=len(aggregated),
            jenks_k=k_actual,
            breaks=breaks_json,
            elapsed_ms=round(elapsed_res_ms, 1),
        )

        results[resolution] = aggregated

    log.info(
        "h3.aggregate.complete",
        dataset=dataset_name,
        resolutions_produced=list(results.keys()),
        total_elapsed_ms=round((time.perf_counter() - t_start) * 1000, 1),
    )
    return results


# ── Airflow task entry point (Section 25) ─────────────────────────────────────
def run_gold_h3(
    gdf: gpd.GeoDataFrame,
    value_column: str,
    dataset_name: str,
) -> tuple[int, pd.DataFrame]:
    """
    Primary entry point for Airflow DAG task.
    Returns (selected_resolution, aggregated_df) for XCom push.
    H3ResolutionError propagates uncaught — Airflow marks FAILED → Day 2 gate.
    """
    results = aggregate_to_h3(
        gdf=gdf,
        value_column=value_column,
        dataset_name=dataset_name,
        auto_select=True,
    )
    assert len(results) == 1, "auto_select=True must yield exactly one resolution"
    resolution, df = next(iter(results.items()))
    return resolution, df
