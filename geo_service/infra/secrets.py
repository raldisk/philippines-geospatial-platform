"""
Docker Secrets reader (Section 23.2).
Rule: secrets read from /run/secrets/ files only — never from env var values.
lru_cache: reads file once at startup; never on the hot path.
Never logs secret values — only path metadata in error messages.
"""
import os
from functools import lru_cache
from pathlib import Path

import structlog

log = structlog.get_logger()

# Environment variable names that point to secret FILE paths
_SECRET_ENV_VARS = {
    "postgres_dsn":      "POSTGRES_DSN_FILE",
    "minio_access_key":  "MINIO_ACCESS_KEY_FILE",
    "minio_secret_key":  "MINIO_SECRET_KEY_FILE",
}


@lru_cache(maxsize=None)
def read_secret(secret_name: str) -> str:
    """
    Read secret from Docker Secrets mount.
    secret_name: logical name (key in _SECRET_ENV_VARS).
    Returns stripped string value.
    Raises ValueError with non-sensitive message on any failure.

    lru_cache ensures each secret file is read exactly once per process lifetime.
    """
    env_var = _SECRET_ENV_VARS.get(secret_name)
    if env_var is None:
        raise ValueError(f"Unknown secret {secret_name!r}. "
                         f"Registered secrets: {list(_SECRET_ENV_VARS)}")

    secret_path_str = os.environ.get(env_var)
    if not secret_path_str:
        raise ValueError(
            f"Secret env var {env_var!r} is not set. "
            "Ensure docker-compose.prod.yml mounts the secret and sets the _FILE env var."
        )

    secret_path = Path(secret_path_str)
    if not secret_path.exists():
        raise ValueError(
            f"Secret file for {secret_name!r} not found. "
            f"Expected path from {env_var!r}. Check Docker Secrets mount."
        )

    if not secret_path.is_file():
        raise ValueError(f"Secret path for {secret_name!r} is not a regular file.")

    value = secret_path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Secret file for {secret_name!r} is empty.")

    log.info("secrets.loaded", secret=secret_name)  # name only — never value
    return value


def get_postgres_dsn() -> str:
    return read_secret("postgres_dsn")


def get_minio_access_key() -> str:
    return read_secret("minio_access_key")


def get_minio_secret_key() -> str:
    return read_secret("minio_secret_key")
