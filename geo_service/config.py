"""
geo_service/config.py

Environment-driven configuration. All env vars have local-dev defaults.
Override via shell export or .env file before running pipeline scripts.

FIX-09 (Day 7): TIPPECANOE_SHA256 placeholder replaced with os.getenv() injection.
Must be supplied via TIPPECANOE_SHA256 env var or GitHub Actions secret before CI.
See RUNBOOK.md §Secrets Management.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # ── Paths ──────────────────────────────────────────────────────────────────
    DATA_DIR: Path = field(
        default_factory=lambda: Path(os.environ.get("GEO_DATA_DIR", "data"))
    )
    OUTPUT_DIR: Path = field(
        default_factory=lambda: Path(os.environ.get("GEO_OUTPUT_DIR", "outputs/day2"))
    )
    CURATED_PARQUET: Path = field(
        default_factory=lambda: Path(
            os.environ.get("GEO_CURATED_PARQUET", "data/curated/curated.parquet")
        )
    )

    # ── tippecanoe / pmtiles binaries (ADR-008) ───────────────────────────────
    TIPPECANOE_BIN: str = field(
        default_factory=lambda: os.environ.get(
            "TIPPECANOE_BIN", "/usr/local/bin/tippecanoe"
        )
    )
    PMTILES_BIN: str = field(
        default_factory=lambda: os.environ.get("PMTILES_BIN", "/usr/local/bin/pmtiles")
    )

    # FIX-09: SHA-256 must be injected via env var or Docker/CI secret.
    # Not hardcoded. Not a placeholder. UNSET signals misconfiguration.
    # Obtain with: curl -fsSL <tippecanoe-url> | sha256sum
    # Inject via: export TIPPECANOE_SHA256=<hash>
    # CI:          GitHub secret TIPPECANOE_SHA256
    TIPPECANOE_VERSION: str = "2.67.0"
    TIPPECANOE_SHA256: str = field(
        default_factory=lambda: os.environ.get("TIPPECANOE_SHA256", "UNSET")
        # Must be injected via Docker secret or env before CI — see RUNBOOK.md §Secrets Management
    )
    TIPPECANOE_SKIP_SHA256: bool = field(
        default_factory=lambda: os.environ.get("TIPPECANOE_SKIP_SHA256", "0") == "1"
    )

    # ── H3 / Jenks (ADR-009, Section 7.3) ────────────────────────────────────
    H3_MIN_OCCUPANCY_RATIO: float = 0.70
    H3_MIN_FEATURES_PER_HEX: int = 2
    H3_CANDIDATE_RESOLUTIONS: tuple[int, ...] = (7, 6, 5)
    JENKS_CLASSES: int = 5

    # ── tippecanoe tile parameters ────────────────────────────────────────────
    TILE_MIN_ZOOM: int = 4
    TILE_MAX_ZOOM: int = 12
    TILE_MAX_BYTES: int = 500_000
    TILE_MAX_OUTPUT_MB: float = 50.0

    # ── Value column ──────────────────────────────────────────────────────────
    VALUE_COLUMN: str = field(
        default_factory=lambda: os.environ.get("GEO_VALUE_COLUMN", "poverty_rate")
    )

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = field(
        default_factory=lambda: os.environ.get("GEO_LOG_LEVEL", "INFO")
    )
    LOG_JSON: bool = field(
        default_factory=lambda: os.environ.get("GEO_LOG_JSON", "0") == "1"
    )

    # ── Dataset name ──────────────────────────────────────────────────────────
    DATASET_NAME: str = field(
        default_factory=lambda: os.environ.get(
            "GEO_DATASET_NAME", "psa_provincial_2023"
        )
    )

    # ── MinIO / S3 (BYOD-01) ─────────────────────────────────────────────────
    MINIO_ENDPOINT: str = field(
        default_factory=lambda: os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
    )
    MINIO_BUCKET: str = field(
        default_factory=lambda: os.environ.get("MINIO_BUCKET", "geospatial-archives")
    )
    MINIO_ARCHIVE_PREFIX: str = field(
        default_factory=lambda: os.environ.get("MINIO_ARCHIVE_PREFIX", "uploads/")
    )
    # S3 bucket names for each medallion layer (BYOD-01: not hardcoded in DAG)
    BRONZE_BUCKET: str = field(
        default_factory=lambda: os.environ.get("GEO_BRONZE_BUCKET", "geo-bronze")
    )
    SILVER_TEMP_BUCKET: str = field(
        default_factory=lambda: os.environ.get("GEO_SILVER_TEMP_BUCKET", "geo-silver-tmp")
    )
    GOLD_BUCKET: str = field(
        default_factory=lambda: os.environ.get("GEO_GOLD_BUCKET", "geo-gold")
    )

    # ── Archive staging ───────────────────────────────────────────────────────
    ARCHIVE_LOCAL_STAGING: str = field(
        default_factory=lambda: os.environ.get("ARCHIVE_LOCAL_STAGING", "/tmp/geo_staging")
    )

    # ── CRS / projection (BYOD-03) ────────────────────────────────────────────
    # Used as fallback when shapefile has no .prj or CRS is unreadable.
    # EPSG:4326 is the universal default; Philippine engineering surveys use 32651.
    TARGET_CRS: str = field(
        default_factory=lambda: os.environ.get("TARGET_CRS", "EPSG:4326")
    )

    # ── Column name mapping (BYOD — non-PSA shapefiles) ──────────────────────
    # JSON object mapping canonical column roles to actual column names in the
    # user's shapefile.  Example:
    #   REGION_COLUMN_MAP='{"region":"NAME_1","province":"NAME_2","city":"NAME_3","barangay":"NAME_4"}'
    # Default: PSA/NAMRIA ADM column names.
    REGION_COLUMN_MAP: str = field(
        default_factory=lambda: os.environ.get(
            "REGION_COLUMN_MAP",
            '{"region":"ADM1_EN","province":"ADM2_EN","city":"ADM3_EN","barangay":"ADM4_EN"}'
        )
    )

    # ── Layer filter (BYOD — multi-shapefile archives) ────────────────────────
    # Comma-separated shapefile stem names to ingest when an archive contains
    # multiple .shp files.  Empty string = ingest all.
    # Example: LAYER_FILTER="Provinces,Municipalities"
    LAYER_FILTER: str = field(
        default_factory=lambda: os.environ.get("LAYER_FILTER", "")
    )

    # ── Silver worker limits (BYOD-06 / memory safety) ───────────────────────
    SILVER_MAX_WORKERS: int = field(
        default_factory=lambda: int(os.environ.get("SILVER_MAX_WORKERS", "4"))
    )
    SILVER_MEMORY_LIMIT_MB: int = field(
        default_factory=lambda: int(os.environ.get("SILVER_MEMORY_LIMIT_MB", "4096"))
    )


# Singleton
settings = Settings()
