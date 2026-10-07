"""TramServer — starts the TRAM daemon (manager or worker) via uvicorn."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping

from tram.core.config import AppConfig, http_accelerated
from tram.core.log_config import setup_logging

logger = logging.getLogger(__name__)


def _http_accel_kwargs() -> dict:
    """uvicorn ``loop``/``http`` kwargs for TRAM_HTTP_ACCELERATED.

    Flag on + uvloop/httptools importable → ``loop="uvloop"``,
    ``http="httptools"``. Flag off (default), or either package missing →
    explicit default runtime ``loop="asyncio"``, ``http="h11"`` (never
    ``{}``: with the http_accel extra installed, uvicorn's auto selection
    would silently pick uvloop/httptools even with the flag off — installed
    must not mean selected) with one WARNING naming the missing package —
    never crashes on missing dependencies.
    """
    default_runtime = {"loop": "asyncio", "http": "h11"}
    if not http_accelerated():
        return dict(default_runtime)
    missing: list[str] = []
    try:
        import uvloop  # noqa: F401
    except ImportError:
        missing.append("uvloop")
    try:
        import httptools  # noqa: F401
    except ImportError:
        missing.append("httptools")
    if missing:
        logger.warning(
            "TRAM_HTTP_ACCELERATED=1 but %s not installed; "
            "using default asyncio/h11 runtime",
            " and ".join(missing),
        )
        return dict(default_runtime)
    return {"loop": "uvloop", "http": "httptools"}


def _loop_runtime_name(loop) -> str:
    """Short event-loop module name ('uvloop' | 'asyncio') from the ACTUAL class."""
    cls = loop if isinstance(loop, type) else type(loop)
    module = (getattr(cls, "__module__", "") or "").split(".")[0]
    return "uvloop" if module == "uvloop" else "asyncio"


def _http_runtime_name(protocol_class) -> str:
    """Short HTTP-parser name from the ACTUAL uvicorn protocol class.

    ``uvicorn.protocols.http.httptools_impl.HttpToolsProtocol`` → 'httptools'
    ``uvicorn.protocols.http.h11_impl.H11Protocol``          → 'h11'
    """
    module = getattr(protocol_class, "__module__", "") or ""
    if module.startswith("uvicorn.protocols.http.httptools_impl"):
        return "httptools"
    if module.startswith("uvicorn.protocols.http.h11_impl"):
        return "h11"
    try:
        from uvicorn.protocols.http import h11_impl, httptools_impl
    except ImportError:
        return getattr(protocol_class, "__name__", str(protocol_class))
    if issubclass(protocol_class, httptools_impl.HttpToolsProtocol):
        return "httptools"
    if issubclass(protocol_class, h11_impl.H11Protocol):
        return "h11"
    return getattr(protocol_class, "__name__", str(protocol_class))


def _probe_event_loop(loop: str):
    """Create a throwaway event loop of the class uvicorn will actually use."""
    try:
        from uvicorn.config import LOOP_FACTORIES as loop_map
    except ImportError:  # uvicorn < 0.36 — LOOP_SETUPS installed the policy
        from uvicorn.config import LOOP_SETUPS as loop_map

    resolved = loop_map.get(loop, loop)
    if resolved is None:
        return asyncio.new_event_loop()
    from uvicorn.config import import_from_string

    setup = import_from_string(resolved)
    factory = setup()
    if factory is None:  # pre-0.36 loop_setup: the policy is already installed
        return asyncio.new_event_loop()
    return factory()


def _resolve_active_runtime(loop: str, http: str) -> tuple[str, str]:
    """Resolve the ACTUAL event-loop and HTTP-parser uvicorn will serve with.

    Uses uvicorn's own factories/mappings (``uvicorn.config.LOOP_FACTORIES`` /
    ``HTTP_PROTOCOLS``), then probes them for the concrete classes — an
    installed dependency that never engaged is not reported as active.
    """
    from uvicorn.config import HTTP_PROTOCOLS, import_from_string

    probe = _probe_event_loop(loop)
    try:
        loop_name = _loop_runtime_name(probe)
    finally:
        if not isinstance(probe, type):
            probe.close()

    protocol_class = import_from_string(HTTP_PROTOCOLS.get(http, http))
    return loop_name, _http_runtime_name(protocol_class)


def _report_runtime(uvicorn_kwargs: Mapping) -> None:
    """Log ONE INFO line naming the ACTUAL event loop + HTTP parser serving."""
    try:
        loop_name, http_name = _resolve_active_runtime(
            uvicorn_kwargs.get("loop", "auto"),
            uvicorn_kwargs.get("http", "auto"),
        )
    except Exception:
        logger.warning("Unable to resolve the active HTTP runtime for reporting")
        return
    logger.info("HTTP runtime: loop=%s http=%s", loop_name, http_name)


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

        # Resolved once (main thread) so the accelerated-runtime WARNING —
        # if any — is logged once, not once per server thread.
        http_accel_kwargs = _http_accel_kwargs()

        def _run_agent():
            uvicorn_kwargs = {
                "host": config.host,
                "port": agent_port,
                "log_config": None,
                "access_log": False,
                **tls_kwargs,
                **http_accel_kwargs,
            }
            _report_runtime(uvicorn_kwargs)
            uvicorn.run(worker_app, **uvicorn_kwargs)

        def _run_ingress():
            uvicorn_kwargs = {
                "host": config.host,
                "port": ingress_port,
                "log_config": None,
                "access_log": False,
                **tls_kwargs,
                **http_accel_kwargs,
            }
            _report_runtime(uvicorn_kwargs)
            uvicorn.run(ingress_app, **uvicorn_kwargs)

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

    # Review §3.13 (warn-only first step): with uvicorn workers > 1 and no
    # shared TRAM_AUTH_SECRET, each worker process mints its own session
    # signing secret, so browser auth tokens fail verification intermittently
    # across workers. Warn loudly; refusing to start would be the stricter
    # option but is left to deployment policy.
    if config.workers > 1 and not os.environ.get("TRAM_AUTH_SECRET"):
        logger.warning(
            "TRAM_WORKERS > 1 without TRAM_AUTH_SECRET: each worker process "
            "generates its own auth-token signing secret, so browser sessions "
            "will fail verification intermittently across workers. Set "
            "TRAM_AUTH_SECRET to one shared value when running multi-worker."
        )

    uvicorn_kwargs = dict(
        host=config.host,
        port=config.port,
        workers=config.workers,
        log_config=None,  # We handle logging ourselves
        access_log=False,
    )
    uvicorn_kwargs.update(_http_accel_kwargs())
    if config.tls_certfile and config.tls_keyfile:
        uvicorn_kwargs["ssl_certfile"] = config.tls_certfile
        uvicorn_kwargs["ssl_keyfile"] = config.tls_keyfile
        logger.info(
            "TLS enabled",
            extra={"certfile": config.tls_certfile},
        )

    _report_runtime(uvicorn_kwargs)
    uvicorn.run(app, **uvicorn_kwargs)
