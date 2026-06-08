"""
FastAPI /geo/v1 — Day 3 serving layer.
Sections 7.4, 23.4, 23.6 compliant.

Action 2: RequestIDMiddleware generates a UUID per request and binds it to
structlog contextvars so every log call within that request context carries
request_id automatically.
Action 4: /metrics endpoint exposes prometheus_client metrics including the
http_request_duration_seconds histogram.
"""
import os
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from geo_service.api.limiter import limiter

from geo_service.api.routes import health, tiles, geojson, h3, metadata
from geo_service.infra.duckdb_conn import DuckDBConnectionManager
from geo_service.infra.metrics import REQUEST_DURATION, metrics_app

log = structlog.get_logger()



@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: warm DuckDB read conn on main thread (lazy per-thread init)."""
    log.info("geo_service.startup")
    yield
    log.info("geo_service.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="PH Geospatial Intelligence Platform",
        version="2.2.0",
        docs_url="/geo/v1/docs",
        redoc_url="/geo/v1/redoc",
        lifespan=lifespan,
    )

    # ---- rate limiting ------------------------------------------------
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # ---- CORS (read-only public tile API — origins unrestricted) ------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # ---- request_id middleware (Action 2) -----------------------------
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        # Record prometheus histogram (Action 4)
        route = request.url.path
        REQUEST_DURATION.labels(method=request.method, endpoint=route).observe(duration)
        response.headers["X-Request-ID"] = request_id
        return response

    # ---- routers -------------------------------------------------------
    app.include_router(health.router)
    app.include_router(tiles.router)
    app.include_router(geojson.router)
    app.include_router(h3.router)
    app.include_router(metadata.router)

    # ---- /metrics endpoint (Action 4) ---------------------------------
    app.mount("/metrics", metrics_app)

    return app


app = create_app()
