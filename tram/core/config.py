"""AppConfig — all values from environment variables (12-factor)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    """Read an integer env var; raise ValueError with the variable name on bad input."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {name}={raw!r} is not a valid integer"
        ) from None


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
    # v1.0.0 security additions
    api_key: str
    rate_limit: int
    rate_limit_window: int
    tls_certfile: str
    tls_keyfile: str
    # v1.0.0 observability additions
    otel_endpoint: str
    otel_service: str
    # v1.0.0 operations additions
    watch_pipelines: bool
    # v1.0.0 SNMP MIB directory
    mib_dir: str
    # v1.0.0 schema directory
    schema_dir: str
    # v1.0.4 schema registry (proxy + serializer fallback)
    schema_registry_url: str
    schema_registry_username: str
    schema_registry_password: str
    # v1.0.7 web UI static dir (empty = UI disabled)
    ui_dir: str
    # v1.0.8 browser user auth — comma-separated user:password pairs
    auth_users: str
    # v1.1.0 bundled pipeline templates directory
    templates_dir: str
    # v1.2.0 manager+worker mode
    tram_mode: str       # "standalone" | "manager" | "worker"
    manager_url: str     # worker → manager callback base URL (also used by manager itself)
    # Worker discovery (TRAM_MODE=manager)
    worker_urls: str     # explicit comma-separated worker agent URLs (TRAM_WORKER_URLS)
    worker_replicas: int # K8s headless DNS replica count (0 = disabled)
    worker_service: str  # K8s headless service name
    worker_namespace: str  # K8s namespace
    worker_port: int     # worker agent port

    @classmethod
    def from_env(cls) -> AppConfig:
        node_id = os.environ.get("TRAM_NODE_ID", socket.gethostname())
        return cls(
            host=os.environ.get("TRAM_HOST", "0.0.0.0"),
            port=_env_int("TRAM_PORT", 8765),
            pipeline_dir=os.environ.get("TRAM_PIPELINE_DIR", "./pipelines"),
            state_dir=os.environ.get("TRAM_STATE_DIR") or None,
            api_url=os.environ.get("TRAM_API_URL", "http://localhost:8765"),
            log_level=os.environ.get("TRAM_LOG_LEVEL", "INFO").upper(),
            log_format=os.environ.get("TRAM_LOG_FORMAT", "json"),
            workers=_env_int("TRAM_WORKERS", 1),
            reload_on_start=os.environ.get("TRAM_RELOAD_ON_START", "true").lower() == "true",
            node_id=node_id,
            db_url=os.environ.get("TRAM_DB_URL", ""),
            shutdown_timeout=_env_int("TRAM_SHUTDOWN_TIMEOUT_SECONDS", 30),
            api_key=os.environ.get("TRAM_API_KEY", ""),
            rate_limit=_env_int("TRAM_RATE_LIMIT", 0),
            rate_limit_window=_env_int("TRAM_RATE_LIMIT_WINDOW", 60),
            tls_certfile=os.environ.get("TRAM_TLS_CERTFILE", ""),
            tls_keyfile=os.environ.get("TRAM_TLS_KEYFILE", ""),
            otel_endpoint=os.environ.get("TRAM_OTEL_ENDPOINT", ""),
            otel_service=os.environ.get("TRAM_OTEL_SERVICE", "tram"),
            watch_pipelines=os.environ.get("TRAM_WATCH_PIPELINES", "false").lower() == "true",
            mib_dir=os.environ.get("TRAM_MIB_DIR", "/mibs"),
            schema_dir=os.environ.get("TRAM_SCHEMA_DIR", "/schemas"),
            schema_registry_url=os.environ.get("TRAM_SCHEMA_REGISTRY_URL", ""),
            schema_registry_username=os.environ.get("TRAM_SCHEMA_REGISTRY_USERNAME", ""),
            schema_registry_password=os.environ.get("TRAM_SCHEMA_REGISTRY_PASSWORD", ""),
            ui_dir=os.environ.get("TRAM_UI_DIR", "/ui"),
            auth_users=os.environ.get("TRAM_AUTH_USERS", ""),
            templates_dir=os.environ.get("TRAM_TEMPLATES_DIR", "/tram-templates"),
            tram_mode=os.environ.get("TRAM_MODE", "standalone").lower(),
            manager_url=os.environ.get("TRAM_MANAGER_URL", ""),
            worker_urls=os.environ.get("TRAM_WORKER_URLS", ""),
            worker_replicas=_env_int("TRAM_WORKER_REPLICAS", 0),
            worker_service=os.environ.get("TRAM_WORKER_SERVICE", "tram-worker"),
            worker_namespace=os.environ.get("TRAM_WORKER_NAMESPACE", "default"),
            worker_port=_env_int("TRAM_WORKER_PORT", 8766),
        )
