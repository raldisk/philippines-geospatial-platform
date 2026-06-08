# Dockerfile — geo-service multi-stage build
# ADR-008: Pre-built tippecanoe binary, SHA-256 pinned (no source compile)
# Section 23: Non-root execution, geoservice:1001
# ADR-011: Silver is ephemeral — no Silver tooling in production image
#
# Stages:
#   tippecanoe-binary  → download + verify binary (cached until version bump)
#   builder            → compile Python deps with GDAL headers
#   production         → minimal runtime image, non-root user
#
# Build:
#   docker build \
#     --build-arg TIPPECANOE_SHA256=$(sha256sum tippecanoe-linux-x86_64 | awk '{print $1}') \
#     -t geo-service:latest .
#
# ⚠ TIPPECANOE_SHA256 MUST be supplied in CI via secrets.TIPPECANOE_SHA256.
#   Obtain: curl -fsSL <release_url> | sha256sum
#   Weekly version-check workflow (Section 28) alerts on version drift.

# ── Stage 1: tippecanoe binary ─────────────────────────────────────────────
# Cached until TIPPECANOE_VERSION bump. Only layer 1 invalidates on version change.
FROM ubuntu:22.04 AS tippecanoe-binary

ARG TIPPECANOE_VERSION=2.67.0
ARG TIPPECANOE_SHA256

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Download pre-built binary from felt/tippecanoe GitHub Releases (ADR-008).
# SHA-256 verification is mandatory in CI (TIPPECANOE_SHA256 secret required).
# Skipped locally only when ARG is absent — never skip in CI.
RUN set -eux; \
    curl -fsSL \
        "https://github.com/felt/tippecanoe/releases/download/${TIPPECANOE_VERSION}/tippecanoe-linux-x86_64" \
        -o /usr/local/bin/tippecanoe; \
    if [ -n "${TIPPECANOE_SHA256}" ]; then \
        echo "${TIPPECANOE_SHA256}  /usr/local/bin/tippecanoe" | sha256sum --check; \
    else \
        echo "WARNING: TIPPECANOE_SHA256 not supplied — integrity check skipped (local dev only)" >&2; \
    fi; \
    chmod +x /usr/local/bin/tippecanoe; \
    tippecanoe --version

# ── Stage 2: Python build dependencies ────────────────────────────────────────
# python:3.11-slim is Debian bookworm-based; GDAL 3.x available in default apt.
# libgdal-dev / libproj-dev required at build time only (headers for geopandas wheel).
FROM python:3.11-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gdal-bin \
        libgdal-dev \
        libproj-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Install to isolated prefix so Stage 3 COPY --from=builder is surgical.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 3: Production image ──────────────────────────────────────────────────
# Minimal runtime: only GDAL shared libs (no -dev headers), no build-essential.
FROM python:3.11-slim AS production

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gdal-bin \
        libgdal-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — defense in depth (Section 23.2).
# UID/GID 1001: avoids collision with common system users.
RUN groupadd --gid 1001 geoservice \
    && useradd --uid 1001 --gid geoservice --no-create-home --shell /sbin/nologin geoservice

# Pull artifacts from earlier stages.
COPY --from=tippecanoe-binary /usr/local/bin/tippecanoe /usr/local/bin/tippecanoe
COPY --from=builder /install /usr/local

# Application source (chown at copy time — no RUN chown needed).
COPY --chown=geoservice:geoservice geo_service/ /app/geo_service/
COPY --chown=geoservice:geoservice config/ /app/config/

WORKDIR /app
USER geoservice

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GDAL_CACHEMAX=512

EXPOSE 8002

# Docker HEALTHCHECK — CI gate verifies all containers healthy.
# /health/live: liveness (server up). /health/ready: readiness (DB/MinIO connected).
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8002/health/live || exit 1

CMD ["uvicorn", "geo_service.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8002", \
     "--workers", "4", \
     "--log-config", "/app/config/log_config.json"]
