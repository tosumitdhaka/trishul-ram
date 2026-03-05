"""TramServer — starts TramScheduler + uvicorn in one process."""

from __future__ import annotations

import logging
import os
import signal

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

    # Install SIGTERM handler so the OS / container runtime gets a clean exit.
    # Uvicorn handles SIGINT (Ctrl-C) natively; SIGTERM needs an explicit handler
    # when running as PID 1 (Docker / Kubernetes).
    _orig_sigterm = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum, frame):  # noqa: ANN001
        logger.info("SIGTERM received — initiating graceful shutdown")
        os.kill(os.getpid(), signal.SIGINT)  # uvicorn responds to SIGINT for graceful stop
        signal.signal(signal.SIGTERM, _orig_sigterm)  # restore

    signal.signal(signal.SIGTERM, _on_sigterm)

    logger.info(
        "Starting TRAM daemon",
        extra={
            "host": config.host,
            "port": config.port,
            "node_id": config.node_id,
        },
    )

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        workers=config.workers,
        log_config=None,  # We handle logging ourselves
        access_log=False,
    )
