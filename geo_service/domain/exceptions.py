"""
geo_service/domain/exceptions.py

Centralised exception hierarchy (Section 8.2).
All pipeline modules import from here — never define bare Exception subclasses inline.
"""


class GeospatialPlatformError(Exception):
    """Base class for all platform errors."""


# ── Ingestion / extraction ─────────────────────────────────────────────────────
class ExtractionError(GeospatialPlatformError):
    """7z extraction or file validation failure."""


class GeospatialValidationError(GeospatialPlatformError):
    """CRS, geometry type, or coordinate bound failure."""


class QualityContractViolation(GeospatialPlatformError):
    """YAML quality gate threshold exceeded — triggers quarantine."""


class SchemaEvolutionError(GeospatialPlatformError):
    """Breaking schema change detected — requires human review."""


# ── Gold / H3 (ADR-009) ────────────────────────────────────────────────────────
class H3ResolutionError(GeospatialPlatformError):
    """
    No H3 candidate resolution satisfies occupancy thresholds.
    Propagates to Airflow → fires Day 2 gate. Do NOT catch silently.
    """


# ── Gold / PMTiles (ADR-008) ───────────────────────────────────────────────────
class TileGenerationError(GeospatialPlatformError):
    """tippecanoe subprocess failure — non-zero exit or empty output."""


class PMTilesVerificationError(GeospatialPlatformError):
    """`pmtiles verify` failure — tile archive structurally invalid. HALT surface."""


class FileSizeError(GeospatialPlatformError):
    """Output PMTiles ≥ 50 MB gate — reduce zoom or dataset scope. HALT surface."""


class BinaryIntegrityError(GeospatialPlatformError):
    """tippecanoe binary SHA-256 mismatch — supply-chain guard (ADR-008). HALT surface."""


# ── Geometry repair ────────────────────────────────────────────────────────────
class GeometryRepairFailure(GeospatialPlatformError):
    """Geometry invalid after shapely.make_valid() or PostGIS ST_MakeValid()."""
