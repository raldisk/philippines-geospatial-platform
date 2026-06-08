"""
geo_service/pipeline/extract/archive.py

BYOD archive discovery and extraction (FIX-BYOD-01, FIX-BYOD-02).

Responsibilities:
  1. find_new_archives() — discover .7z/.zip archives in MinIO uploads/ prefix
     that are not yet recorded in geo_ingest_registry (SHA-256 dedup).
  2. extract_archive()   — extract to local staging dir; recursively locate all
     .shp files; skip non-geospatial files (PDF, XML, DOCX, TXT, etc.).
  3. Multi-shapefile archives: return ALL .shp paths, caller selects via LAYER_FILTER.

Design principles:
  - Data-agnostic: no assumption about internal archive structure.
  - Recursive discovery: PSA archives may nest under Boundaries/, SHP/, or flat.
  - Graceful: missing .prj does not crash; non-geo files are silently skipped.
  - Config-driven: staging dir, bucket, endpoint all from settings/env.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()

# ── Geospatial extensions that the pipeline can process ───────────────────────
_GEO_EXTENSIONS: frozenset[str] = frozenset(
    {".shp", ".shx", ".dbf", ".prj", ".cpg", ".geojson", ".parquet"}
)

# ── Extensions to silently skip (docs, metadata bundled in PSA archives) ─────
_SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".xml", ".docx", ".doc", ".txt", ".html", ".htm",
     ".xlsx", ".csv", ".json", ".md", ".readme", ".log"}
)


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class DiscoveredArchive:
    """One .7z/.zip archive located in MinIO and staged locally."""
    archive_path: Path          # local path to downloaded .7z/.zip
    archive_hash: str           # SHA-256 of the archive file
    dataset_name: str           # stem of the archive filename (e.g. "psa_provincial_2024")
    shp_paths: list[Path] = field(default_factory=list)  # recursively found .shp files
    skipped_files: list[str] = field(default_factory=list)  # non-geo files ignored


# ── SHA-256 ────────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Non-geo file detection ─────────────────────────────────────────────────────

def _is_non_geo(path: Path) -> bool:
    """True if file should be skipped during extraction scan."""
    return path.suffix.lower() in _SKIP_EXTENSIONS


# ── 7z / zip extraction ────────────────────────────────────────────────────────

def _extract_7z(archive_path: Path, dest_dir: Path) -> None:
    """Extract .7z archive using py7zr (preferred) or subprocess fallback."""
    try:
        import py7zr
        with py7zr.SevenZipFile(str(archive_path), mode="r") as z:
            z.extractall(path=str(dest_dir))
        log.info("archive.7z_extracted", archive=archive_path.name, dest=str(dest_dir))
    except ImportError:
        # py7zr not installed — fall back to system 7z binary
        import subprocess
        result = subprocess.run(
            ["7z", "x", str(archive_path), f"-o{dest_dir}", "-y"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"7z extraction failed for {archive_path.name}: {result.stderr}"
            )
        log.info("archive.7z_extracted_via_binary", archive=archive_path.name)


def _extract_zip(archive_path: Path, dest_dir: Path) -> None:
    """Extract .zip archive using stdlib zipfile."""
    import zipfile
    with zipfile.ZipFile(str(archive_path), "r") as z:
        z.extractall(path=str(dest_dir))
    log.info("archive.zip_extracted", archive=archive_path.name, dest=str(dest_dir))


def extract_archive(archive_path: Path, staging_dir: Optional[Path] = None) -> DiscoveredArchive:
    """
    Extract archive to a unique staging subdirectory.

    Recursively locates all .shp files inside (BYOD-02: nested folder support).
    Non-geospatial files (PDF, XML, README, etc.) are logged and skipped.
    Multiple .shp files in one archive are all returned (caller applies LAYER_FILTER).

    Args:
        archive_path: local path to .7z or .zip file.
        staging_dir:  parent staging dir; defaults to ARCHIVE_LOCAL_STAGING env var
                      or /tmp/geo_staging.

    Returns:
        DiscoveredArchive with all found .shp paths.
    """
    if staging_dir is None:
        staging_root = Path(os.environ.get("ARCHIVE_LOCAL_STAGING", "/tmp/geo_staging"))
    else:
        staging_root = staging_dir

    staging_root.mkdir(parents=True, exist_ok=True)

    # Unique extraction dir per archive (prevents cross-run collisions)
    archive_hash = _sha256(archive_path)
    dest_dir = staging_root / f"{archive_path.stem}_{archive_hash[:12]}"
    dest_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "archive.extracting",
        archive=archive_path.name,
        hash_prefix=archive_hash[:12],
        dest=str(dest_dir),
    )

    suffix = archive_path.suffix.lower()
    if suffix == ".7z":
        _extract_7z(archive_path, dest_dir)
    elif suffix in (".zip", ".gz"):
        _extract_zip(archive_path, dest_dir)
    else:
        raise ValueError(
            f"Unsupported archive format: {archive_path.suffix}. "
            "Supported: .7z, .zip"
        )

    # ── Recursive .shp discovery (BYOD-02) ────────────────────────────────────
    shp_paths: list[Path] = []
    skipped: list[str] = []

    for file in sorted(dest_dir.rglob("*")):
        if not file.is_file():
            continue
        ext = file.suffix.lower()
        if ext == ".shp":
            shp_paths.append(file)
            log.info("archive.shp_found", shp=str(file.relative_to(dest_dir)))
        elif _is_non_geo(file):
            log.info(
                "archive.non_geo_skipped",
                file=file.name,
                reason=f"extension {ext!r} not in geospatial set",
            )
            skipped.append(file.name)
        elif ext not in _GEO_EXTENSIONS:
            log.debug("archive.unknown_ext_skipped", file=file.name, ext=ext)
            skipped.append(file.name)

    if not shp_paths:
        log.warning(
            "archive.no_shp_found",
            archive=archive_path.name,
            dest=str(dest_dir),
            hint="Archive may contain GeoJSON or GeoParquet directly — check dest_dir manually.",
        )

    log.info(
        "archive.extraction_complete",
        archive=archive_path.name,
        shp_count=len(shp_paths),
        skipped_count=len(skipped),
    )

    return DiscoveredArchive(
        archive_path=archive_path,
        archive_hash=archive_hash,
        dataset_name=archive_path.stem,
        shp_paths=shp_paths,
        skipped_files=skipped,
    )


# ── MinIO discovery ────────────────────────────────────────────────────────────

async def _list_minio_archives(bucket: str, prefix: str, endpoint: str) -> list[str]:
    """
    List all .7z/.zip object keys in MinIO bucket under prefix.
    Returns list of object keys (not full URLs).
    """
    try:
        import boto3
        from botocore.config import Config as BotoConfig
        from geo_service.infra.secrets import get_minio_access_key, get_minio_secret_key

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=get_minio_access_key(),
            aws_secret_access_key=get_minio_secret_key(),
            config=BotoConfig(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
        )
        paginator = s3.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if key.endswith(".7z") or key.endswith(".zip"):
                    keys.append(key)
        log.info("archive.minio_listed", bucket=bucket, prefix=prefix, count=len(keys))
        return keys
    except ImportError:
        log.error("archive.boto3_missing", hint="pip install boto3")
        raise


async def _download_archive(
    key: str,
    bucket: str,
    endpoint: str,
    staging_dir: Path,
) -> Path:
    """Download a MinIO object to local staging dir. Returns local path."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
        from geo_service.infra.secrets import get_minio_access_key, get_minio_secret_key

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=get_minio_access_key(),
            aws_secret_access_key=get_minio_secret_key(),
            config=BotoConfig(connect_timeout=5, read_timeout=120, retries={"max_attempts": 2}),
        )
        local_path = staging_dir / Path(key).name
        staging_dir.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, key, str(local_path))
        log.info("archive.downloaded", key=key, local=str(local_path))
        return local_path
    except ImportError:
        log.error("archive.boto3_missing")
        raise


