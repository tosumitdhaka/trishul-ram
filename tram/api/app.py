"""FastAPI application factory with lifespan."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from tram import __version__
from tram.api.routers import (
    ai,
    auth,
    connectors,
    health,
    internal,
    metrics_router,
    mibs,
    pipelines,
    runs,
    schemas,
    stats,
    templates,
    webhooks,
)
from tram.core.config import AppConfig
from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import scan_pipeline_dir

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    config: AppConfig = app.state.config
    controller: PipelineController = app.state.controller
    worker_pool = getattr(app.state, "worker_pool", None)
    reconciler = getattr(app.state, "reconciler", None)

    # ── Startup ────────────────────────────────────────────────────────────
    logger.info("TRAM daemon starting",
                extra={"node_id": config.node_id, "mode": config.tram_mode})

    if config.otel_endpoint:
        try:
            from tram.telemetry.tracing import init_tracing
            init_tracing(service_name=config.otel_service, otlp_endpoint=config.otel_endpoint)
            logger.info("OpenTelemetry tracing initialised",
                        extra={"endpoint": config.otel_endpoint})
        except Exception as exc:
            logger.warning("OTel init failed: %s", exc)

    # Import plugins to trigger registration
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401

    # Seed disk pipelines into DB (DB is single source of truth).
    # Disk seed skips pipelines already owned by user (source='api').
    if config.reload_on_start:
        db = getattr(app.state, "db", None)
        for pipeline_config, yaml_text in scan_pipeline_dir(config.pipeline_dir):
            if db is not None:
                existing_source = db.get_pipeline_source(pipeline_config.name)
                if existing_source == "api":
                    logger.debug("Disk seed skipped (user-owned pipeline)",
                                 extra={"pipeline": pipeline_config.name})
                    continue
                db.save_pipeline(pipeline_config.name, yaml_text, source="disk")
                logger.info("Seeded pipeline to DB",
                            extra={"pipeline": pipeline_config.name})

    # Start worker pool before controller so workers are ready to receive runs
    if worker_pool is not None:
        worker_pool.start()
        logger.info("Manager mode: WorkerPool started",
                    extra={"workers": len(worker_pool.healthy_workers())})

    # Start controller — loads pipelines from DB, schedules them
    controller.start()

    if reconciler is not None:
        reconciler.start()

    watcher = None
    if config.watch_pipelines:
        try:
            from tram.watcher.pipeline_watcher import PipelineWatcher
            watcher = PipelineWatcher(pipeline_dir=config.pipeline_dir,
                                      manager=controller.manager)
            watcher.start()
            logger.info("Pipeline file watcher started", extra={"dir": config.pipeline_dir})
        except ImportError:
            logger.warning("Pipeline watcher requires watchdog — pip install tram[watch]")
        except Exception as exc:
            logger.warning("Pipeline watcher failed to start: %s", exc)

    logger.info("TRAM daemon ready", extra={"port": config.port})

    yield  # application runs here

    # ── Shutdown ───────────────────────────────────────────────────────────
    logger.info("TRAM daemon shutting down")

    if watcher is not None:
        watcher.stop()

    if reconciler is not None:
        reconciler.stop()

    controller.stop(timeout=config.shutdown_timeout)

    if worker_pool is not None:
        worker_pool.stop()

    db = getattr(app.state, "db", None)
    if db is not None:
        db.close()

    logger.info("TRAM daemon stopped")


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    import time

    if config is None:
        config = AppConfig.from_env()

    # Initialise persistence — retry up to 30 s so that PostgreSQL has time to
    # become ready in Kubernetes before we give up and fall back to in-memory.
    db = None
    _db_retry_interval = 5
    _db_max_retries = 6   # 6 × 5 s = 30 s
    for _attempt in range(_db_max_retries):
        try:
            from tram.persistence.db import TramDB
            db = TramDB(url=config.db_url, node_id=config.node_id)
            break
        except Exception as exc:
            remaining = _db_max_retries - _attempt - 1
            if remaining > 0:
                logger.warning(
                    "TramDB init attempt %d/%d failed, retrying in %ds: %s",
                    _attempt + 1, _db_max_retries, _db_retry_interval, exc,
                )
                time.sleep(_db_retry_interval)
            else:
                logger.warning(
                    "Could not initialise TramDB after %d attempts: %s"
                    " — run history will be in-memory only",
                    _db_max_retries, exc,
                )
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

    # Build WorkerPool when running as manager (TRAM_MODE=manager)
    from tram.agent.stats_store import StatsStore
    stats_store = StatsStore(interval=config.stats_interval)

    worker_pool = None
    reconciler = None
    if config.tram_mode == "manager":
        from tram.agent.worker_pool import WorkerPool
        worker_pool = WorkerPool.from_env(
            manager_url=config.manager_url,
            stats_store=stats_store,
            stats_interval=config.stats_interval,
        )
        if worker_pool is None:
            logger.warning(
                "TRAM_MODE=manager but no workers configured — "
                "set TRAM_WORKER_URLS or TRAM_WORKER_REPLICAS"
            )

    controller = PipelineController(
        db=db,
        file_tracker=file_tracker,
        node_id=config.node_id,
        worker_pool=worker_pool,
        manager_url=config.manager_url,
        stats_store=stats_store,
    )
    # Keep manager reference on controller's alert evaluator
    controller.manager._alert_evaluator = alert_evaluator
    if config.tram_mode == "manager" and worker_pool is not None and db is not None:
        from tram.agent.reconciler import PlacementReconciler
        reconciler = PlacementReconciler(
            controller=controller,
            worker_pool=worker_pool,
            stats_store=stats_store,
            db=db,
            stats_interval=config.stats_interval,
        )
    # Convenience alias — routers that still reference app.state.manager continue to work
    manager = controller.manager

    app = FastAPI(
        title="TRAM",
        description="Trishul Real-time Aggregation & Mediation",
        version=__version__,
        lifespan=lifespan,
    )

    # Store shared state
    app.state.config = config
    app.state.controller = controller
    app.state.manager = manager          # alias: controller.manager (routers use both)
    app.state.scheduler = controller     # alias: routers that use app.state.scheduler still work
    app.state.db = db
    app.state.alert_evaluator = alert_evaluator
    app.state.worker_pool = worker_pool
    app.state.stats_store = stats_store
    app.state.reconciler = reconciler
    from datetime import datetime
    app.state.started_at = datetime.now(UTC)

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
    app.include_router(mibs.router)
    app.include_router(schemas.router)
    app.include_router(auth.router)
    app.include_router(templates.router)
    app.include_router(stats.router)
    app.include_router(connectors.router)
    app.include_router(ai.router)
    app.include_router(internal.router)

    # Mount web UI static files (v1.0.7)
    ui_dir = config.ui_dir
    if ui_dir and os.path.isdir(ui_dir):
        @app.get("/", include_in_schema=False)
        async def _ui_root():
            return RedirectResponse(url="/ui/")

        app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")
        logger.info("Web UI mounted", extra={"path": "/ui", "dir": ui_dir})

    return app
