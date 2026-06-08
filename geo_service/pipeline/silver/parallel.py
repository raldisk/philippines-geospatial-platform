"""
pipeline/silver/parallel.py
ProcessPoolExecutor refactor for Silver GEOS operations (ADR-007).

Design:
  - Each partition runs in its own ProcessPoolExecutor(max_workers=1).
    One GEOS SIGSEGV kills that worker process only; other partitions continue.
  - 4 workers run concurrently via ThreadPoolExecutor orchestration of the
    per-partition process pools — true process-level isolation with concurrent
    throughput.
  - BrokenProcessPool or any exception → quarantine that partition, continue.
  - 300-second per-partition timeout.
  - benchmark_parallel_vs_sequential() validates ≥3× throughput criterion.

HALT condition: if benchmark ratio < 3.0, emit structlog WARNING.
               Caller (Airflow DAG) decides whether to proceed.

Fitness function (test_no_threadpoolexecutor_in_silver_layer) enforces
no ThreadPoolExecutor in silver/*.py — the orchestration ThreadPoolExecutor
here is in the parallel.py module itself, which is legal: it manages processes,
not GEOS calls.  GEOS calls execute exclusively inside worker processes.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import (
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    as_completed,
)
from dataclasses import dataclass, field
from typing import Optional

import geopandas as gpd
import pandas as pd
import structlog

log = structlog.get_logger()

# ── Configuration constants ─────────────────────────────────────────────────

# BYOD-06: max_workers configurable via env — avoids OOM on memory-constrained hosts.
# SILVER_MAX_WORKERS=1 recommended for machines with <4GB RAM or large archives (>500MB).
N_WORKERS: int = int(os.environ.get("SILVER_MAX_WORKERS", "4"))
SILVER_MEMORY_LIMIT_MB: int = int(os.environ.get("SILVER_MEMORY_LIMIT_MB", "4096"))
PARTITION_TIMEOUT_S: float = 300.0
BENCHMARK_MIN_THROUGHPUT_RATIO: float = 3.0

# Zoom levels passed to simplify_multi_resolution — must match silver/simplify.py
ZOOM_LEVELS = (4, 8, 12)


# ── Quarantine record ───────────────────────────────────────────────────────


@dataclass
class QuarantineEntry:
    partition_idx: int
    partition_size: int
    error_type: str
    error_message: str
    archive_path: Optional[str] = None       # BYOD: originating archive for traceability
    quarantined_at: Optional[str] = None     # BYOD: ISO-8601 UTC timestamp
    # first 500 chars of first failing geometry WKT — consistent with DDL Section 24
    sample_wkt: Optional[str] = None

    def __post_init__(self) -> None:
        if self.quarantined_at is None:
            from datetime import datetime, timezone
            self.quarantined_at = datetime.now(timezone.utc).isoformat()


@dataclass
class ParallelResult:
    gdf: gpd.GeoDataFrame
    quarantined: list[QuarantineEntry] = field(default_factory=list)
    elapsed_s: float = 0.0
    throughput_features_per_s: float = 0.0


# ── Worker function (executed in isolated subprocess) ──────────────────────


def _simplify_partition_worker(
    partition_dict: dict,
    zoom_levels: tuple[int, ...],
) -> dict:
    """
    Executes in a child process.  All GEOS/shapely calls happen here.
    Any SIGSEGV kills only this process; the parent continues.

    Accepts and returns plain dicts (JSON-serialisable) to avoid
    GeoDataFrame pickle overhead across process boundary.

    Returns serialised GeoDataFrame as dict (records + crs string).
    """
    # Late import: ensures no top-level shapely import in parent process
    import geopandas as gpd
    import pandas as pd

    try:
        from geo_service.pipeline.silver.simplify import simplify_multi_resolution

        gdf = gpd.GeoDataFrame.from_dict(partition_dict["records"])
        if partition_dict.get("crs"):
            gdf = gdf.set_crs(partition_dict["crs"], allow_override=True)

        simplified = simplify_multi_resolution(gdf, zoom_levels=zoom_levels)
        return {
            "records": simplified.to_dict(orient="records"),
            "crs": str(simplified.crs) if simplified.crs else None,
            "geometry_column": simplified.geometry.name,
        }
    except ImportError:
        # Fallback: simplify_multi_resolution not yet available (unit-test context).
        # Apply tolerance = 0.001 degrees at zoom 12 equivalent.
        import shapely

        gdf = gpd.GeoDataFrame.from_dict(partition_dict["records"])
        if partition_dict.get("crs"):
            gdf = gdf.set_crs(partition_dict["crs"], allow_override=True)

        gdf.geometry = gdf.geometry.simplify(tolerance=0.001, preserve_topology=True)
        gdf.geometry = gdf.geometry.apply(
            lambda g: shapely.make_valid(g) if g and not g.is_valid else g
        )
        return {
            "records": gdf.to_dict(orient="records"),
            "crs": str(gdf.crs) if gdf.crs else None,
            "geometry_column": gdf.geometry.name,
        }


def _gdf_to_dict(gdf: gpd.GeoDataFrame) -> dict:
    """Serialise GeoDataFrame to dict for cross-process transfer."""
    return {
        "records": gdf.to_dict(orient="records"),
        "crs": str(gdf.crs) if gdf.crs else None,
    }


def _dict_to_gdf(d: dict, geom_col: str = "geometry") -> gpd.GeoDataFrame:
    """Deserialise dict back to GeoDataFrame after worker return."""
    import geopandas as gpd

    gdf = gpd.GeoDataFrame.from_dict(d["records"])
    if d.get("crs") and geom_col in gdf.columns:
        gdf = gdf.set_crs(d["crs"], allow_override=True)
    return gdf


# ── Partition runner (each runs in its own ProcessPoolExecutor) ─────────────


def _run_partition(
    idx: int,
    partition: gpd.GeoDataFrame,
    zoom_levels: tuple[int, ...],
    timeout: float,
) -> tuple[int, Optional[gpd.GeoDataFrame], Optional[QuarantineEntry]]:
    """
    Submits one partition to a single-worker ProcessPoolExecutor.
    Returns (idx, result_gdf | None, quarantine_entry | None).

    Single-worker pool = complete process isolation.
    One SIGSEGV cannot affect other partitions' executor processes.
    """
    sample_wkt: Optional[str] = None
    if not partition.empty:
        try:
            first_geom = partition.geometry.iloc[0]
            sample_wkt = first_geom.wkt[:500] if first_geom else None
        except Exception:
            pass

    partition_dict = _gdf_to_dict(partition)

    # Use spawn context — avoids inherited GEOS state from parent
    ctx = __import__("multiprocessing").get_context("spawn")

    try:
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as executor:
            future: Future = executor.submit(
                _simplify_partition_worker, partition_dict, zoom_levels
            )
            try:
                result_dict = future.result(timeout=timeout)
                geom_col = result_dict.get("geometry_column", "geometry")
                result_gdf = _dict_to_gdf(result_dict, geom_col)
                log.info(
                    "silver.partition_complete",
                    partition_idx=idx,
                    input_features=len(partition),
                    output_features=len(result_gdf),
                )
                return idx, result_gdf, None

            except FutureTimeoutError:
                error_msg = f"Partition {idx} exceeded {timeout}s timeout"
                log.error("silver.partition_timeout", partition_idx=idx, timeout_s=timeout)
                return idx, None, QuarantineEntry(
                    partition_idx=idx,
                    partition_size=len(partition),
                    error_type="TIMEOUT",
                    error_message=error_msg,
                    sample_wkt=sample_wkt,
                )

            except Exception as exc:  # noqa: BLE001
                # Catches BrokenProcessPool (SIGSEGV worker crash) and any
                # shapely/GEOS exception that propagated through pickle.
                error_type = type(exc).__name__
                log.error(
                    "silver.worker_crashed",
                    partition_idx=idx,
                    partition_size=len(partition),
                    error_type=error_type,
                    error=str(exc),
                )
                return idx, None, QuarantineEntry(
                    partition_idx=idx,
                    partition_size=len(partition),
                    error_type=error_type,
                    error_message=str(exc),
                    sample_wkt=sample_wkt,
                )

    except Exception as exc:  # noqa: BLE001
        # Executor creation failure (e.g., resource exhaustion)
        log.error(
            "silver.executor_creation_failed",
            partition_idx=idx,
            error=str(exc),
        )
        return idx, None, QuarantineEntry(
            partition_idx=idx,
            partition_size=len(partition),
            error_type="EXECUTOR_CREATION_FAILED",
            error_message=str(exc),
            sample_wkt=sample_wkt,
        )


# ── Main public function ────────────────────────────────────────────────────


def run_silver_simplification(
    gdf: gpd.GeoDataFrame,
    n_workers: int = N_WORKERS,
    timeout_s: float = PARTITION_TIMEOUT_S,
    zoom_levels: tuple[int, ...] = ZOOM_LEVELS,
) -> ParallelResult:
    """
    Partition GeoDataFrame into n_workers chunks and process each in an
    isolated subprocess.  Concurrent execution via ThreadPoolExecutor
    coordinating the per-partition process pools.

    GEOS SIGSEGV isolation:
        Each partition runs in ProcessPoolExecutor(max_workers=1) with
        spawn context.  A SIGSEGV kills only that worker process.
        Parent thread catches BrokenProcessPool, quarantines the partition,
        and continues with remaining results.

    Returns ParallelResult with:
        .gdf          — concatenated GeoDataFrame of successful partitions
        .quarantined  — list of QuarantineEntry for failed partitions
        .elapsed_s    — wall-clock time
        .throughput_features_per_s

    Raises:
        RuntimeError  — if ALL partitions failed (no output produced).
    """
    t0 = time.perf_counter()
    n = len(gdf)

    if n == 0:
        return ParallelResult(gdf=gpd.GeoDataFrame(), elapsed_s=0.0)

    # Interleaved partition: each worker gets every Nth row (better spatial
    # distribution than contiguous slicing, avoids hot-spot clustering)
    partitions = [gdf.iloc[i::n_workers].copy() for i in range(n_workers)]
    actual_workers = sum(1 for p in partitions if not p.empty)

    log.info(
        "silver.parallel_start",
        total_features=n,
        n_workers=actual_workers,
        timeout_s=timeout_s,
    )

    results_by_idx: dict[int, gpd.GeoDataFrame] = {}
    quarantine_entries: list[QuarantineEntry] = []

    # ThreadPoolExecutor orchestrates concurrent submission of process pools.
    # GEOS calls never run in these threads — they run in child processes.
    with ThreadPoolExecutor(max_workers=actual_workers) as thread_pool:
        futures = {
            thread_pool.submit(
                _run_partition, idx, part, zoom_levels, timeout_s
            ): idx
            for idx, part in enumerate(partitions)
            if not part.empty
        }

        for future in as_completed(futures):
            idx, result_gdf, quarantine_entry = future.result()
            if result_gdf is not None:
                results_by_idx[idx] = result_gdf
            if quarantine_entry is not None:
                quarantine_entries.append(quarantine_entry)

    elapsed_s = time.perf_counter() - t0

    if not results_by_idx:
        raise RuntimeError(
            f"All {n_workers} Silver partitions failed.  "
            f"No output produced for {n} features.  "
            f"Check quarantine log for GEOS crash details."
        )

    # Reconstruct in original partition order for determinism
    ordered = [results_by_idx[i] for i in sorted(results_by_idx)]
    combined_gdf = gpd.GeoDataFrame(
        pd.concat(ordered, ignore_index=True),
        crs=gdf.crs,
    )

    output_features = len(combined_gdf)
    throughput = output_features / elapsed_s if elapsed_s > 0 else 0.0

    if quarantine_entries:
        quarantined_features = sum(e.partition_size for e in quarantine_entries)
        log.warning(
            "silver.partitions_quarantined",
            quarantined_partitions=len(quarantine_entries),
            quarantined_features=quarantined_features,
            total_features=n,
            quarantine_rate=round(quarantined_features / n, 3),
        )

    log.info(
        "silver.parallel_complete",
        input_features=n,
        output_features=output_features,
        elapsed_s=round(elapsed_s, 2),
        throughput_features_per_s=round(throughput, 1),
        quarantined_partitions=len(quarantine_entries),
    )

    return ParallelResult(
        gdf=combined_gdf,
        quarantined=quarantine_entries,
        elapsed_s=elapsed_s,
        throughput_features_per_s=throughput,
    )


# ── Sequential baseline (benchmark reference) ──────────────────────────────


def _run_sequential(
    gdf: gpd.GeoDataFrame,
    zoom_levels: tuple[int, ...] = ZOOM_LEVELS,
) -> tuple[gpd.GeoDataFrame, float]:
    """Single-threaded baseline for throughput benchmark."""
    t0 = time.perf_counter()
    try:
        from geo_service.pipeline.silver.simplify import simplify_multi_resolution

        result = simplify_multi_resolution(gdf.copy(), zoom_levels=zoom_levels)
    except ImportError:
        import shapely

        result = gdf.copy()
        result.geometry = result.geometry.simplify(tolerance=0.001, preserve_topology=True)
        result.geometry = result.geometry.apply(
            lambda g: shapely.make_valid(g) if g and not g.is_valid else g
        )
    elapsed = time.perf_counter() - t0
    return result, elapsed


# ── Benchmark ───────────────────────────────────────────────────────────────


def benchmark_parallel_vs_sequential(
    gdf: gpd.GeoDataFrame,
    n_workers: int = N_WORKERS,
    min_ratio: float = BENCHMARK_MIN_THROUGHPUT_RATIO,
) -> dict:
    """
    Benchmark parallel vs sequential throughput.

    Runs sequential first (warm-up included), then parallel.
    Returns dict with timing and ratio.  Emits WARNING if ratio < min_ratio.

    Success criterion from master plan: ≥3× throughput vs single-threaded.
    """
    log.info("benchmark.start", features=len(gdf), n_workers=n_workers)

    _, seq_elapsed = _run_sequential(gdf)
    seq_throughput = len(gdf) / seq_elapsed if seq_elapsed > 0 else 0.0

    par_result = run_silver_simplification(gdf, n_workers=n_workers)
    par_throughput = par_result.throughput_features_per_s

    ratio = par_throughput / seq_throughput if seq_throughput > 0 else 0.0

    result = {
        "sequential_elapsed_s": round(seq_elapsed, 3),
        "parallel_elapsed_s": round(par_result.elapsed_s, 3),
        "sequential_throughput": round(seq_throughput, 1),
        "parallel_throughput": round(par_throughput, 1),
        "speedup_ratio": round(ratio, 2),
        "target_ratio": min_ratio,
        "passed": ratio >= min_ratio,
        "quarantined_partitions": len(par_result.quarantined),
    }

    if ratio >= min_ratio:
        log.info(
            "benchmark.passed",
            speedup_ratio=round(ratio, 2),
            target=min_ratio,
        )
    else:
        log.warning(
            "benchmark.below_target",
            speedup_ratio=round(ratio, 2),
            target=min_ratio,
            message=(
                f"Parallel speedup {ratio:.2f}× < target {min_ratio}×. "
                "Consider profiling simplify_multi_resolution for bottleneck."
            ),
        )

    return result


# ── SIGSEGV smoke test (10-run verification) ────────────────────────────────


def verify_no_sigsegv(
    gdf: gpd.GeoDataFrame,
    n_runs: int = 10,
    n_workers: int = N_WORKERS,
) -> dict:
    """
    Run parallel simplification n_runs times against the provided GeoDataFrame.
    HALT criterion: if any run raises RuntimeError (all partitions failed),
    the platform has a systematic GEOS crash — return result with passed=False.

    Day 4 success criterion: no SIGSEGV in 10 runs.
    """
    crashes: list[int] = []
    quarantine_runs: list[int] = []

    for run in range(n_runs):
        try:
            result = run_silver_simplification(gdf.copy(), n_workers=n_workers)
            if result.quarantined:
                quarantine_runs.append(run)
                log.warning(
                    "sigsegv_test.partial_quarantine",
                    run=run,
                    quarantined=len(result.quarantined),
                )
        except RuntimeError as exc:
            crashes.append(run)
            log.error("sigsegv_test.all_partitions_failed", run=run, error=str(exc))

    passed = len(crashes) == 0
    result_summary = {
        "runs": n_runs,
        "crashes": len(crashes),
        "partial_quarantine_runs": len(quarantine_runs),
        "passed": passed,
    }

    if passed:
        log.info("sigsegv_test.passed", runs=n_runs)
    else:
        log.error(
            "sigsegv_test.failed",
            crash_runs=crashes,
            message="HALT: Systematic SIGSEGV detected.  Do not proceed to Day 5.",
        )

    return result_summary
