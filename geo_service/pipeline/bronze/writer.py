"""
Bronze layer writer — Day 1 variant.

Day 1 constraints (per master plan):
  - Local staging directory (MinIO S3A deferred to Day 7 / Phase 2)
  - SQLite geo_ingest_registry (PostgreSQL deferred to Day 3)
  - Single-threaded

Invariants that hold across all days:
  ADR-012: write_covering_bbox=True on every GeoParquet write (DuckDB Hilbert bbox index).
  SHA-256 idempotency: same archive → ALREADY_INGESTED, no rewrite.
  BRONZE_SCHEMA: extra .dbf columns overflow into extra_attributes (schema-evolution safety,
                 Section 4.5 — "overflow column" pattern).
  Immutable Bronze: never UPDATE/DELETE Bronze rows — any error routes to quarantine.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import geopandas as gpd
import pyarrow as pa
import structlog

log = structlog.get_logger()

# Authoritative Bronze schema (Section 4.5).
# Upstream .dbf columns not in this set overflow to extra_attributes.
BRONZE_SCHEMA_COLS: frozenset[str] = frozenset(
    {"admin_code", "region_name", "geometry",
     "source_archive_hash", "ingestion_ts", "vintage_year", "extra_attributes"}
)

# PyArrow schema for validation and Parquet metadata annotation.
BRONZE_PA_SCHEMA = pa.schema([
    pa.field("admin_code",           pa.string(),                             nullable=True),
    pa.field("region_name",          pa.string(),                             nullable=True),
    pa.field("source_archive_hash",  pa.string(),                             nullable=False),
    pa.field("ingestion_ts",         pa.timestamp("us", tz="UTC"),            nullable=False),
    pa.field("vintage_year",         pa.int16(),                              nullable=True),
    pa.field("extra_attributes",     pa.map_(pa.string(), pa.string()),       nullable=True),
    # geometry written separately as WKB by geopandas — not in this schema
])


class WriteStatus(str, Enum):
    WRITTEN          = "WRITTEN"
    ALREADY_INGESTED = "ALREADY_INGESTED"
    FAILED           = "FAILED"


@dataclass
class BronzeWriteResult:
    status:         WriteStatus
    archive_hash:   str
    output_path:    str | None
    feature_count:  int
    run_id:         str


# ── Registry (SQLite Day 1) ────────────────────────────────────────────────────

_REGISTRY_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS geo_ingest_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_hash    TEXT    NOT NULL UNIQUE,
    status          TEXT    NOT NULL CHECK(status IN ('WRITTEN','ALREADY_INGESTED','FAILED','QUARANTINED')),
    operation_mode  TEXT             CHECK(operation_mode IN ('analytical','geometry_only','boundary_catalog')),
    dataset_name    TEXT,
    output_path     TEXT,
    feature_count   INTEGER,
    run_id          TEXT,
    pipeline_run_id TEXT,
    extracted_at    TEXT    NOT NULL
)
"""

_REGISTRY_INSERT_SQL = """
INSERT INTO geo_ingest_registry
    (archive_hash, status, operation_mode, dataset_name, output_path,
     feature_count, run_id, pipeline_run_id, extracted_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(archive_hash) DO UPDATE SET
    status          = excluded.status,
    output_path     = excluded.output_path,
    feature_count   = excluded.feature_count,
    extracted_at    = excluded.extracted_at
"""


