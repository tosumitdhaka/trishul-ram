"""FastAPI application factory with lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tram.api.routers import health, pipelines, runs
from tram.api.routers import metrics_router, webhooks
from tram.core.config import AppConfig
from tram.pipeline.loader import scan_pipeline_dir
from tram.pipeline.manager import PipelineManager
from tram.scheduler.scheduler import TramScheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    config: AppConfig = app.state.config
    manager: PipelineManager = app.state.manager
    scheduler: TramScheduler = app.state.scheduler
    node_registry = getattr(app.state, "node_registry", None)

    # ── Startup ────────────────────────────────────────────────────────────
    logger.info("TRAM daemon starting", extra={"node_id": config.node_id})

    # Init OpenTelemetry tracing if configured
    if config.otel_endpoint:
        try:
            from tram.telemetry.tracing import init_tracing
            init_tracing(service_name=config.otel_service, otlp_endpoint=config.otel_endpoint)
            logger.info("OpenTelemetry tracing initialised", extra={"endpoint": config.otel_endpoint})
        except Exception as exc:
            logger.warning("OTel init failed: %s", exc)

    # Import plugins to trigger registration
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401

    # Load pipelines from directory
    if config.reload_on_start:
        configs = scan_pipeline_dir(config.pipeline_dir)
        for pipeline_config in configs:
            try:
                manager.register(pipeline_config)
                logger.info("Loaded pipeline", extra={"pipeline": pipeline_config.name})
            except Exception as exc:
                logger.error(
                    "Failed to load pipeline",
                    extra={"pipeline": pipeline_config.name, "error": str(exc)},
                )

    # Start node registry (heartbeat) before scheduler so coordinator
    # has an initial topology before the first pipeline is scheduled
    if node_registry is not None:
        node_registry.start()
        logger.info("Cluster mode active", extra={"node_id": config.node_id})

    # Start scheduler
    scheduler.start()

    # Start pipeline file watcher if enabled
    watcher = None
    if config.watch_pipelines:
        try:
            from tram.watcher.pipeline_watcher import PipelineWatcher
            watcher = PipelineWatcher(pipeline_dir=config.pipeline_dir, manager=manager)
            watcher.start()
            logger.info("Pipeline file watcher started", extra={"dir": config.pipeline_dir})
        except ImportError:
            logger.warning(
                "Pipeline watcher requires watchdog — install with: pip install tram[watch]"
            )
        except Exception as exc:
            logger.warning("Pipeline watcher failed to start: %s", exc)

    logger.info("TRAM daemon ready", extra={"port": config.port})

    yield  # application runs here

    # ── Shutdown ───────────────────────────────────────────────────────────
    logger.info("TRAM daemon shutting down")

    # Stop file watcher first
    if watcher is not None:
        watcher.stop()

    scheduler.stop(timeout=config.shutdown_timeout)

    # Stop node registry (deregisters from cluster) after scheduler drain
    if node_registry is not None:
        node_registry.stop()

    # Close DB if present
    db = getattr(app.state, "db", None)
    if db is not None:
        db.close()

    logger.info("TRAM daemon stopped")


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = AppConfig.from_env()

    # Initialise persistence
    try:
        from tram.persistence.db import TramDB
        db = TramDB(url=config.db_url, node_id=config.node_id)
    except Exception as exc:
        logger.warning("Could not initialise TramDB: %s — run history will be in-memory only", exc)
        db = None

    # Initialise alert evaluator
    try:
        from tram.alerts.evaluator import AlertEvaluator
        alert_evaluator = AlertEvaluator(db=db)
    except Exception as exc:
        logger.warning("Could not initialise AlertEvaluator: %s", exc)
        alert_evaluator = None

    # Initialise processed-file tracker (requires DB)
    file_tracker = None
    if db is not None:
        from tram.persistence.file_tracker import ProcessedFileTracker
        file_tracker = ProcessedFileTracker(db=db)

    manager = PipelineManager(db=db, alert_evaluator=alert_evaluator)

    # Cluster setup (only when enabled + external DB is configured)
    node_registry = None
    coordinator = None
    if config.cluster_enabled:
        if not config.db_url:
            logger.warning(
                "TRAM_CLUSTER_ENABLED=true but TRAM_DB_URL is not set — "
                "cluster mode requires an external DB (PostgreSQL or MariaDB). "
                "Falling back to standalone mode."
            )
        elif db is not None:
            try:
                from tram.cluster.coordinator import ClusterCoordinator
                from tram.cluster.registry import NodeRegistry
                node_registry = NodeRegistry(
                    db=db,
                    node_id=config.node_id,
                    ordinal=config.node_ordinal,
                    heartbeat_seconds=config.heartbeat_seconds,
                    ttl_seconds=config.node_ttl_seconds,
                )
                coordinator = ClusterCoordinator(
                    registry=node_registry,
                    node_id=config.node_id,
                )
                logger.info(
                    "Cluster mode initialised",
                    extra={"node_id": config.node_id, "ordinal": config.node_ordinal},
                )
            except Exception as exc:
                logger.error("Failed to initialise cluster — running standalone: %s", exc)
                node_registry = None
                coordinator = None

    scheduler = TramScheduler(
        manager,
        coordinator=coordinator,
        rebalance_interval=config.heartbeat_seconds,
        file_tracker=file_tracker,
    )

    app = FastAPI(
        title="TRAM",
        description="Trishul Real-time Adapter & Mapper",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Store shared state
    app.state.config = config
    app.state.manager = manager
    app.state.scheduler = scheduler
    app.state.db = db
    app.state.alert_evaluator = alert_evaluator
    app.state.node_registry = node_registry
    app.state.coordinator = coordinator

    # Add security + rate-limit middleware (outermost = last applied)
    from tram.api.middleware import APIKeyMiddleware, RateLimitMiddleware
    if config.rate_limit > 0:
        app.add_middleware(
            RateLimitMiddleware,
            rate_limit=config.rate_limit,
            window_seconds=config.rate_limit_window,
        )
    app.add_middleware(APIKeyMiddleware)

    # Register routers
    app.include_router(health.router)
    app.include_router(pipelines.router)
    app.include_router(runs.router)
    app.include_router(webhooks.router)
    app.include_router(metrics_router.router)

    return app
