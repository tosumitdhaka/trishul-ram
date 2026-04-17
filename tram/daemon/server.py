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

    # ── Worker branch ──────────────────────────────────────────────────────
    # Must be checked BEFORE importing create_app so that the worker image
    # (which does not have apscheduler / sqlalchemy installed) never touches
    # the manager import chain.
    if config.tram_mode == "worker":
        import threading

        import uvicorn

        from tram.agent.server import create_worker_app, create_worker_ingress_app

        worker_app = create_worker_app(
            worker_id=config.node_id,
            manager_url=config.manager_url,
            stats_interval=config.stats_interval,
        )
        ingress_app = create_worker_ingress_app(
            worker_id=config.node_id,
            api_key=config.api_key,
        )

        agent_port = config.worker_port
        ingress_port = config.worker_ingress_port

        tls_kwargs: dict = {}
        if config.tls_certfile and config.tls_keyfile:
            tls_kwargs = {
                "ssl_certfile": config.tls_certfile,
                "ssl_keyfile": config.tls_keyfile,
            }

        def _run_agent():
            uvicorn.run(
                worker_app,
                host=config.host,
                port=agent_port,
                log_config=None,
                access_log=False,
                **tls_kwargs,
            )

        def _run_ingress():
            uvicorn.run(
                ingress_app,
                host=config.host,
                port=ingress_port,
                log_config=None,
                access_log=False,
                **tls_kwargs,
            )

        agent_thread = threading.Thread(
            target=_run_agent,
            name="tram-worker-agent",
            daemon=True,
        )
        ingress_thread = threading.Thread(
            target=_run_ingress,
            name="tram-worker-ingress",
            daemon=True,
        )
        worker_app.state.ingress_thread = ingress_thread

        logger.info(
            "Starting TRAM worker agent",
            extra={
                "host": config.host,
                "agent_port": agent_port,
                "ingress_port": ingress_port,
                "worker_id": config.node_id,
            },
        )

        agent_thread.start()
        ingress_thread.start()

        while agent_thread.is_alive() and ingress_thread.is_alive():
            agent_thread.join(timeout=1.0)
            ingress_thread.join(timeout=1.0)

        if not agent_thread.is_alive():
            logger.warning("Agent thread (:%d) exited — triggering worker restart", agent_port)
        elif not ingress_thread.is_alive():
            logger.warning("Ingress thread (:%d) exited — triggering worker restart", ingress_port)

        os.kill(os.getpid(), signal.SIGTERM)
        return

    # ── Manager / standalone branch ────────────────────────────────────────
    # Imports apscheduler + sqlalchemy transitively — only safe on manager image.
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
