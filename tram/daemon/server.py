"""TramServer — starts TramScheduler + uvicorn in one process."""

from __future__ import annotations

import logging

from tram.core.config import AppConfig
from tram.core.log_config import setup_logging

logger = logging.getLogger(__name__)


def serve(config: AppConfig | None = None) -> None:
    """Start the TRAM daemon (blocking)."""
    if config is None:
        config = AppConfig.from_env()

    setup_logging(level=config.log_level, fmt=config.log_format)

    import uvicorn

    from tram.api.app import create_app

    app = create_app(config)

    logger.info(
        "Starting TRAM daemon",
        extra={"host": config.host, "port": config.port},
    )

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        workers=config.workers,
        log_config=None,  # We handle logging ourselves
        access_log=False,
    )
