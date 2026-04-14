"""TramServer — starts the TRAM daemon (manager or worker) via uvicorn."""

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

    # ── Worker branch ──────────────────────────────────────────────────────
    # Must be checked BEFORE importing create_app so that the worker image
    # (which does not have apscheduler / sqlalchemy installed) never touches
    # the manager import chain.
    if config.tram_mode == "worker":
        from tram.agent.server import create_worker_app

        worker_app = create_worker_app(
            worker_id=config.node_id,
            manager_url=config.manager_url,
        )
        worker_port = int(os.environ.get("TRAM_WORKER_PORT", "8766"))
        logger.info(
            "Starting TRAM worker agent",
            extra={
                "host": config.host,
                "port": worker_port,
                "worker_id": config.node_id,
                "manager_url": config.manager_url,
            },
        )
        uvicorn_kwargs: dict = dict(
            host=config.host,
            port=worker_port,
            workers=1,          # worker agent is always single-process
            log_config=None,
            access_log=False,
        )
        if config.tls_certfile and config.tls_keyfile:
            uvicorn_kwargs["ssl_certfile"] = config.tls_certfile
            uvicorn_kwargs["ssl_keyfile"] = config.tls_keyfile
        uvicorn.run(worker_app, **uvicorn_kwargs)
        return

    # ── Manager / standalone branch ────────────────────────────────────────
    # Imports apscheduler + sqlalchemy transitively — only safe on manager image.
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

    uvicorn_kwargs = dict(
        host=config.host,
        port=config.port,
        workers=config.workers,
        log_config=None,  # We handle logging ourselves
        access_log=False,
    )
    if config.tls_certfile and config.tls_keyfile:
        uvicorn_kwargs["ssl_certfile"] = config.tls_certfile
        uvicorn_kwargs["ssl_keyfile"] = config.tls_keyfile
        logger.info(
            "TLS enabled",
            extra={"certfile": config.tls_certfile},
        )

    uvicorn.run(app, **uvicorn_kwargs)
