"""
geo_service/infra/logging_config.py

Structlog initialisation (Section 8.1).
Call configure_logging() once at process start (run_day2.py, Airflow task entry).

Every log line produced by this platform includes:
    run_id, dataset, stage, duration_ms, record_count  (where applicable)
No string formatting in log calls — use bound loggers with keyword args only.

Action 2 (request_id): structlog.contextvars is already in the shared_processors chain
via merge_contextvars. RequestIDMiddleware binds request_id per-request, and it
appears automatically in every downstream log call for that request.
"""

import logging
import sys

import structlog


def configure_logging(log_level: str = "INFO", json_output: bool = False) -> None:
    """
    Initialise structlog with console or JSON renderer.

    Parameters
    ----------
    log_level   : "DEBUG" | "INFO" | "WARNING" | "ERROR"
    json_output : True for production/Airflow (machine-parseable).
                  False for local development (human-readable coloured output).
    """
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
