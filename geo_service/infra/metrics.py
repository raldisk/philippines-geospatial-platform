"""
geo_service/infra/metrics.py

Prometheus metrics definitions (Action 4, Section 8.4).
Import REQUEST_DURATION histogram and metrics_app WSGI app into main.py.

Usage:
    from geo_service.infra.metrics import REQUEST_DURATION, metrics_app

Verify:
    curl http://localhost:8000/metrics | grep http_request_duration_seconds
"""
from prometheus_client import Counter, Histogram, make_wsgi_app
from starlette.middleware.wsgi import WSGIMiddleware

# http_request_duration_seconds — labelled by HTTP method + endpoint path.
# Buckets: standard web latency (ms-range to multi-second for heavy tile queries).
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ingestion_runs_total — BYOD observability counter (checklist §Health).
# Labels:
#   archive_name — stem of the .7z/.zip file ingested (e.g. "psa_provincial_2024")
#   status       — one of: success | failure | quarantine
#   vintage      — year string extracted from dataset profile (e.g. "2024")
#
# Usage in pipeline code:
#   from geo_service.infra.metrics import INGESTION_RUNS
#   INGESTION_RUNS.labels(archive_name="psa_provincial_2024",
#                         status="success", vintage="2024").inc()
INGESTION_RUNS = Counter(
    "ingestion_runs_total",
    "Total pipeline ingestion runs by archive, outcome, and vintage",
    labelnames=["archive_name", "status", "vintage"],
)

# WSGI app mounted at /metrics by FastAPI via starlette WSGIMiddleware
metrics_app = WSGIMiddleware(make_wsgi_app())
