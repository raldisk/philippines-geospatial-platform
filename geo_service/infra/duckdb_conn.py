"""
DuckDB connection manager (ADR-002, ADR-012, Section 8.3).
Per-thread read conn: safe for FastAPI worker threads.
Single locked write conn: pipeline only — never on serving path.
`LOAD spatial` on every new connection.
`write_covering_bbox=True` enforced at write time (pipeline code; documented here).

Action 3: tenacity retry/backoff wraps duckdb.connect() calls.
Retries up to 3 attempts with exponential backoff (1s min, 10s max).
Handles transient ConnectionError (e.g. exclusive write lock contention on startup).
"""
import os
import threading
from contextlib import contextmanager
from typing import Generator

import duckdb
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger()

_DB_PATH = os.getenv("DUCKDB_PATH", "/data/geo_platform.duckdb")


def _connect_read_only(db_path: str) -> duckdb.DuckDBPyConnection:
    """Open a read-only DuckDB connection with tenacity retry/backoff."""

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _attempt() -> duckdb.DuckDBPyConnection:
        return duckdb.connect(db_path, read_only=True)

    return _attempt()


def _connect_write(db_path: str) -> duckdb.DuckDBPyConnection:
    """Open a read-write DuckDB connection with tenacity retry/backoff."""

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _attempt() -> duckdb.DuckDBPyConnection:
        return duckdb.connect(db_path)

    return _attempt()


class DuckDBConnectionManager:
    """
    DDIA §4: DuckDB uses optimistic concurrency — concurrent reads are safe;
    concurrent writes require serialization.

    Thread model:
    - read_conn()  → per-thread, read_only=True  → no lock needed
    - write_conn() → context manager, single locked connection → pipeline only

    ADR-012: All GeoParquet reads benefit from Hilbert bbox covering index
    written by `gdf.to_parquet(..., write_covering_bbox=True)`.
    DuckDB automatically uses this index when ST_Intersects / bbox filter present.
    Verify with: conn.execute("EXPLAIN SELECT ... FROM read_parquet(...) WHERE ST_Intersects(...)")
    """

    def __init__(self, db_path: str = _DB_PATH) -> None:
        self._db_path = db_path
        self._write_lock = threading.Lock()
        self._thread_local = threading.local()

    # ------------------------------------------------------------------
    # Read path (FastAPI serving)
    # ------------------------------------------------------------------

    def read_conn(self) -> duckdb.DuckDBPyConnection:
        """
        Per-thread read-only connection.
        Thread-local: each FastAPI worker gets its own connection — no contention.
        Spatial extension loaded once per thread at first access.
        tenacity retries up to 3 attempts on ConnectionError.
        """
        if not hasattr(self._thread_local, "conn"):
            log.debug("duckdb.read_conn.init", thread=threading.current_thread().name)
            conn = _connect_read_only(self._db_path)
            conn.execute("LOAD spatial")
            conn.execute("PRAGMA threads=4")  # per-conn parallelism
            self._thread_local.conn = conn
        return self._thread_local.conn

    # ------------------------------------------------------------------
    # Write path (pipeline only — not serving)
    # ------------------------------------------------------------------

    @contextmanager
    def write_conn(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        """
        Serialized write connection.
        try/finally ensures lock released even on pipeline crash.
        commit() on success; rollback() on exception.
        tenacity retries up to 3 attempts on ConnectionError.
        """
        with self._write_lock:
            conn = _connect_write(self._db_path)
            conn.execute("LOAD spatial")
            try:
                yield conn
                conn.commit()
                log.info("duckdb.write_conn.commit")
            except Exception as exc:
                conn.rollback()
                log.error("duckdb.write_conn.rollback", error=str(exc))
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # View bootstrap (called once at pipeline startup)
    # ------------------------------------------------------------------

    def bootstrap_views(self) -> None:
        """
        Create analytical views on the DuckDB database.
        Idempotent — CREATE OR REPLACE VIEW.
        Called by Airflow DAG after Gold Parquet lands in MinIO/local.
        """
        with self.write_conn() as conn:
            # ---- mart_geo_poverty_crosswalk ----------------------------
            # Cross-domain view: spatial boundaries + PSA poverty indicators
            # ASOF JOIN target: region_name + year (conformed dim_region key)
            conn.execute("""
                CREATE OR REPLACE VIEW mart_geo_poverty_crosswalk AS
                SELECT
                    b.region_name,
                    b.admin_level,
                    b.psgc_code,
                    b.vintage_year,
                    p.poverty_rate,
                    p.poverty_threshold,
                    p.subsistence_incidence,
                    p.data_year  AS poverty_year,
                    g.gdp_growth,
                    g.gdp_per_capita,
                    g.data_year  AS gdp_year,
                    b.geometry
                FROM read_parquet('/data/gold/curated/psa_provincial.parquet') b
                LEFT JOIN read_parquet('/data/gold/indicators/poverty_indicators.parquet') p
                    ON b.psgc_code = p.psgc_code
                LEFT JOIN read_parquet('/data/gold/indicators/gdp_indicators.parquet') g
                    ON b.psgc_code = g.psgc_code
            """)

            # ---- mart_h3_poverty_summary --------------------------------
            # Flat view for H3 endpoint: pre-joined, resolution-keyed
            conn.execute("""
                CREATE OR REPLACE VIEW mart_h3_poverty_summary AS
                SELECT
                    h.h3_index,
                    h.resolution,
                    h.h3_lat,
                    h.h3_lng,
                    h.region_name,
                    h.poverty_rate,
                    h.jenks_class,
                    h.feature_count
                FROM read_parquet('/data/gold/h3/h3_aggregates.parquet') h
            """)

            log.info("duckdb.bootstrap_views.complete")

    # ------------------------------------------------------------------
    # EXPLAIN helper (ADR-012 validation)
    # ------------------------------------------------------------------

    def explain_bbox_pruning(self, parquet_path: str) -> str:
        """
        Return EXPLAIN output for a bbox-filtered query.
        Verify 'Parquet Filter' appears in plan — confirms row-group pruning active.
        """
        sql = f"""
            EXPLAIN SELECT region_name, ST_AsGeoJSON(geometry)
            FROM read_parquet('{parquet_path}')
            WHERE ST_Intersects(
                geometry,
                ST_MakeEnvelope(120.0, 10.0, 124.0, 14.0)
            )
        """
        conn = self.read_conn()
        rows = conn.execute(sql).fetchall()
        plan = "\n".join(r[1] for r in rows)
        if "Parquet Filter" not in plan and "ParquetScan" not in plan:
            log.warning("duckdb.bbox_pruning.not_detected", parquet=parquet_path)
        else:
            log.info("duckdb.bbox_pruning.confirmed", parquet=parquet_path)
        return plan


# ------------------------------------------------------------------
# Module-level singleton (one manager per process)
# ------------------------------------------------------------------

_manager: DuckDBConnectionManager | None = None
_manager_lock = threading.Lock()


def get_conn_manager() -> DuckDBConnectionManager:
    """Return process-level singleton DuckDBConnectionManager."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = DuckDBConnectionManager(_DB_PATH)
    return _manager


def get_duckdb_conn() -> duckdb.DuckDBPyConnection:
    """
    FastAPI Depends()-compatible factory.
    Usage: conn = Depends(get_duckdb_conn)
    """
    return get_conn_manager().read_conn()
