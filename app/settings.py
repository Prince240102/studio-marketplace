from __future__ import annotations

import os


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


class Settings:
    data_root: str = os.getenv("MARKETPLACE_DATA_ROOT", "/data")
    index_db_path: str = os.getenv("MARKETPLACE_INDEX_DB_PATH", "")
    # Optional sync step (e.g. git pull + unzip) executed on service startup.
    # Use a single shell command (run via /bin/sh -lc).
    sync_cmd: str = os.getenv("MARKETPLACE_SYNC_CMD", "")
    sync_timeout_seconds: int = _get_int("MARKETPLACE_SYNC_TIMEOUT_SECONDS", 600)
    # Whether to validate DB metadata against filesystem stats.
    # If true and stats differ, the index is rebuilt and DB rewritten.
    validate_index_db: bool = (
        os.getenv("MARKETPLACE_VALIDATE_INDEX_DB", "true").lower() != "false"
    )
    host: str = os.getenv("MARKETPLACE_HOST", "0.0.0.0")
    port: int = _get_int("MARKETPLACE_PORT", 3001)
    reindex_interval_seconds: int = _get_int("MARKETPLACE_REINDEX_INTERVAL_SECONDS", 30)
    admin_token: str | None = os.getenv("MARKETPLACE_ADMIN_TOKEN") or None
    cors_allow_origins: str = os.getenv("MARKETPLACE_CORS_ALLOW_ORIGINS", "")


settings = Settings()
