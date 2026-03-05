"""AppConfig — all values from environment variables (12-factor)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """Application-wide configuration loaded from environment variables."""

    host: str
    port: int
    pipeline_dir: str
    state_dir: str | None
    api_url: str
    log_level: str
    log_format: str
    workers: int
    reload_on_start: bool
    # v0.7.0 additions
    node_id: str
    db_url: str
    shutdown_timeout: int
    # v0.8.0 cluster additions
    cluster_enabled: bool
    node_ordinal: int
    heartbeat_seconds: int
    node_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "AppConfig":
        node_id = os.environ.get("TRAM_NODE_ID", socket.gethostname())
        return cls(
            host=os.environ.get("TRAM_HOST", "0.0.0.0"),
            port=int(os.environ.get("TRAM_PORT", "8765")),
            pipeline_dir=os.environ.get("TRAM_PIPELINE_DIR", "./pipelines"),
            state_dir=os.environ.get("TRAM_STATE_DIR") or None,
            api_url=os.environ.get("TRAM_API_URL", "http://localhost:8765"),
            log_level=os.environ.get("TRAM_LOG_LEVEL", "INFO").upper(),
            log_format=os.environ.get("TRAM_LOG_FORMAT", "json"),
            workers=int(os.environ.get("TRAM_WORKERS", "1")),
            reload_on_start=os.environ.get("TRAM_RELOAD_ON_START", "true").lower() == "true",
            node_id=node_id,
            db_url=os.environ.get("TRAM_DB_URL", ""),
            shutdown_timeout=int(os.environ.get("TRAM_SHUTDOWN_TIMEOUT_SECONDS", "30")),
            cluster_enabled=os.environ.get("TRAM_CLUSTER_ENABLED", "false").lower() == "true",
            node_ordinal=int(os.environ.get("TRAM_NODE_ORDINAL", str(_detect_ordinal(node_id)))),
            heartbeat_seconds=int(os.environ.get("TRAM_HEARTBEAT_SECONDS", "10")),
            node_ttl_seconds=int(os.environ.get("TRAM_NODE_TTL_SECONDS", "30")),
        )


def _detect_ordinal(node_id: str) -> int:
    """Extract StatefulSet ordinal from hostname (e.g. ``tram-2`` → ``2``)."""
    parts = node_id.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return 0