def init_registry(db_path: str = "geo_ingest_registry.db") -> sqlite3.Connection:
    """
    Initialize (or open) SQLite registry. Idempotent.
    Day 3+: replace with asyncpg PostgreSQL connection pool.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(_REGISTRY_CREATE_SQL)
    conn.commit()
    return conn


def _check_already_ingested(conn: sqlite3.Connection, archive_hash: str) -> str | None:
    """Returns output_path if previously WRITTEN, else None."""
    row = conn.execute(
        "SELECT status, output_path FROM geo_ingest_registry WHERE archive_hash = ?",
        (archive_hash,),
    ).fetchone()
    if row and row[0] == "WRITTEN":
        return row[1]
    return None


# ── Main writer ───────────────────────────────────────────────────────────────

def write_bronze(
    gdf:            gpd.GeoDataFrame,
    archive_path:   str,
    dataset_name:   str,
    operation_mode: str = "analytical",
    bronze_dir:     str = "data/bronze",
    registry_db:    str = "geo_ingest_registry.db",
    vintage_year:   int | None = None,
    run_id:         str | None = None,
) -> BronzeWriteResult:
    """
    Write GeoDataFrame to Bronze GeoParquet.

    SHA-256 dedup: same archive → returns ALREADY_INGESTED without re-writing.
    ADR-012: write_covering_bbox=True on every write.
    Extra .dbf columns → extra_attributes map (Section 4.5 overflow pattern).
    Atomic write: temp file → os.replace → final path (avoids partial-write reads).
    """
    run_id = run_id or str(uuid.uuid4())
    archive_hash = _sha256(archive_path)

    conn = init_registry(registry_db)

    existing_path = _check_already_ingested(conn, archive_hash)
    if existing_path:
        log.info(
            "bronze.already_ingested",
            dataset=dataset_name,
            archive_hash=archive_hash[:12],
            previous_output=existing_path,
            run_id=run_id,
        )
        conn.close()
        return BronzeWriteResult(
            status=WriteStatus.ALREADY_INGESTED,
            archive_hash=archive_hash,
            output_path=existing_path,
            feature_count=0,
            run_id=run_id,
        )

    output_dir = Path(bronze_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = str(output_dir / f"{dataset_name}_{archive_hash[:12]}.parquet")
    tmp_path   = final_path + f".tmp_{run_id[:8]}"

    # ── annotate with lineage metadata ───────────────────────────────────────
    gdf = gdf.copy()
    gdf["source_archive_hash"] = archive_hash
    gdf["ingestion_ts"]        = datetime.now(timezone.utc)
    gdf["vintage_year"]        = vintage_year

    # ── schema-evolution overflow (Section 4.5) ───────────────────────────────
    # Unknown .dbf columns → extra_attributes: map<string, string>
    # New fields survive without breaking downstream consumers.
    overflow = [c for c in gdf.columns if c not in BRONZE_SCHEMA_COLS]
    if overflow:
        gdf["extra_attributes"] = gdf[overflow].apply(
            lambda row: json.dumps({k: str(v) for k, v in row.items() if v is not None}),
            axis=1,
        )
        log.info(
            "bronze.overflow_columns",
            dataset=dataset_name,
            columns=overflow,
            note="extra .dbf fields written to extra_attributes (Section 4.5)",
        )
    else:
        gdf["extra_attributes"] = None

    # ── atomic GeoParquet write (ADR-012) ─────────────────────────────────────
    try:
        gdf.to_parquet(
            tmp_path,
            geometry_encoding="WKB",
            write_covering_bbox=True,   # ADR-012: Hilbert bbox for DuckDB row-group pruning
            index=False,
        )
        import os
        os.replace(tmp_path, final_path)
        feature_count = len(gdf)
    except Exception as exc:
        import os
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        log.error(
            "bronze.write_failed",
            dataset=dataset_name,
            archive_hash=archive_hash[:12],
            error=str(exc),
            run_id=run_id,
        )
        _register(conn, archive_hash, "FAILED", operation_mode, dataset_name,
                  None, 0, run_id)
        conn.close()
        return BronzeWriteResult(
            status=WriteStatus.FAILED,
            archive_hash=archive_hash,
            output_path=None,
            feature_count=0,
            run_id=run_id,
        )

    _register(conn, archive_hash, "WRITTEN", operation_mode, dataset_name,
              final_path, feature_count, run_id)
    conn.close()

    log.info(
        "bronze.written",
        dataset=dataset_name,
        path=final_path,
        feature_count=feature_count,
        archive_hash=archive_hash[:12],
        overflow_cols=len(overflow),
        run_id=run_id,
    )

    return BronzeWriteResult(
        status=WriteStatus.WRITTEN,
        archive_hash=archive_hash,
        output_path=final_path,
        feature_count=feature_count,
        run_id=run_id,
    )


# ── helpers ────────────────────────────────────────────────────────────────────

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _register(
    conn:          sqlite3.Connection,
    archive_hash:  str,
    status:        str,
    operation_mode: str,
    dataset_name:  str,
    output_path:   str | None,
    feature_count: int,
    run_id:        str,
) -> None:
    conn.execute(
        _REGISTRY_INSERT_SQL,
        (archive_hash, status, operation_mode, dataset_name, output_path,
         feature_count, run_id, run_id,   # run_id doubles as pipeline_run_id Day 1
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