async def _known_hashes(dsn: str) -> set[str]:
    """
    Return set of archive_hash values already in geo_ingest_registry (PostgreSQL).
    Falls back to empty set if PostgreSQL is unavailable (first-run case).
    """
    try:
        import asyncpg
        conn = await asyncpg.connect(dsn, timeout=5)
        rows = await conn.fetch(
            "SELECT archive_hash FROM geo_ingest_registry WHERE status != 'FAILED'"
        )
        await conn.close()
        return {r["archive_hash"] for r in rows}
    except Exception as exc:
        log.warning(
            "archive.registry_unavailable",
            error=str(exc),
            hint="Treating all archives as new — PostgreSQL may not be initialized yet.",
        )
        return set()


# ── Public entry point (called by DAG) ────────────────────────────────────────

async def find_new_archives(
    dsn: str,
    bucket: str,
    prefix: Optional[str] = None,
    endpoint: Optional[str] = None,
    staging_dir: Optional[Path] = None,
) -> list[DiscoveredArchive]:
    """
    Discover, download, and extract archives from MinIO that are not yet in
    geo_ingest_registry.

    Args:
        dsn:         PostgreSQL DSN for registry dedup check.
        bucket:      MinIO bucket name (e.g. "geo-uploads").
        prefix:      Object key prefix to scan (defaults to MINIO_ARCHIVE_PREFIX env var).
        endpoint:    MinIO endpoint URL (defaults to MINIO_ENDPOINT env var).
        staging_dir: Local staging path (defaults to ARCHIVE_LOCAL_STAGING env var).

    Returns:
        List of DiscoveredArchive — one per new archive, with shp_paths populated.
        Empty list if no new archives found.
    """
    _prefix = prefix or os.environ.get("MINIO_ARCHIVE_PREFIX", "uploads/")
    _endpoint = endpoint or os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
    _staging = staging_dir or Path(os.environ.get("ARCHIVE_LOCAL_STAGING", "/tmp/geo_staging"))

    # 1. List available archives in MinIO
    available_keys = await _list_minio_archives(bucket, _prefix, _endpoint)
    if not available_keys:
        log.info("archive.none_in_bucket", bucket=bucket, prefix=_prefix)
        return []

    # 2. Dedup against registry
    known = await _known_hashes(dsn)

    # 3. Download, hash, filter, extract
    discovered: list[DiscoveredArchive] = []
    layer_filter_raw = os.environ.get("LAYER_FILTER", "")
    layer_filter: set[str] = (
        {s.strip() for s in layer_filter_raw.split(",") if s.strip()}
        if layer_filter_raw else set()
    )

    for key in available_keys:
        local_path = await _download_archive(key, bucket, _endpoint, _staging)
        archive_hash = _sha256(local_path)

        if archive_hash in known:
            log.info(
                "archive.already_ingested",
                key=key,
                hash_prefix=archive_hash[:12],
            )
            local_path.unlink(missing_ok=True)  # clean up downloaded file
            continue

        try:
            disc = extract_archive(local_path, staging_dir=_staging)
        except Exception as exc:
            log.error(
                "archive.extraction_failed",
                key=key,
                error=str(exc),
            )
            continue

        # Apply LAYER_FILTER if set (e.g. "Provinces,Municipalities")
        if layer_filter:
            filtered = [p for p in disc.shp_paths if p.stem in layer_filter]
            if filtered != disc.shp_paths:
                skipped_layers = [p.stem for p in disc.shp_paths if p not in filtered]
                log.info(
                    "archive.layer_filter_applied",
                    kept=[p.stem for p in filtered],
                    skipped=skipped_layers,
                )
            disc.shp_paths = filtered

        discovered.append(disc)

    log.info(
        "archive.discovery_complete",
        total_in_bucket=len(available_keys),
        new_archives=len(discovered),
    )
    return discovered


# ── Cleanup helper ─────────────────────────────────────────────────────────────

def cleanup_staging(archive: DiscoveredArchive) -> None:
    """
    Remove local staging directory after pipeline completes.
    Called by deploy_gate or DAG teardown.
    Safe to call multiple times (ignores missing dirs).
    """
    staging_root = Path(os.environ.get("ARCHIVE_LOCAL_STAGING", "/tmp/geo_staging"))
    archive_hash = archive.archive_hash
    dest_dir = staging_root / f"{archive.archive_path.stem}_{archive_hash[:12]}"
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
        log.info("archive.staging_cleaned", dir=str(dest_dir))
    # Remove the .7z itself
    if archive.archive_path.exists():
        archive.archive_path.unlink(missing_ok=True)
        log.info("archive.local_archive_removed", file=archive.archive_path.name)
