"""
ADR-006: Two-Phase Inspection Before Bronze Write (Resolution 16.1, Section 17).

Run unconditionally before any Bronze write. Determines operation_mode per dataset:
  'analytical'       → numeric columns found — H3 + Jenks + Kimball fact
  'geometry_only'    → no numeric, no PSGC codes — boundary catalog, no fact table
  'boundary_catalog' → code columns only — dim_region SCD Type 2 load, no aggregation

HALT gate: if zero analytical datasets after full run, caller must not proceed.
Day 1 constraint: fiona-only inspection, no geopandas load (faster, avoids GEOS in loop).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

import fiona
import structlog

log = structlog.get_logger()

# BYOD-03: Target CRS for all output geometries.
# Default EPSG:4326 (WGS84 geographic). Override via TARGET_CRS env var.
# Philippine engineering surveys commonly use EPSG:32651 (UTM Zone 51N).
import os as _os
_TARGET_CRS: str = _os.environ.get("TARGET_CRS", "EPSG:4326")

OperationMode = Literal["analytical", "geometry_only", "boundary_catalog"]

# PSGC = Philippine Standard Geographic Code (9-digit admin codes).
# Column name patterns observed across PSA / NAMRIA / COMELEC shapefiles.
_PSGC_COL_RE: Final = re.compile(
    r"psgc"
    r"|geo_?code"
    r"|adm\d*_?p?code"
    r"|adm\d*_?code"
    r"|(reg|prov|mun|brgy|city)_?(code|id|no)"
    r"|province_?code"
    r"|municipality_?code"
    r"|barangay_?code"
    r"|region_?code"
    r"|reg_?code"
    r"|prov_?code",
    flags=re.IGNORECASE,
)

# Columns that look numeric but carry no analytical signal (shape metadata).
_NOISE_NUMERIC: Final[frozenset[str]] = frozenset(
    {"objectid", "fid", "shape_leng", "shape_length", "shape_area",
     "shape_len", "perimeter", "area", "gid", "id"}
)

# Columns excluded from categorical list entirely.
_EXCLUDE_CAT: Final[frozenset[str]] = frozenset(
    {"objectid", "fid", "shape_leng", "shape_length", "shape_area",
     "shape_len", "perimeter", "area", "gid", "id"}
)


@dataclass
class DatasetProfile:
    dataset_name: str
    archive_path: str
    archive_sha256: str
    shp_path: str
    feature_count: int
    geometry_type: str
    crs_epsg: int | None
    crs_wkt: str | None
    numeric_columns: list[str]
    categorical_columns: list[str]
    code_columns: list[str]   # PSGC-like columns
    operation_mode: OperationMode
    recommended_indicator: str | None  # highest-cardinality non-binary numeric column
    sample_values: dict[str, list]     # up to 5 samples per key column for QA
    fiona_schema: dict                 # raw fiona schema for audit trail

    # Mutable post-init fields
    inspection_error: str | None = field(default=None)


def profile_shapefile(
    shp_path: str,
    dataset_name: str,
    archive_path: str | None = None,
) -> DatasetProfile:
    """
    Mandatory pre-Bronze inspection (ADR-006).
    `archive_path` used for SHA-256 (the .7z); defaults to shp_path if absent.

    fiona type strings observed in practice:
      'int', 'int:10', 'float', 'float:15.6', 'str', 'str:254', 'date'
    Numeric detection splits on ':' and checks the base type.
    """
    archive_path = archive_path or shp_path
    archive_sha256 = _sha256(archive_path)

    log.info("inspector.start", dataset=dataset_name, shp=shp_path)

    with fiona.open(shp_path) as src:
        schema_props: dict[str, str] = src.schema["properties"]
        geometry_type: str = src.schema["geometry"]
        feature_count: int = len(src)

        # CRS — detect, warn on missing .prj, reproject to TARGET_CRS if needed
        try:
            crs_wkt = src.crs_wkt if hasattr(src, "crs_wkt") else None
            crs_epsg = fiona.crs.to_epsg(src.crs) if src.crs else None
        except Exception:
            crs_epsg, crs_wkt = None, None

        if crs_epsg is None:
            log.warning(
                "inspector.crs_missing",
                shp=shp_path,
                fallback=_TARGET_CRS,
                hint="No .prj file or unreadable CRS. Defaulting to TARGET_CRS env var.",
            )
            # Attempt to parse TARGET_CRS into an EPSG int for the profile
            try:
                _epsg_str = _TARGET_CRS.upper().replace("EPSG:", "")
                crs_epsg = int(_epsg_str)
            except ValueError:
                crs_epsg = None

        # Sample up to 1 000 features for value-level analysis
        sample_records: list[dict] = []
        for i, feat in enumerate(src):
            if i >= 1000:
                break
            sample_records.append(feat["properties"])

    # ── classify columns ────────────────────────────────────────────────────
    numeric_cols = [
        k for k, v in schema_props.items()
        if _is_numeric_type(v) and k.lower() not in _NOISE_NUMERIC
    ]
    cat_cols = [
        k for k, v in schema_props.items()
        if _is_string_type(v) and k.lower() not in _EXCLUDE_CAT
    ]
    code_cols = [k for k in cat_cols if _PSGC_COL_RE.search(k)]

    # ── operation mode ───────────────────────────────────────────────────────
    if numeric_cols:
        mode: OperationMode = "analytical"
        recommended = _pick_indicator(numeric_cols, sample_records)
    elif code_cols:
        mode = "boundary_catalog"
        recommended = None
    else:
        mode = "geometry_only"
        recommended = None

    # ── sample values (QA aid) ───────────────────────────────────────────────
    sample_values: dict[str, list] = {}
    if sample_records:
        for col in (numeric_cols[:5] + code_cols[:3]):
            vals = [r.get(col) for r in sample_records[:5] if r.get(col) is not None]
            sample_values[col] = vals

    profile = DatasetProfile(
        dataset_name=dataset_name,
        archive_path=str(archive_path),
        archive_sha256=archive_sha256,
        shp_path=str(shp_path),
        feature_count=feature_count,
        geometry_type=geometry_type,
        crs_epsg=crs_epsg,
        crs_wkt=crs_wkt,
        numeric_columns=numeric_cols,
        categorical_columns=cat_cols,
        code_columns=code_cols,
        operation_mode=mode,
        recommended_indicator=recommended,
        sample_values=sample_values,
        fiona_schema=dict(schema_props),
    )

    # BYOD-03: log if reprojection will be needed downstream (actual to_crs()
    # happens in silver/crs.py where geopandas is already loaded).
    if crs_epsg is not None and crs_epsg != 4326:
        log.info(
            "inspector.crs_reprojection_needed",
            dataset=dataset_name,
            source_epsg=crs_epsg,
            target_crs=_TARGET_CRS,
        )
    elif crs_epsg is None:
        log.warning(
            "inspector.crs_undefined",
            dataset=dataset_name,
            action=f"Silver layer will assume {_TARGET_CRS}",
        )

    log.info(
        "inspector.complete",
        dataset=dataset_name,
        mode=mode,
        feature_count=feature_count,
        numeric_columns=numeric_cols,
        recommended_indicator=recommended,
    )
    return profile


def run_inspection(
    targets: list[tuple[str, str, str]],  # (shp_path, dataset_name, archive_path)
    output_json: str = "day1_profile_report.json",
) -> tuple[list[DatasetProfile], bool]:
    """
    Run profile_shapefile() against every target. Write day1_profile_report.json.

    Returns (profiles, halt_condition).
    halt_condition=True  → zero analytical datasets → caller MUST NOT proceed to Bronze.
    halt_condition=False → ≥1 analytical dataset  → pipeline may proceed.
    """
    run_id = str(uuid.uuid4())
    profiles: list[DatasetProfile] = []

    for shp_path, dataset_name, archive_path in targets:
        try:
            p = profile_shapefile(shp_path, dataset_name, archive_path)
        except Exception as exc:
            log.error("inspector.failed", dataset=dataset_name, error=str(exc))
            p = DatasetProfile(
                dataset_name=dataset_name,
                archive_path=archive_path,
                archive_sha256="",
                shp_path=shp_path,
                feature_count=0,
                geometry_type="Unknown",
                crs_epsg=None,
                crs_wkt=None,
                numeric_columns=[],
                categorical_columns=[],
                code_columns=[],
                operation_mode="geometry_only",
                recommended_indicator=None,
                sample_values={},
                fiona_schema={},
                inspection_error=str(exc),
            )
        profiles.append(p)

    analytical_count = sum(1 for p in profiles if p.operation_mode == "analytical")
    halt = analytical_count == 0

    report = _build_report(profiles, run_id, analytical_count, halt)
    _write_report(report, output_json)

    if halt:
        log.error(
            "inspector.HALT",
            run_id=run_id,
            message="Zero analytical datasets. Per Day 1 Go/No-Go Gate: "
                    "do not proceed to Bronze. Pivot to geometry-only + external CSV join.",
        )
    else:
        log.info(
            "inspector.go",
            run_id=run_id,
            analytical_count=analytical_count,
            total=len(profiles),
        )

    return profiles, halt


# ── private helpers ────────────────────────────────────────────────────────────

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_numeric_type(fiona_type: str) -> bool:
    base = fiona_type.split(":")[0].strip().lower()
    return base in ("int", "float")


def _is_string_type(fiona_type: str) -> bool:
    base = fiona_type.split(":")[0].strip().lower()
    return base == "str"


def _pick_indicator(numeric_cols: list[str], sample_records: list[dict]) -> str | None:
    """
    Pick the highest-information numeric column for H3 aggregation.
    Strategy: max cardinality (unique value count) excluding binary flags (≤2 unique values).
    Falls back to first non-noise numeric column when all are binary.
    """
    if not sample_records:
        return numeric_cols[0] if numeric_cols else None

    cardinalities: dict[str, int] = {}
    for col in numeric_cols:
        vals = [r.get(col) for r in sample_records if r.get(col) is not None]
        cardinalities[col] = len(set(vals))

    non_binary = {k: v for k, v in cardinalities.items() if v > 2}
    if non_binary:
        return max(non_binary, key=non_binary.__getitem__)

    log.warning("inspector.all_numeric_binary", numeric_cols=numeric_cols,
                note="All numeric columns have ≤2 unique values in sample. Using first.")
    return numeric_cols[0]


def _build_report(
    profiles: list[DatasetProfile],
    run_id: str,
    analytical_count: int,
    halt: bool,
) -> dict:
    return {
        "inspection_run_id": run_id,
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "analytical_dataset_count": analytical_count,
        "halt_condition": halt,
        "halt_reason": (
            "Zero analytical datasets confirmed after full .dbf inspection. "
            "Pivot to geometry-only mode + external CSV join per Day 1 Go/No-Go Gate."
            if halt else None
        ),
        "datasets": [
            {
                "dataset_name": p.dataset_name,
                "archive_path": p.archive_path,
                "archive_sha256": p.archive_sha256,
                "shp_path": p.shp_path,
                "feature_count": p.feature_count,
                "geometry_type": p.geometry_type,
                "crs_epsg": p.crs_epsg,
                "numeric_columns": p.numeric_columns,
                "categorical_columns": p.categorical_columns,
                "code_columns": p.code_columns,
                "operation_mode": p.operation_mode,
                "recommended_indicator": p.recommended_indicator,
                "sample_values": p.sample_values,
                "fiona_schema": p.fiona_schema,
                "inspection_error": p.inspection_error,
            }
            for p in profiles
        ],
    }


def _write_report(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("inspector.report_written", path=path,
             run_id=report["inspection_run_id"],
             analytical_count=report["analytical_dataset_count"],
             halt=report["halt_condition"])
