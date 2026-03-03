"""AppConfig — all values from environment variables (12-factor)."""

from __future__ import annotations

import os
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

    @classmethod
    def from_env(cls) -> "AppConfig":
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
        )
